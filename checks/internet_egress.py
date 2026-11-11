"""Traffic to the internet always goes through a firewall of ours, and the hardware is there to enforce it.

A ``SecurityPolicyRule`` whose ``destination_zone`` is INTERNET names the internet (checks/segment_security.py
keeps the zone the only one a rule may name). A permit rule is only an intent: nothing enforces it unless the
firewall pair, its legs, the exchange to the INTERNET VRF, the border ports and the SNAT address are in place. So the
check fails closed: every ENABLED internet rule is followed from its source segment to each on-prem footprint of that
segment (``network_segments`` of a ``TopologyCustomerDC`` or ``TopologyCustomerColocation``), and a missing element is
an ERROR that blocks the proposed change. One finding per rule, footprint, context and missing element.

Every firewall context a footprint lists (``serving_firewall_contexts``, internal and Internet edge alike) that holds
its internet rules (utils/firewall_split.py: the Internet edge context when the footprint lists one, else its
contexts) needs:

1. exactly two member devices (``shared_devices``), both with role ``internal-firewall`` (``external-firewall`` for
   the Internet edge context);
2. for every member, an INTERNET leg: a sub-interface of the context tagged with the encapsulation ``vlan_id`` (the transit VLAN of the
   INTERNET slot, utils/exchange_transit.py) with an address in the INTERNET namespace;
3. a ``TopologyRoutedExchange`` of the context between the tenant VRF and INTERNET, with every INTERNET leg in its
   INTERNET VRF (generators/firewall_context.py names it ``{context}-{A}-{Z}``; its legs are derived,
   utils/service_contexts.py ``exchange_legs``);
4. the border service ports behind the context: one ``DcimPhysicalInterface`` of role ``firewall`` on a border or
   edge device per member (the port the member's uplink is cabled to, utils/service_contexts.py);
5. a ``snat_address``.

A colocation footprint names no zone: it is served by the context of every zone of its metro that has a physical
firewall (generators/topology/customer_colocation.py). Each such zone must pass the elements above (a zone with no
firewall device is ignored), and the footprint is served when at least one zone passed OR a
``TopologyVirtualColocation`` of the metro has a complete provider-hosted pair: a ``ManagedFirewallHA`` whose members
are exactly two ``CloudNetworkAppliance`` with role ``firewall`` and status ``active`` (no legs, ports, SNAT or
contexts apply to it, the provider runs them). A metro with neither is an ERROR.

A footprint that lists an Internet edge context (a context on an external-firewall pair) is checked against THAT
context (the elements above): it must list exactly one, for its environment, and a dedicated internal context of it
(``tenant`` set) needs only a ``snat_address`` (the public address its traffic is translated to on the edge). A
footprint that lists none keeps the elements above on its serving contexts.

Deliberate gap: only the on-prem footprints are checked. A cloud footprint (``TopologyCustomerCloud``) and an office
(``TopologyCustomerOffice``) are skipped silently: egress from the cloud leaves through the provider's own gateways,
which are outside our firewalls, and an office reaches the internet through its SD-WAN edge.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from infrahub_sdk.checks import InfrahubCheck

from transforms.helpers.policy import rule_enabled
from utils.data_cleaning import clean_data
from utils.exchange_transit import INTERNET_TYPE, INTERNET_ZONE, namespace_type_for_environment, transit_vlan
from utils.fabric_roles import CARRIER_ROLES, EXTERNAL_FIREWALL_ROLE, INTERNAL_FIREWALL_ROLE
from utils.firewall_split import footprint_contexts, internet_contexts, is_external
from utils.interface_encapsulation import vlan_of
from utils.routing_domain import interface_security_zone
from utils.service_contexts import exchange_legs, leg_interfaces, member_border_ports

DC_KIND = "TopologyCustomerDC"
COLOCATION_KIND = "TopologyCustomerColocation"
FOOTPRINT_KINDS = (DC_KIND, COLOCATION_KIND)
"""The on-prem footprint kinds; every other kind (cloud, office) is outside our firewalls and is not checked."""

FIREWALL_ROLE = INTERNAL_FIREWALL_ROLE
SERVICE_PORT_ROLE = "firewall"
"""The role of a border port facing a firewall member (an INTERFACE role, not a device role)."""
APPLIANCE_KIND = "CloudNetworkAppliance"
PHYSICAL_DEVICE_KIND = "DcimPhysicalDevice"
PHYSICAL_PORT_KIND = "DcimPhysicalInterface"
EXCHANGE_KIND = "TopologyRoutedExchange"
"""The query root of the exchanges (queries/validation/internet_egress.gql)."""
ACTIVE = "active"
PAIR_SIZE = 2
"""A firewall cluster is exactly a pair: two members, and one border service port per member."""


class Gap(NamedTuple):
    """One missing element: the context it belongs to (None when it is not a context's) and what is missing."""

    context: str | None
    missing: str


def _name(node: dict[str, Any] | str | None) -> str:
    """The name of a cleaned node, else its id (a bare id string is what an id-only selection collapses to)."""
    if isinstance(node, dict):
        return str(node.get("name") or node.get("id") or "<unnamed>")
    return str(node or "<unnamed>")


def _id(node: dict[str, Any] | str | None) -> str | None:
    """The id of a cleaned relationship: a dict with an ``id`` or the bare id string."""
    if isinstance(node, dict):
        node = node.get("id")
    return str(node) if node else None


def _members(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The member devices of a context (its ``shared_devices``), by name."""
    return sorted(
        (member for member in context.get("shared_devices") or [] if isinstance(member, dict)),
        key=_name,
    )


def _ha_members(domain: dict[str, Any]) -> list[dict[str, Any]]:
    """The member devices of an HA domain (its ``capabilities``), by name."""
    return sorted(
        (member for member in domain.get("capabilities") or [] if isinstance(member, dict)),
        key=_name,
    )


def _internet_exchange(context: dict[str, Any], exchanges: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The context's exchange with the INTERNET namespace on one side, None when it has none."""
    for exchange in sorted(exchanges, key=_name):
        if _id(exchange.get("gateway")) != context.get("id"):
            continue
        sides = tuple((exchange.get(f"vrf_{side}") or {}).get("security_zone") for side in ("a", "z"))
        if any(isinstance(side, dict) and side.get("vrf_type") == INTERNET_TYPE for side in sides):
            return exchange
    return None


def _leg_gaps(
    member: dict[str, Any], legs: list[dict[str, Any]], vlan: int | None, exchange: dict[str, Any] | None
) -> list[str]:
    """What the INTERNET leg of one member lacks: the leg itself, its address in INTERNET, being a leg of the exchange.

    ``legs`` are the member's legs of the context (utils/service_contexts.py ``leg_interfaces``). The leg is the one
    that carries the INTERNET transit VLAN or already has an address in INTERNET; an address in another namespace is
    reported, not skipped.
    """
    member_name = _name(member)
    wanted_vlan = transit_vlan(vlan, INTERNET_TYPE) if vlan else None
    candidates = sorted(
        (
            leg
            for leg in legs
            if (wanted_vlan is not None and vlan_of(leg) == wanted_vlan)
            or interface_security_zone(leg).get("vrf_type") == INTERNET_TYPE
        ),
        key=_name,
    )
    if not candidates:
        wanted = f"VLAN {wanted_vlan}" if wanted_vlan is not None else "an INTERNET VLAN"
        return [f"member '{member_name}' has no INTERNET leg (sub-interface with {wanted} of the context)"]
    leg = candidates[0]
    leg_name = f"{member_name}:{_name(leg)}"
    address = leg.get("ip_address")
    if not isinstance(address, dict) or not address.get("address"):
        return [f"INTERNET leg '{leg_name}' has no address"]
    namespace = interface_security_zone(leg)
    if namespace.get("vrf_type") != INTERNET_TYPE:
        return [
            f"INTERNET leg '{leg_name}' is addressed in namespace '{namespace.get('name')}', not in the INTERNET namespace"
        ]
    if exchange is not None and leg.get("id") not in {
        other.get("id") for other in exchange_legs(member, _id(exchange.get("gateway")) or "", exchange)
    }:
        return [f"INTERNET leg '{leg_name}' is not in a VRF of the exchange '{_name(exchange)}'"]
    return []


def context_gaps(
    context: dict[str, Any], exchanges: list[dict[str, Any]], member_role: str = FIREWALL_ROLE
) -> list[Gap]:
    """What a firewall context lacks to enforce the internet egress, one gap per missing element.

    ``member_role`` is the device role of the pair: ``firewall`` for a serving context, ``external-firewall`` for the
    context a topology names as its Internet edge.
    """
    label = _name(context)
    members = _members(context)
    firewalls = [member for member in members if member.get("role") == member_role]
    gaps: list[str] = []

    if not members:
        gaps.append("no member firewalls (shared_devices)")
    elif len(members) != PAIR_SIZE or len(firewalls) != PAIR_SIZE:
        gaps.append(
            f"context '{label}' has {len(members)} member device(s), {len(firewalls)} with role "
            f"{member_role}: a complete pair is exactly {PAIR_SIZE} firewalls"
        )

    vlan = context.get("vlan_id")
    if not vlan:
        gaps.append("no vlan_id (the transit VLAN of its legs is not allocated)")
    exchange = _internet_exchange(context, exchanges)
    if exchange is None:
        gaps.append("no TopologyRoutedExchange between the tenant VRF and INTERNET")
    for member in firewalls:
        gaps.extend(_leg_gaps(member, leg_interfaces(member).get(str(context.get("id")), []), vlan, exchange))

    ports = {
        _id(interface)
        for member in members
        for interface in member_border_ports(member)
        if interface.get("typename") == PHYSICAL_PORT_KIND
        and interface.get("role") == SERVICE_PORT_ROLE
        and (interface.get("device") or {}).get("role") in CARRIER_ROLES
    }
    if len(ports) < PAIR_SIZE:
        gaps.append(
            f"{len(ports)} border service port(s) tagged with the context, one {SERVICE_PORT_ROLE}-role port of a "
            f"border or edge device per member ({PAIR_SIZE}) is required"
        )

    if not _id(context.get("snat_address")):
        gaps.append("no snat_address")
    return [Gap(label, missing) for missing in gaps]


def virtual_pair_gaps(colocation: dict[str, Any]) -> list[str]:
    """Why a virtual colocation has no complete firewall pair; empty when it has one.

    A pair is a ManagedFirewallHA whose members are exactly two CloudNetworkAppliance with role firewall and status
    active. The HA domains are found through the presence's appliances.
    """
    domains: dict[str, dict[str, Any]] = {}
    for appliance in colocation.get("appliances") or []:
        for capability in appliance.get("capabilities") or []:
            if isinstance(capability, dict) and capability.get("typename") == "ManagedFirewallHA":
                domains.setdefault(str(capability.get("id")), capability)
    if not domains:
        return ["it hosts no firewall HA domain (ManagedFirewallHA)"]
    problems: list[str] = []
    for domain in sorted(domains.values(), key=_name):
        members = _ha_members(domain)
        complete = len(members) == PAIR_SIZE and all(
            member.get("typename") == APPLIANCE_KIND
            and member.get("role") == FIREWALL_ROLE
            and member.get("status") == ACTIVE
            for member in members
        )
        if complete:
            return []
        problems.append(
            f"firewall HA '{_name(domain)}' has {len(members)} member(s), a complete pair is exactly {PAIR_SIZE} "
            f"{APPLIANCE_KIND} with role {FIREWALL_ROLE} and status {ACTIVE}"
        )
    return problems


def _zone_firewall_ids(zone: dict[str, Any]) -> set[str]:
    """The ids of the physical firewall devices of a zone (a dedicated virtual pair is hosted on them)."""
    return {
        str(device["id"])
        for device in zone.get("devices") or []
        if isinstance(device, dict) and device.get("typename") == PHYSICAL_DEVICE_KIND and device.get("id")
    }


def _context_member_ids(context: dict[str, Any]) -> set[str]:
    return {str(member["id"]) for member in _members(context) if member.get("id")}


def colocation_gaps(footprint: dict[str, Any], cleaned: dict[str, Any]) -> list[Gap]:
    """What a colocation footprint lacks: its zones with firewalls must pass, and something must serve it."""
    metro_id = _id(footprint.get("parent"))
    metro = _name(footprint.get("parent"))
    contexts = footprint_contexts(footprint)
    exchanges = cleaned.get(EXCHANGE_KIND) or []
    gaps: list[Gap] = []
    passed = False
    zones = [
        (zone, ids)
        for zone in sorted(cleaned.get("TopologyColocationZone") or [], key=_name)
        if _id(zone.get("parent")) == metro_id and (ids := _zone_firewall_ids(zone))
    ]
    for zone, firewall_ids in zones:
        serving = [context for context in contexts if _context_member_ids(context) & firewall_ids]
        zone_gaps: list[Gap] = []
        if not serving:
            zone_gaps.append(
                Gap(None, f"zone '{_name(zone)}' has a firewall pair but no serving context of the footprint on it")
            )
        for context in sorted(serving, key=_name):
            zone_gaps.extend(
                Gap(gap.context, f"zone '{_name(zone)}': {gap.missing}") for gap in context_gaps(context, exchanges)
            )
        passed = passed or not zone_gaps
        gaps.extend(zone_gaps)

    colocations = [
        colocation
        for colocation in sorted(cleaned.get("TopologyVirtualColocation") or [], key=_name)
        if _id(colocation.get("parent")) == metro_id
    ]
    virtual_problems = {_name(colocation): virtual_pair_gaps(colocation) for colocation in colocations}
    if passed or any(not problems for problems in virtual_problems.values()):
        return gaps
    if not zones:
        reasons = "; ".join(
            f"virtual colocation '{name}': {'; '.join(problems)}" for name, problems in virtual_problems.items()
        )
        gaps.append(
            Gap(
                None,
                f"no firewall serves the footprint: metro '{metro}' has no zone with a firewall pair and no virtual "
                f"colocation with a complete pair" + (f" ({reasons})" if reasons else ""),
            )
        )
    return gaps


def _tenant_type(context: dict[str, Any], exchanges: list[dict[str, Any]]) -> str | None:
    """The tenant namespace type of a context: the side of its INTERNET exchange that is not INTERNET."""
    exchange = _internet_exchange(context, exchanges)
    for side in (((exchange or {}).get(f"vrf_{end}") or {}).get("security_zone") for end in ("a", "z")):
        if isinstance(side, dict) and side.get("vrf_type") not in (None, INTERNET_TYPE):
            return str(side["vrf_type"])
    return None


def _edge_gaps(footprint: dict[str, Any], contexts: list[dict[str, Any]], exchanges: list[dict[str, Any]]) -> list[Gap]:
    """What the footprint's Internet edge lacks: one edge context of its environment, complete, and the public address
    of a dedicated internal context (the address its traffic is translated to on the edge)."""
    edges = sorted((context for context in contexts if is_external(context)), key=_name)
    if len(edges) > 1:
        names = ", ".join(_name(context) for context in edges)
        return [Gap(None, f"it lists {len(edges)} Internet edge contexts ({names}): one per footprint")]
    (edge,) = edges
    gaps = [
        Gap(gap.context, f"egress context: {gap.missing}")
        for gap in context_gaps(edge, exchanges, EXTERNAL_FIREWALL_ROLE)
    ]
    wanted = namespace_type_for_environment(str(footprint.get("environment") or ""))
    tenant_type = _tenant_type(edge, exchanges)
    if footprint.get("environment") and tenant_type is not None and tenant_type != wanted:
        gaps.append(Gap(_name(edge), f"the Internet edge context is for {tenant_type}, the footprint is {wanted}"))
    gaps.extend(
        Gap(_name(context), "no snat_address (the public address its traffic is translated to on the egress)")
        for context in sorted(contexts, key=_name)
        if not is_external(context) and _id(context.get("tenant")) and not _id(context.get("snat_address"))
    )
    return gaps


def footprint_gaps(kind: str, footprint: dict[str, Any], cleaned: dict[str, Any]) -> list[Gap]:
    """What an on-prem footprint lacks to send its segments' internet traffic through our firewalls.

    The contexts that hold the footprint's internet rules are its Internet edge context when it lists one, else its
    serving contexts (utils/firewall_split.py ``internet_contexts``)."""
    exchanges = cleaned.get(EXCHANGE_KIND) or []
    contexts = footprint_contexts(footprint)
    holders = internet_contexts(contexts)
    if any(is_external(context) for context in holders):
        return _edge_gaps(footprint, contexts, exchanges)
    if kind == COLOCATION_KIND:
        return colocation_gaps(footprint, cleaned)
    if not holders:
        return [Gap(None, "no serving firewall context (serving_firewall_contexts is empty)")]
    return [gap for context in sorted(holders, key=_name) for gap in context_gaps(context, exchanges)]


def _ip_version(segment: dict[str, Any] | str) -> str:
    """The segment's ``ip_version`` (``ipv4`` when the query row is only an id)."""
    return (segment.get("ip_version") if isinstance(segment, dict) else None) or "ipv4"


def internet_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The enabled rules that name the internet and a source segment, by name."""
    return sorted(
        (
            rule
            for rule in rules
            if rule_enabled(rule)
            and (rule.get("destination_zone") or {}).get("name") == INTERNET_ZONE
            and _id(rule.get("source_segment"))
            # An IPv6 source is not translated (accepted gap, like cloud and office egress): the check demands no
            # snat_address for it, which is an IPv4 address.
            and _ip_version(rule["source_segment"]) != "ipv6"
        ),
        key=_name,
    )


def egress_findings(cleaned: dict[str, Any]) -> list[str]:
    """One message per rule, on-prem footprint of its source segment, context and missing element."""
    footprints: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for kind in FOOTPRINT_KINDS:
        for footprint in cleaned.get(kind) or []:
            for segment in footprint.get("network_segments") or []:
                if segment_id := _id(segment):
                    footprints.setdefault(segment_id, []).append((kind, footprint))
    gaps_of: dict[str, list[Gap]] = {}
    findings: list[str] = []
    for rule in internet_rules(cleaned.get("SecurityPolicyRule") or []):
        segment = rule["source_segment"]
        for kind, footprint in sorted(footprints.get(str(_id(segment)), []), key=lambda item: _name(item[1])):
            key = str(footprint.get("id"))
            if key not in gaps_of:
                gaps_of[key] = footprint_gaps(kind, footprint, cleaned)
            for gap in gaps_of[key]:
                where = f", context '{gap.context}'" if gap.context else ""
                findings.append(
                    f"rule '{_name(rule)}': internet egress of segment '{_name(segment)}' is not enforced on "
                    f"{kind} '{_name(footprint)}'{where}: {gap.missing}"
                )
    return findings


class CheckInternetEgress(InfrahubCheck):
    """Every enabled internet rule is enforced by firewall hardware of ours on each on-prem footprint of its source
    segment (see the module docstring)."""

    query = "internet_egress"

    def validate(self, data: Any) -> None:
        for message in egress_findings(clean_data(data)):
            self.log_error(message=message)
