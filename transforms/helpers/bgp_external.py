"""External BGP session policy helpers: what an external (tenant VRF, handoff) session accepts and is announced.

Split out of ``transforms.helpers.bgp`` (session building and peer groups stay there). The prefix registries
(on-prem segments, office LANs, cloud VPC blocks) and the per-session policy built from them by the same-owner
exchange rule in ``utils/ownership.py``.
"""

import logging
from collections.abc import Iterable
from ipaddress import ip_address
from typing import Any

from transforms.helpers.bgp import _peering_ends
from utils.exchange_transit import EXCHANGE_PEERS
from utils.ownership import customer_owner, exchange_policies, ownership_conflicts

_log = logging.getLogger(__name__)


def get_internet_prefixes(prefixes: list[dict[str, Any]] | None) -> list[str]:
    """IPv4 public prefixes of the namespace_type internet namespace, sorted.

    ``prefixes`` is the cleaned IpamPrefix(role public) query root; the edge
    announces exactly these upstream (aggregate + RM-TRANSIT-OUT).
    """
    found = {
        p["prefix"]
        for p in prefixes or []
        if p.get("prefix")
        and ":" not in p["prefix"]
        and (p.get("ip_namespace") or {}).get("namespace_type") == "internet"
    }
    return _sorted_prefixes(found)


def _sorted_prefixes(prefixes: Iterable[str]) -> list[str]:
    """Prefixes in address order, then by length."""
    return sorted(prefixes, key=lambda p: (ip_address(p.split("/")[0]), int(p.split("/")[1])))


def _routable_ipv4(prefix: str | None) -> bool:
    """An IPv4 prefix other than the default route: what an external tenant-VRF filter may list."""
    return bool(prefix) and ":" not in str(prefix) and str(prefix).split("/")[0] != "0.0.0.0"


RETIRED_STATUS = "retired"


def get_onprem_prefixes(segments: list[dict[str, Any]] | None) -> dict[str, dict[str, list[str]]]:
    """Tenant VRF name -> owner -> the IPv4 prefixes behind that owner's on-prem segment gateways, sorted.

    ``segments`` is the cleaned ManagedNetworkSegment query root. A prefix behind
    a segment gateway is the on-prem routed space (the same definition
    checks/routing_invariants.py uses for the VPC overlap gate); a container
    (member_type prefix) is skipped. Only prod/non_prod namespaces qualify, each
    keeps its own lists, so an external peer in PROD is never offered a
    NON-PROD prefix. A ``retired`` segment is skipped (its space is no longer
    served), and so is a segment without a customer owner (customer_owner): the
    owner registers the prefix, so an ownerless one is announced to nobody.
    What an external tenant-VRF peer is announced (get_external_session_policies).
    """
    by_vrf: dict[str, dict[str, set[str]]] = {}
    for segment in segments or []:
        owner = customer_owner(segment.get("owner"))
        prefix = ((segment.get("gateway") or {}).get("ip_prefix")) or {}
        namespace = prefix.get("ip_namespace") or {}
        if (
            owner
            and segment.get("status") != RETIRED_STATUS
            and namespace.get("namespace_type") in EXCHANGE_PEERS
            and namespace.get("name")
            and prefix.get("member_type") != "prefix"
            and _routable_ipv4(prefix.get("prefix"))
        ):
            by_vrf.setdefault(namespace["name"], {}).setdefault(owner, set()).add(prefix["prefix"])
    return {
        vrf: {owner: _sorted_prefixes(found) for owner, found in sorted(owners.items())}
        for vrf, owners in sorted(by_vrf.items())
    }


