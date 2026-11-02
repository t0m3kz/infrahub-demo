"""The cloud side of an external BGP session: what a ``CloudPeeringInterface`` tells the edge and the checks.

A cloud leg is a hand-written ``ManagedBGPPeering`` whose OUR end is an interface of one of our devices (a sub-interface
or a routed port). The far end is not a device with a BGP process: it is the ``CloudPeeringInterface`` that ends the same
``TopologySessionCircuit`` (an interface of a cloud transport, ``CloudDirectConnect`` or ``CloudVPNConnection``). This
module reads that leg from the cleaned query roots ``CloudPeeringInterface`` and ``CloudGatewayAttachment`` into
``CloudLeg`` records, the one reading the edge transform and ``checks/routing_invariants.py`` and
``checks/segment_security.py`` share:

- the neighbor: the peering's ``peer_ip`` and ``peer_asn``, the BGP key ``auth``;
- the owner: the transport's, never the peering's: its account's owner, else (a SaaS has no account) its own ``owner``;
  only an ``OrganizationCustomer`` counts (``utils/ownership.py`` customer_owner);
- the prefixes registered behind the session, cloud to us (``registered``): for a private or saas peering the
  ``allowed_prefixes`` of every gateway attachment reached from the peering's gateway, an attachment with none meaning
  the CIDR blocks of its virtual network; the walk goes gateway -> attachments -> peer gateways (hubs) -> their
  attachments, once per gateway. Entering a hub through an attachment that has an associated route table, only the VPC
  attachments propagating into that table count. A public peering registers nothing;
- the prefixes WE announce to the cloud (``advertised``): the peering's ``advertised_prefixes``, public or static only.

The exchange rule itself stays in ``utils/ownership.py``.
"""

import logging
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from utils.ownership import customer_owner

_log = logging.getLogger(__name__)

PEERING_KIND = "CloudPeeringInterface"
# A session circuit that routes on top of a cloud peering: a handoff (BGP) or a vpn (BGP over a tunnel pair). The
# tunnel type is inventory only (a static SaaS tunnel) and carries no session.
_SESSION_TYPES = frozenset({"handoff", "vpn"})
_REGISTERING_SCOPES = frozenset({"private", "saas"})

Registered = tuple[str | None, dict[str, Any]]


@dataclass(frozen=True)
class CloudLeg:
    """One cloud session: our interface, the cloud peering interface that ends its circuit, and what it registers."""

    peering: str
    transport: str
    transport_kind: str | None
    circuit: str
    local: tuple[str, str]  # (our device, our interface name)
    owner: str | None
    group: str  # the attachment point: the gateway the peering lands on, else the peering itself
    scope: str | None
    peer_ip: dict[str, Any] | None
    peer_asn: int | None
    password: str | None
    registered: tuple[Registered, ...]
    advertised: tuple[str, ...]

    @property
    def point(self) -> str:
        """The attachment point of the leg, as a message names it: its gateway, shared by a dual connection."""
        return f"cloud gateway '{self.group}'"


def _ref(node: Any) -> str | None:
    """The id of a cleaned reference: a bare id string, or a dict carrying ``id`` (else its name)."""
    if isinstance(node, dict):
        return node.get("id") or node.get("name")
    return node or None


def _name(node: Any) -> str | None:
    """The name of a cleaned reference, None for a bare id string."""
    return node.get("name") if isinstance(node, dict) else None


