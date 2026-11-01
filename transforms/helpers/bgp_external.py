"""External BGP session policy helpers: what an external (tenant VRF, handoff) session accepts and is announced.

Split out of ``transforms.helpers.bgp`` (session building and peer groups stay there). The prefix registries
(on-prem segments, office LANs, the prefixes registered through a provider connection) and the per-session policy built from them by the same-owner
exchange rule in ``utils/ownership.py``.
"""

import logging
from collections.abc import Iterable, Mapping
from ipaddress import ip_address
from typing import Any

from transforms.helpers.bgp import _peering_ends
from utils.exchange_transit import EXCHANGE_PEERS
from utils.ownership import (
    CircuitOwnerError,
    circuit_owner,
    connection_owner,
    customer_owner,
    exchange_policies,
    ownership_conflicts,
)
from utils.routing_identity import customer_community, list_name_part

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


def get_onprem_prefixes(segments: list[dict[str, Any]] | None) -> dict[str, dict[str, list[str]]]:
    """Tenant VRF name -> owner -> the IPv4 prefixes behind that owner's on-prem segment gateways, sorted.

    ``segments`` is the cleaned ManagedNetworkSegment query root. A prefix behind
    a segment gateway is the on-prem routed space (the same definition
    checks/routing_invariants.py uses for the VPC overlap gate); a container
    (member_type prefix) is skipped. Only prod/non_prod namespaces qualify, each
    keeps its own lists, so an external peer in PROD is never offered a
    NON-PROD prefix. A segment without a customer owner (customer_owner) is skipped:
    the owner registers the prefix, so an ownerless one is announced to nobody.
    What an external tenant-VRF peer is announced (get_external_session_policies).
    """
    by_vrf: dict[str, dict[str, set[str]]] = {}
    for segment in segments or []:
        owner = customer_owner(segment.get("owner"))
        prefix = ((segment.get("gateway") or {}).get("ip_prefix")) or {}
        namespace = prefix.get("ip_namespace") or {}
        if (
            owner
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


def connection_owner_key(connection: dict[str, Any]) -> str | None:
    """The customer key of a cleaned provider connection: its account's owner, else its own ``owner``."""
    return connection_owner(
        customer_owner(connection.get("owner")), customer_owner((connection.get("account") or {}).get("owner"))
    )


def session_circuit_owner(circuit: dict[str, Any]) -> str | None:
    """The customer key of a cleaned session circuit, derived (the circuit stores no owner).

    The owner of the single ``TopologyCustomerOffice`` among its ``locations`` (a tunnel), else the owner of its
    ``connection`` (a vpn circuit); ``utils/ownership.py`` circuit_owner. Raises ``CircuitOwnerError`` for two offices
    of different owners.
    """
    offices = [customer_owner(location.get("owner")) for location in circuit.get("locations") or []]
    connection = circuit.get("connection")
    return circuit_owner(offices, connection_owner_key(connection) if connection else None)


def connection_registered(connection: dict[str, Any]) -> list[tuple[str | None, dict[str, Any]]]:
    """The (owner, prefix) pairs registered behind a provider connection, by what it is attached to.

    ``attached_network`` (private): the blocks of that virtual network, owned by the owner of its account.
    ``attached_gateway`` (transit): the blocks of every VPC attachment propagating into the route table its
    ``transit_attachment`` is associated with, the table the Transit Gateway looks traffic from the connection up in;
    a VPC propagating only elsewhere is unreachable over it. A connection has at most one of the two (a check). The owner of a block is the owner of its own VPC's
    account (None when it is not a customer, or the account is unknown). Whatever it is attached to, the connection's
    ``registered_prefixes`` (a SaaS range, a partner range) are owned by the connection's owner
    (``connection_owner_key``).
    """

    def blocks(virtual_network: dict[str, Any] | None) -> list[tuple[str | None, dict[str, Any]]]:
        network = virtual_network or {}
        owner = customer_owner(((network.get("account") or {}).get("owner")))
        return [(owner, block) for block in network.get("cidr_blocks") or []]

    registered: list[tuple[str | None, dict[str, Any]]] = []
    if connection.get("attached_network"):
        registered += blocks(connection["attached_network"])
    elif connection.get("attached_gateway"):
        table_id = ((connection.get("transit_attachment") or {}).get("associated_route_table") or {}).get("id")
        if table_id:
            registered += [
                found
                for attachment in (connection.get("attached_gateway") or {}).get("attachments") or []
                if attachment.get("attachment_type") == "vpc"
                and table_id in {table.get("id") for table in attachment.get("propagates_to_route_tables") or []}
                for found in blocks(attachment.get("virtual_network"))
            ]
    owner = connection_owner_key(connection)
    return registered + [(owner, prefix) for prefix in connection.get("registered_prefixes") or []]


def connection_provider_space(connections: Iterable[dict[str, Any]] | None) -> frozenset[str]:
    """The prefixes registered through a provider connection (``registered_prefixes``): provider space, not an owner's.

    Passed to ``ownership_conflicts`` so two owners may each register the same SaaS range behind their own session.
    """
    return frozenset(
        prefix["prefix"]
        for connection in connections or []
        for prefix in connection.get("registered_prefixes") or []
        if prefix.get("prefix")
    )


def connections_by_interface(connections: list[dict[str, Any]] | None, device_name: str) -> dict[str, dict[str, Any]]:
    """Interface name of ``device_name`` -> the provider connection of the session circuit it is an end of.

    ``connections`` is the cleaned TopologyProviderConnection root with, per ``session_circuits``, the end ports
    (``capabilities``: interface name and device): the relationship edges name the connection, no name or VLAN is
    compared. A port is an end of at most one session circuit; if data puts one port on two connections the
    interface is dropped with a WARNING and accepts nothing.
    """
    found: dict[str, dict[str, dict[str, Any]]] = {}
    for connection in connections or []:
        for session in connection.get("session_circuits") or []:
            for end in session.get("capabilities") or []:
                if end.get("name") and (end.get("device") or {}).get("name") == device_name:
                    found.setdefault(end["name"], {})[str(connection.get("name"))] = connection
    unique: dict[str, dict[str, Any]] = {}
    for name, by_name in found.items():
        if len(by_name) > 1:
            _log.warning(
                "%s: %s is an end of sessions of several provider connections (%s); it accepts nothing",
                device_name,
                name,
                ", ".join(sorted(by_name)),
            )
            continue
        unique[name] = next(iter(by_name.values()))
    return unique


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
    comes from the tenant pool, not from the office) is owned by the segment's owner.
    ``office`` is one cleaned TopologyCustomerOffice.
    """
    office_owner = customer_owner(office.get("owner"))
    registered = [(office_owner, lan) for lan in office.get("lan_prefixes") or []]
    for deployment in office.get("segment_deployments") or []:
        segment = deployment.get("segment") or {}
        hosted = (segment.get("gateway") or {}).get("ip_prefix")
        if hosted:
            registered.append((customer_owner(segment.get("owner")), hosted))
    return registered


def registered_by_gateway(
    offices: list[dict[str, Any]] | None, tunnels: list[dict[str, Any]] | None
) -> dict[str, list[tuple[str | None, dict[str, Any]]]]:
    """SD-WAN gateway name -> the (owner, prefix) pairs registered behind it, by offices and by tunnels.

    An office footprint attached to the gateway registers what office_registered returns. A tunnel registers its
    ``registered_prefixes`` (owned by the tunnel's owner, derived: session_circuit_owner) behind the gateway at one of its ends, the way an office
    LAN is: the space its far end reaches that no office of ours hosts (a B2B partner's range). Its other end (an
    office edge, a partner router) is no gateway and gets nothing.
    ``offices`` is the cleaned TopologyCustomerOffice query root, ``tunnels`` the tunnel TopologySessionCircuit one.
    A gateway with offices but no registered prefix is present with an empty list; a gateway no office names
    is absent, so a tunnel registers behind a gateway only where an office sits behind it too.
    """
    registered_by: dict[str, list[tuple[str | None, dict[str, Any]]]] = {}
    for office in offices or []:
        gateway = (office.get("sdwan_gateway") or {}).get("name")
        if gateway:
            registered_by.setdefault(gateway, []).extend(office_registered(office))
    for tunnel in tunnels or []:
        try:
            tunnel_owner = session_circuit_owner(tunnel)
        except CircuitOwnerError as exc:
            _log.warning("tunnel %s: %s; its registered prefixes are accepted from nobody", tunnel.get("name"), exc)
            tunnel_owner = None
        ends = {(port.get("device") or {}).get("name") for port in tunnel.get("capabilities") or []}
        for gateway in sorted(name for name in ends if name in registered_by):
            registered_by[gateway].extend((tunnel_owner, prefix) for prefix in tunnel.get("registered_prefixes") or [])
    return registered_by


def registered_by_attachment(
    offices: list[dict[str, Any]] | None,
    tunnels: list[dict[str, Any]] | None,
    connections: list[dict[str, Any]] | None,
) -> dict[str, list[tuple[str | None, dict[str, Any]]]]:
    """Attachment point -> the (owner, prefix) pairs registered behind it, as the edge reads them.

    An attachment point is where an external session lands: an SD-WAN gateway (registered_by_gateway: its
    offices and tunnels) or a provider connection (connection_registered; a connection with no session circuit
    has no session and is skipped). The sessions of one connection (the two circuits of a dual connection) are one
    point. Shared by checks/routing_invariants.py rule 8 and checks/segment_security.py, so both see the
    registrations the edge accepts.
    """
    points = {
        f"SD-WAN gateway '{gateway}'": found for gateway, found in registered_by_gateway(offices, tunnels).items()
    }
    for connection in connections or []:
        if connection.get("name") and connection.get("session_circuits"):
            points.setdefault(f"provider connection '{connection['name']}'", []).extend(
                connection_registered(connection)
            )
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
    connections: Mapping[str, dict[str, Any]] | None,
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
      tunnel's derived owner) of every tunnel ending on it (registered_by_gateway).
    - A provider connection: ``connections`` (connections_by_interface) maps the sub-interface to the connection
      of the session circuit it is an end of, and the session accepts what the connection registers
      (connection_registered): a private network's blocks, a transit gateway's reachable blocks, the
      connection's own ``registered_prefixes``.

    Ownership belongs to the object that registers the prefix, never to the
    handoff circuit, and only a customer owner counts (customer_owner): a
    prefix without one is never accepted. Only prefixes registered in the
    sub-interface's own namespace are kept, so a PROD session never accepts a
    NON-PROD prefix, and never the default route or IPv6. A sub-interface with
    neither far end is absent, and a known far end with nothing customer-owned
    registered maps to ``{}``: both accept nothing.
    """
    registered_behind = registered_by_gateway(offices, tunnels)
    far_ends = _far_end_devices(device_capabilities, device_name)

    found: dict[str, dict[str, list[str]]] = {}
    for iface in interfaces or []:
        name = iface.get("name") or ""
        far = far_ends.get(name) or set()
        if len(far) == 1 and next(iter(far)) in registered_behind:
            registered = registered_behind[next(iter(far))]
        elif name in (connections or {}):
            registered = connection_registered((connections or {})[name])
        else:
            continue
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
    customer_tags: Mapping[str, int] | None = None,
    routing_identity: bool = False,
    connections: Mapping[str, dict[str, Any]] | None = None,
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
    logged as a WARNING (ownership_conflicts) with ``device_name``; ``connections`` (connections_by_interface) tells it
    that the sessions of one provider connection are one attachment point and which prefixes are provider space
    (connection_provider_space). Exact
    prefixes only; a session with no owner has empty lists and the template
    renders deny-all both ways, a sub-interface without a namespace exchanges
    with nothing.

    With ``routing_identity`` (any customer or segment carries a routing tag, utils/routing_identity.py
    identity_in_use) a policy also has ``identity: True`` (the template strips our communities from what it
    sends and accepts) and ``in_owners``: one entry per owner with an accepted prefix, sorted, as ``owner``,
    ``name`` (the owner, safe in a prefix-list name), ``prefixes`` and ``community`` (the owner's
    ``64496:<tag>`` from ``customer_tags``, None when it has no tag). The inbound community is set per
    prefix through the owner that registered it, so a session with several owners tags each correctly.
    Without ``routing_identity`` the policy is exactly the three fields above.
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

    session_groups = {name: str(connection.get("name")) for name, connection in (connections or {}).items()}
    provider_space = connection_provider_space((connections or {}).values())
    policies: dict[str, dict[str, Any]] = {}
    for (namespace, _), learned in groups.items():
        segments = onprem_prefixes.get(namespace or "", {})
        for conflict in ownership_conflicts(
            segments, learned, session_groups=session_groups, provider_space=provider_space
        ):
            _log.warning("%s: namespace %s: %s", device_name, namespace or "<none>", conflict.message)
        for name, policy in exchange_policies(segments, learned).items():
            policies[name] = {
                "owners": sorted(policy["owners"]),
                "in": _sorted_prefixes(policy["in"]),
                "out": _sorted_prefixes(policy["out"]),
            }
            if routing_identity:
                policies[name].update(identity=True, in_owners=_in_owners(learned[name], customer_tags or {}))
    return {name: policies[name] for name in peer_prefixes}


def _in_owners(by_owner: Mapping[str, list[str]], customer_tags: Mapping[str, int]) -> list[dict[str, Any]]:
    """The inbound policy entries of one session: its owners with a prefix, each with its customer community."""
    return [
        {
            "owner": owner,
            "name": list_name_part(owner),
            "prefixes": _sorted_prefixes(prefixes),
            "community": customer_community(customer_tags[owner]) if owner in customer_tags else None,
        }
        for owner, prefixes in sorted(by_owner.items())
        if prefixes
    ]
