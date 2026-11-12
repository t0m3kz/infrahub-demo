"""In-process renders and graph snapshots (shared by test_64, test_65)."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from infrahub_sdk import InfrahubClient

from transforms.config.firewall import Firewall

from .service_inventory_helpers import _value

# In-process renders and graph snapshots (shared by test_64, test_65)

_REPO_ROOT = str(Path(__file__).parent.parent.parent)


async def render_device_config(
    client: InfrahubClient,
    branch: str,
    device: str,
    query: str = "firewall_config",
    transform_cls: type[Any] = Firewall,
) -> str:
    """Run a device-config query and its transform in-process, return the rendered config."""
    raw = await client.query_gql_query(name=query, branch_name=branch, variables={"device": device})
    data: dict[str, Any] = raw.get("data") or raw

    transform = transform_cls.__new__(transform_cls)
    transform.root_directory = _REPO_ROOT
    rendered = await transform.transform(data)
    assert isinstance(rendered, str) and rendered, f"{query} transform produced no output for device {device}"
    return rendered


# Kind -> the selection that must not change on a rerun.
TOPOLOGY_SNAPSHOT_KINDS = {
    "DcimDevice": "name { value }",
    # ip_address is declared on each concrete interface kind, not on the generic
    "DcimInterface": (
        "name { value } capabilities { edges { node { id } } } "
        "... on DcimPhysicalInterface { ip_address { node { id address { value } } } } "
        "... on DcimVirtualInterface { ip_address { node { id address { value } } } "
        "role { value } } "
        "... on DcimLAGInterface { ip_address { node { id address { value } } } }"
    ),
    # cable_id, color and length_cm are derived by the cabling, HA and MLAG sites from the endpoints
    "DcimCable": "endpoints { edges { node { id } } } cable_id { value } color { value } length_cm { value }",
    "IpamIPAddress": "address { value }",
    "IpamPrefix": "prefix { value }",
    "CoreNumberPool": "name { value }",
    "CoreIPPrefixPool": "name { value }",
    "CoreIPAddressPool": "name { value }",
    "RoutingAutonomousSystem": "asn { value }",
    "ManagedBGP": "local_as { node { id } }",
    # The <vc>-underlay / <vc>-evpn (Interconnect), <vc>-bgp (Handoff) and FABRIC-* sessions
    "ManagedBGPPeering": (
        "name { value } peering_role { value } session_type { value } ttl { value } bfd_enabled { value } "
        "send_extended_community { value } password { node { id } } "
        "bgp_processes { edges { node { id } } } address_families { edges { node { id } } } "
        "capabilities { edges { node { id } } }"
    ),
    "ManagedHA": "group_id { value }",
    "ManagedSegmentDeployment": "vni { value }",
    "ManagedSegmentVlan": "vlan_id { value } device { node { id } }",
    # VRF instances and segment SVIs are untracked and converged by generators/helpers/vrf.py: any change on rerun is a
    # diff.
    "ManagedVRF": (
        "name { value } deployment { node { id } } security_zone { node { id } } "
        "shared_devices { edges { node { id } } }"
    ),
    "LocationRack": "name { value }",
    "TopologyPod": "name { value }",
    "TopologyDataCenter": "name { value }",
    # add_circuit/add_sdwan_edge output (the carrier circuit and the tunnel)
    "TopologyCarrierCircuit": (
        "name { value } locations { edges { node { id } } } capabilities { edges { node { id } } }"
    ),
    "TopologySessionCircuit": (
        "name { value } circuit_type { value } locations { edges { node { id } } } "
        "capabilities { edges { node { id capabilities { edges { node { id } } } } } } "
        "vrf { node { id } } password { node { id } } "
        "link_prefix { node { id } } registered_prefixes { edges { node { id } } }"
    ),
    "ManagedController": "name { value } managed_devices { edges { node { id } } }",
    "ManagedFirewallHA": "name { value }",
    "ManagedFirewallContext": "name { value }",
    "ManagedLoadbalancerContext": "name { value }",
    "ManagedMLAG": "name { value } peer_devices { value }",
    "RoutingOSPFArea": "name { value } area { value }",
    # The office fabric's OSPF underlay (add_office_fabric): the only OSPF in 30_all.
    "ManagedOSPF": "name { value }",
    "ManagedOSPFPeering": "name { value }",
    "RoutingOSPFInterface": "name { value }",
    # The value is in the snapshot on purpose
    "RoutingPassword": "name { value } password { value }",
    "RoutingBGPAddressFamily": "",
    "SecurityZone": "name { value }",
    "SecurityPolicy": "name { value }",
    "ProxyPolicy": "name { value }",
    "CoreStandardGroup": "name { value } members { edges { node { id } } }",
}


# Kind -> name prefixes left out of the snapshot.
SNAPSHOT_EXCLUDED_NAME_PREFIXES = {"CoreStandardGroup": ("lock-",)}


# Explicit GraphQL paging: the branch holds thousands of interfaces and addresses
SNAPSHOT_PAGE_SIZE = 1000


def _canonical(value: Any) -> Any:
    """Hashable form of a GraphQL node: edges become a sorted tuple."""
    if isinstance(value, dict):
        if "edges" in value:
            return tuple(sorted((_canonical(edge["node"]) for edge in value["edges"]), key=repr))
        return tuple(sorted((key, _canonical(item)) for key, item in value.items()))
    return value


def _excluded(kind: str, node: dict[str, Any]) -> bool:
    prefixes = SNAPSHOT_EXCLUDED_NAME_PREFIXES.get(kind)
    return bool(prefixes) and str(_value(node.get("name")) or "").startswith(prefixes)


def snapshot_query(kind: str, selection: str, offset: int) -> str:
    """One page of ``kind`` for snapshot_topology: id, concrete kind and display label, then ``selection``."""
    return (
        f"query {{ {kind}(limit: {SNAPSHOT_PAGE_SIZE}, offset: {offset}) "
        f"{{ count edges {{ node {{ id __typename display_label {selection} }} }} }} }}"
    )


async def snapshot_topology(
    client: InfrahubClient, branch: str, kinds: Iterable[str] | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Kind -> {id: comparable state} for TOPOLOGY_SNAPSHOT_KINDS (or only ``kinds`` of it), paged, plus id ->
    "<concrete kind> <display_label>" so a diff can name what moved."""
    snapshot: dict[str, dict[str, Any]] = {}
    labels: dict[str, str] = {}
    wanted = TOPOLOGY_SNAPSHOT_KINDS if kinds is None else {kind: TOPOLOGY_SNAPSHOT_KINDS[kind] for kind in kinds}
    for kind, selection in wanted.items():
        objects: dict[str, Any] = {}
        offset = 0
        while True:
            query = snapshot_query(kind, selection, offset)
            result = (await client.execute_graphql(query=query, branch_name=branch))[kind]
            for edge in result["edges"]:
                node = dict(edge["node"])
                if _excluded(kind, node):
                    continue
                node_id = node.pop("id")
                labels[node_id] = f"{node.pop('__typename')} '{node.pop('display_label')}'"
                objects[node_id] = _canonical(node)
            offset += SNAPSHOT_PAGE_SIZE
            if offset >= result["count"]:
                break
        snapshot[kind] = objects
    return snapshot, labels


def diff_snapshots(
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    labels: dict[str, str] | None = None,
) -> list[str]:
    """Deleted, added and changed objects per kind; `labels` (id -> kind and display label, from both snapshots) names
    each one."""
    labels = labels or {}

    def _name(kind: str, node_id: str) -> str:
        return f"{kind} {node_id} [{labels[node_id]}]" if node_id in labels else f"{kind} {node_id}"

    changes: list[str] = []
    for kind, old in before.items():
        new = after[kind]
        changes += [f"{_name(kind, node_id)} deleted: {old[node_id]}" for node_id in sorted(old.keys() - new.keys())]
        changes += [f"{_name(kind, node_id)} added: {new[node_id]}" for node_id in sorted(new.keys() - old.keys())]
        changes += [
            f"{_name(kind, node_id)} changed: {old[node_id]} -> {new[node_id]}"
            for node_id in sorted(old.keys() & new.keys())
            if old[node_id] != new[node_id]
        ]
    return changes