def _prefix_dicts(prefixes: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [prefix for prefix in prefixes or [] if isinstance(prefix, dict) and prefix.get("prefix")]


def _attachment_prefixes(attachment: dict[str, Any]) -> list[dict[str, Any]]:
    """What one attachment lets through, cloud to us: its allowed prefixes, else its network's CIDR blocks."""
    allowed = _prefix_dicts(attachment.get("allowed_prefixes"))
    if allowed or not isinstance(attachment.get("virtual_network"), dict):
        return allowed
    return _prefix_dicts(attachment["virtual_network"].get("cidr_blocks"))


def reachable_prefixes(start: Any, attachments: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The prefix objects the attachments reachable from the gateway ``start`` let through, in a stable order.

    ``start`` is the cleaned gateway reference of the peering; ``attachments`` the cleaned CloudGatewayAttachment
    root. An attachment joins its ``gateway`` to one target, a ``virtual_network`` or a ``peer_gateway``: a hop goes
    either way across the second kind. Each gateway is visited once (a loop of hubs ends), and a hub entered through
    an attachment with an ``associated_route_table`` only lets the VPC attachments through that propagate into it.
    """
    start_id = _ref(start)
    if not start_id:
        return []
    by_gateway: dict[str, list[dict[str, Any]]] = {}
    for attachment in attachments or []:
        for end in (attachment.get("gateway"), attachment.get("peer_gateway")):
            if gateway := _ref(end):
                by_gateway.setdefault(gateway, []).append(attachment)
    found: dict[tuple[str, str], dict[str, Any]] = {}

    def keep(prefixes: Iterable[dict[str, Any]]) -> None:
        for prefix in prefixes:
            found.setdefault(((prefix.get("ip_namespace") or {}).get("name") or "", prefix["prefix"]), prefix)

    visited = {start_id}
    queue: deque[str] = deque([start_id])
    tables: dict[str, str | None] = {}  # hub -> the route table it was entered through
    while queue:
        gateway = queue.popleft()
        for attachment in sorted(by_gateway.get(gateway, ()), key=lambda a: str(a.get("name"))):
            if attachment.get("virtual_network"):
                if _ref(attachment.get("gateway")) != gateway:
                    continue
                entered = tables.get(gateway)
                propagated = {_ref(table) for table in attachment.get("propagates_to_route_tables") or []}
                if entered is None or entered in propagated:
                    keep(_attachment_prefixes(attachment))
                continue
            hub, peer = _ref(attachment.get("gateway")), _ref(attachment.get("peer_gateway"))
            target = peer if hub == gateway else hub if peer == gateway else None
            if not target or target in visited:
                continue
            visited.add(target)
            keep(_attachment_prefixes(attachment))
            if hub == target:
                tables[target] = _ref(attachment.get("associated_route_table"))
            queue.append(target)
    return [found[key] for key in sorted(found)]


def _transport_owner(transport: dict[str, Any]) -> str | None:
    """The customer key of a transport: its account's owner, else its own ``owner`` (a SaaS has no account)."""
    return customer_owner((transport.get("account") or {}).get("owner")) or customer_owner(transport.get("owner"))


def cloud_legs(
    peerings: Iterable[dict[str, Any]] | None, attachments: Iterable[dict[str, Any]] | None
) -> list[CloudLeg]:
    """Every cloud leg of the model: a BGP peering interface that ends a handoff or vpn session circuit.

    ``peerings`` is the cleaned CloudPeeringInterface root (``device`` is its transport), ``attachments`` the cleaned
    CloudGatewayAttachment root. One leg per (peering, session circuit, end of ours on that circuit). A peering with
    static routing, or on no session circuit, is no BGP session and yields nothing.
    """
    attachment_list = list(attachments or [])
    legs: list[CloudLeg] = []
    for peering in peerings or []:
        transport = peering.get("device") or {}
        name, transport_name = peering.get("name"), transport.get("name")
        if not name or not transport_name or peering.get("routing") != "bgp":
            continue
        owner = _transport_owner(transport)
        gateway = peering.get("gateway") or transport.get("gateway")
        scope = peering.get("scope")
        pairs: tuple[Registered, ...] = ()
        if scope in _REGISTERING_SCOPES:
            pairs = tuple((owner, prefix) for prefix in reachable_prefixes(gateway, attachment_list))
        advertised = tuple(p["prefix"] for p in _prefix_dicts(peering.get("advertised_prefixes"))) if owner else ()
        auth = peering.get("auth") or {}
        for circuit in peering.get("capabilities") or []:
            if not isinstance(circuit, dict) or circuit.get("circuit_type") not in _SESSION_TYPES:
                continue
            for end in circuit.get("capabilities") or []:
                end_device = (end.get("device") or {}).get("name")
                if not end.get("name") or not end_device or end_device == transport_name:
                    continue
                legs.append(
                    CloudLeg(
                        peering=name,
                        transport=transport_name,
                        transport_kind=transport.get("typename"),
                        circuit=str(circuit.get("name")),
                        local=(end_device, end["name"]),
                        owner=owner,
                        group=_name(gateway) or name,
                        scope=scope,
                        peer_ip=peering.get("peer_ip") or None,
                        peer_asn=(peering.get("peer_asn") or {}).get("asn"),
                        password=auth.get("password"),
                        registered=pairs,
                        advertised=advertised,
                    )
                )
    return legs


def legs_by_interface(legs: Iterable[CloudLeg], device_name: str) -> dict[str, CloudLeg]:
    """Interface name of ``device_name`` -> the cloud leg it is the end of.

    An interface that ends the sessions of several legs is ambiguous: it is dropped with a WARNING and accepts
    nothing.
    """
    found: dict[str, list[CloudLeg]] = {}
    for leg in legs:
        if leg.local[0] == device_name:
            found.setdefault(leg.local[1], []).append(leg)
    unique: dict[str, CloudLeg] = {}
    for interface, candidates in found.items():
        if len({leg.peering for leg in candidates}) > 1:
            _log.warning(
                "%s: %s is an end of sessions of several cloud peerings (%s); it accepts nothing",
                device_name,
                interface,
                ", ".join(sorted({leg.peering for leg in candidates})),
            )
            continue
        unique[interface] = candidates[0]
    return unique


def graft_cloud_far_ends(device: dict[str, Any], legs: Mapping[str, CloudLeg]) -> None:
    """Give each one-ended peering of the cleaned ``device`` the far end its cloud leg describes, in place.

    The session builder (transforms/helpers/bgp.py) reads a peering as two ``capabilities`` ends and the remote
    process's ASN. A cloud peering lists only our interface, so the peering interface of the leg is added as the remote
    end (address ``peer_ip``, device the transport) and a process of the transport holding ``peer_asn``; a peering with
    no password takes the leg's ``auth``. A missing ``peer_ip`` or ``peer_asn`` makes the session builder skip the
    session with its usual warning. Nothing is added for a peering whose interface is no leg, which the session builder
    then refuses as malformed. The grafted process has no role, so a cloud far end is never "ours" (external).
    """
    device_name = device.get("name")
    for service in device.get("capabilities") or []:
        if service.get("typename") != "ManagedBGP":
            continue
        for peering in service.get("peerings") or []:
            ends = peering.get("capabilities")
            if not isinstance(ends, list) or len(ends) != 1 or (ends[0].get("device") or {}).get("name") != device_name:
                continue
            leg = legs.get(ends[0].get("name") or "")
            if leg is None:
                continue
            ends.append(
                {
                    "name": leg.peering,
                    "typename": PEERING_KIND,
                    "ip_address": leg.peer_ip,
                    "device": {"name": leg.transport, "typename": leg.transport_kind},
                }
            )
            if leg.peer_asn is not None:
                processes = peering.get("bgp_processes")
                peering["bgp_processes"] = [
                    *(processes if isinstance(processes, list) else []),
                    {"local_as": {"asn": leg.peer_asn}, "capabilities": [{"name": leg.transport}]},
                ]
            if leg.password and not peering.get("password"):
                peering["password"] = {"password": leg.password}