def vif_registered_blocks(vif: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """The (owner, VPC aggregate) pairs a cloud virtual interface can reach.

    A private VIF: its own virtual network's blocks. A transit VIF: the blocks of
    every VPC attachment propagating into the route table its hybrid attachment
    is associated with, the table the Transit Gateway looks traffic from the VIF
    up in. A VPC propagating only elsewhere is unreachable over this VIF. The
    owner of a block is the owner of its own VPC's account (None when it is not
    a customer, or the account is unknown).
    """

    def blocks(virtual_network: dict[str, Any] | None) -> list[tuple[str | None, dict[str, Any]]]:
        network = virtual_network or {}
        owner = customer_owner(((network.get("account") or {}).get("owner")))
        return [(owner, block) for block in network.get("cidr_blocks") or []]

    if vif.get("virtual_network"):
        return blocks(vif["virtual_network"])
    hybrid = vif.get("transit_gateway_attachment") or {}
    table_id = (hybrid.get("associated_route_table") or {}).get("id")
    if not table_id:
        return []
    return [
        registered
        for attachment in ((hybrid.get("transit_gateway") or {}).get("attachments")) or []
        if attachment.get("attachment_type") == "vpc"
        and table_id in {table.get("id") for table in attachment.get("propagates_to_route_tables") or []}
        for registered in blocks(attachment.get("virtual_network"))
    ]


def _far_end_devices(device_capabilities: list[dict[str, Any]] | None, device_name: str) -> dict[str, set[str]]:
    """Local interface name -> the devices at the far end of this device's BGP peerings on it."""
    far: dict[str, set[str]] = {}
    for service in device_capabilities or []:
        if service.get("typename") != "ManagedBGP":
            continue
        for peering in service.get("peerings") or []:
            local_iface, remote_iface = _peering_ends(peering, device_name)
            remote = (remote_iface.get("device") or {}).get("name")
            if local_iface.get("name") and remote:
                far.setdefault(local_iface["name"], set()).add(remote)
    return far


def office_registered(office: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """The (owner, prefix) pairs one office footprint registers: its ``lan_prefixes`` and the hosted segments' prefixes.

    The ``lan_prefixes`` are owned by the office's owner. Derived, the gateway prefix of every segment the office
    hosts itself (``segment_deployments``: an office with a fabric hosts its LAN as a VXLAN segment, whose prefix
    comes from the tenant pool, not from the office) is owned by the segment's owner; a ``retired`` segment is
    skipped. ``office`` is one cleaned TopologyCustomerOffice.
    """
    office_owner = customer_owner(office.get("owner"))
    registered = [(office_owner, lan) for lan in office.get("lan_prefixes") or []]
    for deployment in office.get("segment_deployments") or []:
        segment = deployment.get("segment") or {}
        hosted = (segment.get("gateway") or {}).get("ip_prefix")
        if hosted and segment.get("status") != RETIRED_STATUS:
            registered.append((customer_owner(segment.get("owner")), hosted))
    return registered


def registered_by_gateway(
    offices: list[dict[str, Any]] | None, tunnels: list[dict[str, Any]] | None
) -> dict[str, list[tuple[str | None, dict[str, Any]]]]:
    """SD-WAN gateway name -> the (owner, prefix) pairs registered behind it, by offices and by tunnels.

    An office footprint attached to the gateway registers what office_registered returns. A tunnel registers its
    ``registered_prefixes`` (owned by the tunnel's owner) behind the gateway at one of its ends, the way an office
    LAN is: the space its far end reaches that no office of ours hosts (a B2B partner's range). Its other end (an
    office edge, a partner router) is no gateway and gets nothing.
    ``offices`` is the cleaned TopologyCustomerOffice query root, ``tunnels`` the TopologyTunnelCircuit one.
    A gateway with offices but no registered prefix is present with an empty list; a gateway no office names
    is absent, so a tunnel registers behind a gateway only where an office sits behind it too.
    """
    registered_by: dict[str, list[tuple[str | None, dict[str, Any]]]] = {}
    for office in offices or []:
        gateway = (office.get("sdwan_gateway") or {}).get("name")
        if gateway:
            registered_by.setdefault(gateway, []).extend(office_registered(office))
    for tunnel in tunnels or []:
        tunnel_owner = customer_owner(tunnel.get("owner"))
        ends = {(port.get("device") or {}).get("name") for port in tunnel.get("capabilities") or []}
        for gateway in sorted(name for name in ends if name in registered_by):
            registered_by[gateway].extend((tunnel_owner, prefix) for prefix in tunnel.get("registered_prefixes") or [])
    return registered_by


def registered_by_attachment(
    offices: list[dict[str, Any]] | None, tunnels: list[dict[str, Any]] | None, vifs: list[dict[str, Any]] | None
) -> dict[str, list[tuple[str | None, dict[str, Any]]]]:
    """Attachment point -> the (owner, prefix) pairs registered behind it, as the edge reads them.

    An attachment point is where an external session lands: an SD-WAN gateway (registered_by_gateway: its
    offices and tunnels) or the handoff circuit of a cloud virtual interface (vif_registered_blocks, the very
    chain walk the edge uses; a VIF without a handoff circuit has no session and is skipped). Shared by
    checks/routing_invariants.py rule 8 and checks/segment_security.py, so both see the registrations the
    edge accepts.
    """
    points = {
        f"SD-WAN gateway '{gateway}'": found for gateway, found in registered_by_gateway(offices, tunnels).items()
    }
    for vif in vifs or []:
        circuit = (vif.get("virtual_circuit") or {}).get("name")
        if circuit:
            points.setdefault(f"handoff circuit '{circuit}'", []).extend(vif_registered_blocks(vif))
    return points


def registered_by_namespace(registered: list[tuple[str | None, dict[str, Any]]]) -> dict[str, dict[str, set[str]]]:
    """Namespace name -> owner -> the routable IPv4 prefixes of the (owner, IpamPrefix) pairs registered there.

    A pair without a customer owner, a prefix without a namespace, the default route and IPv6 are dropped.
    Shared by the edge (get_external_peer_prefixes) and checks/routing_invariants.py rule 8, so both read
    "what an owner registered behind an attachment point" the same way.
    """
    found: dict[str, dict[str, set[str]]] = {}
    for owner, prefix in registered:
        namespace = (prefix.get("ip_namespace") or {}).get("name")
        if owner and namespace and _routable_ipv4(prefix.get("prefix")):
            found.setdefault(namespace, {}).setdefault(owner, set()).add(prefix["prefix"])
    return found


def get_external_peer_prefixes(
    interfaces: list[dict[str, Any]] | None,
    virtual_interfaces: list[dict[str, Any]] | None,
    device_name: str,
    *,
    device_capabilities: list[dict[str, Any]] | None,
    offices: list[dict[str, Any]] | None,
    tunnels: list[dict[str, Any]] | None,
) -> dict[str, dict[str, list[str]]]:
    """Handoff sub-interface name -> owner -> the far end's registered IPv4 prefixes of that owner, sorted.

    Two kinds of far end are known, each with its registered prefixes:

    - The SD-WAN gateway: a handoff whose only BGP far end
      (``device_capabilities`` peerings) is the ``sdwan_gateway`` of some
      office footprint accepts the ``lan_prefixes`` (owner: the office's owner)
      and the hosted segments' prefixes (owner: the segment's owner) of every
      office behind that gateway, and the ``registered_prefixes`` (owner: the
      tunnel's owner) of every tunnel ending on it (registered_by_gateway).
    - A cloud VIF: it lands on the device through its Direct Connect
      (``physical_devices``) as the sub-interface ``<port>.<vif.vlan_id>`` and
      accepts its reachable VPC blocks (vif_registered_blocks), each owned by
      its own VPC's account owner. Two VIFs of this device on one VLAN are
      ambiguous and accept nothing.

    Ownership belongs to the object that registers the prefix, never to the
    handoff circuit, and only a customer owner counts (customer_owner): a
    prefix without one is never accepted. Only prefixes registered in the
    sub-interface's own namespace are kept, so a PROD session never accepts a
    NON-PROD prefix, and never the default route or IPv6. A sub-interface with
    neither far end is absent, and a known far end with nothing customer-owned
    registered maps to ``{}``: both accept nothing.
    """
    by_vlan: dict[int, list[dict[str, Any]]] = {}
    for vif in virtual_interfaces or []:
        devices = ((vif.get("direct_connect") or {}).get("physical_devices")) or []
        if device_name in {device.get("name") for device in devices} and isinstance(vif.get("vlan_id"), int):
            by_vlan.setdefault(vif["vlan_id"], []).append(vif)
    registered_behind = registered_by_gateway(offices, tunnels)
    far_ends = _far_end_devices(device_capabilities, device_name)

    found: dict[str, dict[str, list[str]]] = {}
    for iface in interfaces or []:
        name = iface.get("name") or ""
        far = far_ends.get(name) or set()
        if len(far) == 1 and next(iter(far)) in registered_behind:
            registered = registered_behind[next(iter(far))]
        else:
            _, dot, tag = name.rpartition(".")
            vifs = by_vlan.get(int(tag)) if dot and tag.isdigit() else None
            if not vifs:
                continue
            if len(vifs) > 1:
                _log.warning(
                    "%s: VLAN %s is used by several cloud virtual interfaces (%s); %s accepts nothing",
                    device_name,
                    tag,
                    ", ".join(sorted(str(vif.get("name")) for vif in vifs)),
                    name,
                )
                continue
            registered = vif_registered_blocks(vifs[0])
        namespace = ((iface.get("ip_address") or {}).get("ip_namespace") or {}).get("name")
        by_owner = registered_by_namespace(registered).get(namespace or "", {})
        found[name] = {owner: _sorted_prefixes(prefixes) for owner, prefixes in sorted(by_owner.items())}
    return found


def get_external_session_policies(
    interfaces: list[dict[str, Any]] | None,
    peer_prefixes: dict[str, dict[str, list[str]]],
    onprem_prefixes: dict[str, dict[str, list[str]]],
    *,
    device_name: str = "",
) -> dict[str, dict[str, Any]]:
    """Handoff sub-interface name -> the prefix policy of its external session.

    ``owners`` are the customers that registered a prefix behind the session
    (``peer_prefixes``, get_external_peer_prefixes); ``in`` is the sorted union
    of their registered prefixes (what the far end may send); ``out`` is what
    the far end is announced, by the same-owner exchange rule
    (utils/ownership.py exchange_policies, one call per namespace with
    ``onprem_prefixes[<namespace>]`` as the segments): for each of the
    session's owners its on-prem segment prefixes and whatever it registered
    behind another session of the namespace, minus ``in`` (never announced
    back to where it was learned), and nothing of any other customer. A prefix
    registered by two owners, or one owner's prefix behind two sessions, is
    logged as a WARNING (ownership_conflicts) with ``device_name``. Exact
    prefixes only; a session with no owner has empty lists and the template
    renders deny-all both ways, a sub-interface without a namespace exchanges
    with nothing.
    """
    namespaces = {
        iface.get("name"): ((iface.get("ip_address") or {}).get("ip_namespace") or {}).get("name")
        for iface in interfaces or []
    }
    # One group per namespace; a session without one stands alone, it has no on-prem space to exchange.
    groups: dict[tuple[str | None, str | None], dict[str, dict[str, list[str]]]] = {}
    for name, by_owner in peer_prefixes.items():
        namespace = namespaces.get(name)
        groups.setdefault((namespace, None) if namespace else (None, name), {})[name] = by_owner

    policies: dict[str, dict[str, Any]] = {}
    for (namespace, _), learned in groups.items():
        segments = onprem_prefixes.get(namespace or "", {})
        for conflict in ownership_conflicts(segments, learned):
            _log.warning("%s: namespace %s: %s", device_name, namespace or "<none>", conflict.message)
        for name, policy in exchange_policies(segments, learned).items():
            policies[name] = {
                "owners": sorted(policy["owners"]),
                "in": _sorted_prefixes(policy["in"]),
                "out": _sorted_prefixes(policy["out"]),
            }
    return {name: policies[name] for name in peer_prefixes}
