"""Readers and judges of the decommission steps of test_67: a provider connection set to `decommissioned` is torn down
by add_provider_connection and what it owned must be gone, what it never owns must be as it was.

What the generator removes (in this order): the peerings `<connection>-L<n>-bgp`, the addresses on the leg interfaces,
the link prefix it allocated (a provider-assigned `link_prefixes` range is authored: kept), the session circuits, the
sub-interfaces `<port>.<vlan>` / `Tunnel<n>`, then the carrier `<connection>-XC` when it created it and nobody touched it
(otherwise it is set to `decommissioned` and kept) and, only then, the NIC `vnic-<connection>`. The connection stays, for
a person to delete. Never touched: physical ports, the stub device with its Loopback0 and BGP process, an authored or
adopted carrier.

The test records the ids of everything BEFORE the status change (``fetch_recorded``) and asserts each by id afterwards
(``fetch_alive`` and the pure ``presence_errors``): a name sweep would also pass when the object was re-created. The judges
are pure (tests/unit/test_integration_provider_connection_decommission.py runs them on canned data); the fetchers and
``create_authored_carrier`` are the only code that talks to a client.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .provider_connection_helpers import PEERINGS_QUERY
from .routed_link_rules import LABEL_HANDOFF, LABEL_VPN

DECOMMISSIONED = "decommissioned"

# The kinds a teardown reclaims, in the order the generator removes them; the carrier and the NIC come last and only
# when the carrier was generated.
KIND_PEERING = "ManagedBGPPeering"
KIND_ADDRESS = "IpamIPAddress"
KIND_PREFIX = "IpamPrefix"
KIND_CIRCUIT = "TopologySessionCircuit"
KIND_INTERFACE = "DcimVirtualInterface"
KIND_CARRIER = "TopologyCarrierCircuit"
LEG_KINDS = (KIND_PEERING, KIND_ADDRESS, KIND_PREFIX, KIND_CIRCUIT, KIND_INTERFACE)

# The authored carrier test_67 puts under the adoption scenario: a person's carrier (the provider's own circuit id, status
# active), so a decommission of the connection that adopted it must leave it exactly as it was.
AUTHORED_NAME = "PC-TEST67-AUTHORED"
AUTHORED_CARRIER_NAME = f"{AUTHORED_NAME}-XC"
AUTHORED_CARRIER = "TEST67-PROVIDER-CIRCUIT-1"  # its circuit_id, the key the inventory and carrier_state use
AUTHORED_NIC = f"vnic-{AUTHORED_NAME}"

# The session circuits of a connection with the interfaces they end on, each with its address, and the carriers it lists.
OUTPUTS_QUERY = """
query ($name: String!) {
  TopologyProviderConnection(name__value: $name) {
    edges {
      node {
        id
        carrier_circuits { edges { node { id circuit_id { value } } } }
        session_circuits {
          edges {
            node {
              id
              name { value }
              capabilities {
                edges {
                  node {
                    id
                    name { value }
                    ... on DcimVirtualInterface { ip_address { node { id address { value } } } }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

PREFIXES_QUERY = """
query ($descriptions: [String]) {
  IpamPrefix(description__values: $descriptions) { edges { node { id prefix { value } } } }
}
"""

# Recorded ids: kind -> {id: what it is, to name it in a finding}.
Recorded = dict[str, dict[str, str]]


def outputs_from_row(row: dict[str, Any] | None) -> Recorded:
    """The circuits, leg interfaces and addresses of a cleaned OUTPUTS_QUERY row (``clean_data`` of the node), by kind.

    A leg interface is an end of a session circuit that is not a physical port: only `DcimVirtualInterface` ends carry
    an ``ip_address`` key in the payload, so a physical near port (no such key) is never recorded as torn down.
    """
    recorded: Recorded = {kind: {} for kind in (KIND_CIRCUIT, KIND_INTERFACE, KIND_ADDRESS)}
    for circuit in (row or {}).get("session_circuits") or []:
        recorded[KIND_CIRCUIT][str(circuit["id"])] = str(circuit["name"])
        for end in circuit.get("capabilities") or []:
            if "ip_address" not in end:
                continue
            recorded[KIND_INTERFACE][str(end["id"])] = f"{circuit['name']} end {end['name']}"
            if end["ip_address"]:
                address = end["ip_address"]
                recorded[KIND_ADDRESS][str(address["id"])] = f"{end['name']} {address['address']}"
    return recorded


def peering_names(recorded: Recorded) -> list[str]:
    """`<circuit>-bgp` of every recorded session circuit: the peering names add_virtual_circuit gives them."""
    return sorted(f"{name}-bgp" for name in recorded[KIND_CIRCUIT].values())


def prefix_descriptions(recorded: Recorded) -> list[str]:
    """The descriptions add_virtual_circuit gives the link prefix it allocates, for either flavour of the circuit."""
    return sorted(f"{label} {name}" for name in recorded[KIND_CIRCUIT].values() for label in (LABEL_HANDOFF, LABEL_VPN))


async def fetch_recorded(client: InfrahubClient, branch: str, connection: str) -> tuple[str | None, Recorded]:
    """The connection's id and everything it owns on the branch right now, by kind and id: the circuits, their leg
    interfaces and addresses, the peerings add_virtual_circuit built and the prefixes it allocated. A provider-assigned
    range is authored, has none of the descriptions, and is not recorded. The carrier and the NIC are the caller's."""
    result = clean_data(
        await client.execute_graphql(query=OUTPUTS_QUERY, variables={"name": connection}, branch_name=branch)
    )
    rows = result.get("TopologyProviderConnection") or []
    row = rows[0] if rows else None
    recorded = outputs_from_row(row)
    recorded[KIND_PEERING] = {}
    recorded[KIND_PREFIX] = {}
    if recorded[KIND_CIRCUIT]:
        peerings = clean_data(
            await client.execute_graphql(
                query=PEERINGS_QUERY, variables={"names": peering_names(recorded)}, branch_name=branch
            )
        )
        recorded[KIND_PEERING] = {str(p["id"]): str(p["name"]) for p in peerings.get("ManagedBGPPeering") or []}
        prefixes = clean_data(
            await client.execute_graphql(
                query=PREFIXES_QUERY, variables={"descriptions": prefix_descriptions(recorded)}, branch_name=branch
            )
        )
        recorded[KIND_PREFIX] = {str(p["id"]): str(p["prefix"]) for p in prefixes.get("IpamPrefix") or []}
    return (str(row["id"]) if row else None), recorded


async def fetch_alive(client: InfrahubClient, branch: str, recorded: Recorded) -> dict[str, set[str]]:
    """Of the recorded ids, the ones that still exist on the branch, by kind (one filter per kind)."""
    alive: dict[str, set[str]] = {}
    for kind, ids in recorded.items():
        found = await client.filters(kind=kind, branch=branch, ids=sorted(ids)) if ids else []
        alive[kind] = {str(node.id) for node in found}
    return alive


async def fetch_peering_names(client: InfrahubClient, branch: str, connection: str, links: int = 4) -> list[str]:
    """The peerings named `<connection>-L<n>-bgp` that exist (the name sweep that backs the id check up)."""
    names = [f"{connection}-L{n}-bgp" for n in range(1, links + 1)]
    result = clean_data(
        await client.execute_graphql(query=PEERINGS_QUERY, variables={"names": names}, branch_name=branch)
    )
    return sorted(str(p["name"]) for p in result.get("ManagedBGPPeering") or [])


async def fetch_ids(client: InfrahubClient, branch: str, kind: str, **filters: Any) -> dict[str, str]:
    """name-or-label -> id of the ``kind`` nodes matching ``filters`` (the objects a teardown must not touch)."""
    return {
        str(getattr(getattr(node, "name", None), "value", node.id)): str(node.id)
        for node in await client.filters(kind=kind, branch=branch, **filters)
    }


def presence_errors(
    recorded: Recorded, alive: dict[str, set[str]], *, expect_present: bool, kinds: tuple[str, ...]
) -> list[str]:
    """Findings for the recorded objects of ``kinds`` whose existence is not the expected one: still there after a
    teardown (``expect_present`` False), or gone although the teardown must keep them (True). An empty record of a kind
    is not a finding here; ``recorded_errors`` names the record that is too thin to prove anything."""
    errors: list[str] = []
    for kind in kinds:
        for node_id, label in sorted(recorded.get(kind, {}).items(), key=lambda item: item[1]):
            present = node_id in alive.get(kind, set())
            if present != expect_present:
                errors.append(f"{kind} {label} ({node_id}) {'still exists' if present else 'is gone'}")
    return errors


def recorded_errors(recorded: Recorded, *, addressed: bool = True, allocated: bool = True) -> list[str]:
    """The record taken before a status change must hold what the teardown is meant to remove: a session circuit, two
    leg interfaces per circuit, a peering per circuit, and (``addressed``/``allocated``) an address per interface and a
    prefix from our pool; otherwise 'everything is gone' would prove nothing."""
    circuits = len(recorded.get(KIND_CIRCUIT, {}))
    errors = [] if circuits else ["nothing recorded: the connection holds no session circuit"]
    wanted = {
        KIND_INTERFACE: circuits * 2,
        KIND_PEERING: circuits,
        **({KIND_ADDRESS: circuits * 2} if addressed else {}),
        **({KIND_PREFIX: circuits} if allocated else {}),
    }
    for kind, count in sorted(wanted.items()):
        if circuits and len(recorded.get(kind, {})) != count:
            errors.append(f"recorded {len(recorded.get(kind, {}))} {kind}, expected {count} for {circuits} circuit(s)")
    return errors


def decommissioned_row_errors(
    row: dict[str, Any] | None, name: str, *, carriers: list[str] | None = None, status: str = DECOMMISSIONED
) -> list[str]:
    """The connection after the teardown: still there (a person deletes it), status ``status``, no session circuit,
    and, when ``carriers`` is given, exactly that carrier list (empty once its generated carrier is gone; an adopted
    carrier is not asserted by the caller that passes None)."""
    if row is None:
        return [f"{name}: the connection is gone, a decommission keeps it for a person to delete"]
    errors = []
    if row["status"] != status:
        errors.append(f"{name}: status {row['status']!r}, expected {status!r}")
    if row["sessions"]:
        errors.append(f"{name}: still lists session circuits {row['sessions']}")
    if carriers is not None and row["carriers"] != sorted(carriers):
        errors.append(f"{name}: carrier_circuits {row['carriers']}, expected {sorted(carriers)}")
    return errors


def port_free_errors(
    port_rows: list[dict[str, Any]], inventory: dict[str, list[dict[str, Any]]], near: tuple[str, str]
) -> list[str]:
    """The near port ``near`` (device, interface) is free again: it exists among the candidate ports
    (provider_connection_helpers.fetch_role_ports), has no sub-interface and ends no live carrier and no session circuit."""
    row = next((p for p in port_rows if (p["device"]["name"], p["name"]) == near), None)
    if row is None:
        return [f"{near[0]}:{near[1]} is no longer a candidate port"]
    live_ends = [e for c in inventory["physical"] if c["status"] != DECOMMISSIONED for e in c["end_ids"]]
    session_ends = [end["id"] for c in inventory["virtual"] for end in c["end_nodes"]]
    errors = []
    if row.get("sub_interfaces"):
        errors.append(f"{near[0]}:{near[1]} still has {len(row['sub_interfaces'])} sub-interface(s)")
    if row["id"] in live_ends:
        errors.append(f"{near[0]}:{near[1]} still ends a live carrier")
    if row["id"] in session_ends:
        errors.append(f"{near[0]}:{near[1]} still ends a session circuit")
    return errors


def moved_ids_errors(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """What a teardown never touches, by name: each object recorded before has the same id after (a re-created stub
    interface or process has another id)."""
    return [
        f"{name}: id {after.get(name)!r}, expected {node_id!r}"
        for name, node_id in sorted(before.items())
        if after.get(name) != node_id
    ]


async def create_authored_carrier(
    client: InfrahubClient,
    branch: str,
    *,
    near_port_id: str,
    stub_device_id: str,
    provider_id: str,
    carrier_type: str = "virtual_connection",
) -> tuple[str, str]:
    """Author what a person would: the NIC ``AUTHORED_NIC`` on the stub device and the carrier ``AUTHORED_CARRIER_NAME`` (the
    provider's own circuit id, status active, so the generator treats it as authored) between ``near_port_id`` and it.
    Returns (carrier id, NIC id)."""
    nic = await client.create(
        kind=KIND_INTERFACE,
        data={
            "name": AUTHORED_NIC,
            "description": "test_67 authored service-edge NIC: deleted by the test",
            "device": {"id": stub_device_id},
            "status": "active",
            "role": "uplink",
        },
        branch=branch,
    )
    await nic.save(allow_upsert=True)
    carrier = await client.create(
        kind=KIND_CARRIER,
        data={
            "name": AUTHORED_CARRIER_NAME,
            "circuit_id": AUTHORED_CARRIER,
            "circuit_type": carrier_type,
            "status": "active",
            "description": "test_67 authored carrier: deleted by the test",
            "provider": {"id": provider_id},
            "locations": [],
            "capabilities": [{"id": near_port_id}, {"id": str(nic.id)}],
        },
        branch=branch,
    )
    await carrier.save(allow_upsert=True)
    return str(carrier.id), str(nic.id)
