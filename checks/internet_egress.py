"""Traffic to the internet always goes through a firewall of ours, and the hardware is there to enforce it.

A ``SecurityPolicyRule`` whose ``destination_zone`` is INTERNET-ZONE names the internet (checks/segment_security.py
keeps the zone the only one a rule may name). A permit rule is only an intent: nothing enforces it unless the
firewall pair, its legs, the exchange to the INTERNET VRF, the border ports and the SNAT address are in place. So the
check fails closed: every ENABLED internet rule is followed from its source segment to each on-prem footprint of that
segment (``network_segments`` of a ``TopologyCustomerDC`` or ``TopologyCustomerColocation``), and a missing element is
an ERROR that blocks the proposed change. One finding per rule, footprint, context and missing element.

A DC footprint (``serving_firewall_context``), and each firewall context of a colocation footprint
(``serving_firewall_contexts``), needs:

1. a cluster (``ManagedFirewallHA``) of exactly two member devices, both with role ``firewall``;
2. for every member, an INTERNET leg: a sub-interface of the context named ``<port>.<vlan>`` (the transit VLAN of the
   INTERNET slot, utils/exchange_transit.py) with an address in the INTERNET namespace;
3. a ``TopologyRoutedExchange`` of the context between the tenant VRF and INTERNET, with every INTERNET leg linked to
   it (generators/firewall_context.py names it ``{context}-{A}-{Z}`` and links the legs);
4. the border service ports tagged with the context: one ``DcimPhysicalInterface`` of role ``firewall`` on a border or
   edge device per member (the port the member's uplink is cabled to);
5. a ``snat_address``.

A colocation footprint names no zone: it is served by the context of every zone of its metro that has a physical
firewall (generators/topology/customer_colocation.py). Each such zone must pass the elements above (a zone with no
firewall device is ignored), and the footprint is served when at least one zone passed OR a
``TopologyVirtualColocation`` of the metro has a complete provider-hosted pair: a ``ManagedFirewallHA`` whose members
are exactly two ``CloudNetworkAppliance`` with role ``firewall`` and status ``active`` (no legs, ports, SNAT or
contexts apply to it, the provider runs them). A metro with neither is an ERROR.

Deliberate gap: only the on-prem footprints are checked. A cloud footprint (``TopologyCustomerCloud``) and an office
(``TopologyCustomerOffice``) are skipped silently: egress from the cloud leaves through the provider's own gateways,
which are outside our firewalls, and an office reaches the internet through its SD-WAN edge.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from infrahub_sdk.checks import InfrahubCheck

from transforms.helpers.policy import rule_enabled
from utils.data_cleaning import clean_data
from utils.exchange_transit import INTERNET_TYPE, INTERNET_ZONE, transit_vlan
from utils.fabric_roles import CARRIER_ROLES

DC_KIND = "TopologyCustomerDC"
COLOCATION_KIND = "TopologyCustomerColocation"
FOOTPRINT_KINDS = (DC_KIND, COLOCATION_KIND)
"""The on-prem footprint kinds; every other kind (cloud, office) is outside our firewalls and is not checked."""

FIREWALL_ROLE = "firewall"
APPLIANCE_KIND = "CloudNetworkAppliance"
PHYSICAL_DEVICE_KIND = "DcimPhysicalDevice"
PHYSICAL_PORT_KIND = "DcimPhysicalInterface"
VIRTUAL_PORT_KIND = "DcimVirtualInterface"
EXCHANGE_KIND = "TopologyRoutedExchange"
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


def _members(cluster: dict[str, Any]) -> list[dict[str, Any]]:
    """The member devices of a cluster, by name."""
    return sorted(
        (member for member in cluster.get("capabilities") or [] if isinstance(member, dict)),
        key=_name,
    )


def _legs(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The context's sub-interfaces (the legs of its members)."""
    return [
        interface
        for interface in context.get("capabilities") or []
        if isinstance(interface, dict) and interface.get("typename") == VIRTUAL_PORT_KIND
    ]


def _internet_exchange(context: dict[str, Any], exchanges: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The context's exchange with the INTERNET namespace on one side, None when it has none."""
    for exchange in sorted(exchanges, key=_name):
        if _id(exchange.get("gateway")) != context.get("id"):
            continue
        sides = (exchange.get("namespace_a"), exchange.get("namespace_z"))
        if any(isinstance(side, dict) and side.get("namespace_type") == INTERNET_TYPE for side in sides):
            return exchange
    return None


def _leg_gaps(
    member: dict[str, Any], legs: list[dict[str, Any]], vlan: int | None, exchange: dict[str, Any] | None
) -> list[str]:
    """What the INTERNET leg of one member lacks: the leg itself, its address in INTERNET, its link to the exchange.

    The leg is the member's sub-interface that carries the INTERNET transit VLAN or already has an address in
    INTERNET; an address in another namespace is reported, not skipped.
    """
    member_name = _name(member)
    suffix = f".{transit_vlan(vlan, INTERNET_TYPE)}" if vlan else None
    candidates = sorted(
        (
            leg
            for leg in legs
            if _id(leg.get("device")) == member.get("id")
            and (
                (suffix is not None and str(leg.get("name")).endswith(suffix))
                or ((leg.get("ip_address") or {}).get("ip_namespace") or {}).get("namespace_type") == INTERNET_TYPE
            )
        ),
        key=_name,
    )
    if not candidates:
        wanted = f"<port>{suffix}" if suffix else "<port>.<vlan>"
        return [f"member '{member_name}' has no INTERNET leg (sub-interface {wanted} of the context)"]
    leg = candidates[0]
    leg_name = f"{member_name}:{_name(leg)}"
    address = leg.get("ip_address")
    if not isinstance(address, dict) or not address.get("address"):
        return [f"INTERNET leg '{leg_name}' has no address"]
    namespace = address.get("ip_namespace") or {}
    if namespace.get("namespace_type") != INTERNET_TYPE:
        return [
            f"INTERNET leg '{leg_name}' is addressed in namespace '{namespace.get('name')}', not in the INTERNET namespace"
        ]
    if exchange is not None and not any(
        isinstance(cap, dict) and cap.get("typename") == EXCHANGE_KIND and cap.get("id") == exchange.get("id")
        for cap in leg.get("capabilities") or []
    ):
        return [f"INTERNET leg '{leg_name}' is not linked to the exchange '{_name(exchange)}'"]
    return []


def context_gaps(context: dict[str, Any], exchanges: list[dict[str, Any]]) -> list[Gap]:
    """What a firewall context lacks to enforce the internet egress, one gap per missing element."""
    label = _name(context)
    cluster = context.get("cluster")
    cluster = cluster if isinstance(cluster, dict) else {}
    members = _members(cluster)
    firewalls = [member for member in members if member.get("role") == FIREWALL_ROLE]
    gaps: list[str] = []

    if not cluster:
        gaps.append("no cluster (ManagedFirewallHA)")
    elif len(members) != PAIR_SIZE or len(firewalls) != PAIR_SIZE:
        gaps.append(
            f"cluster '{_name(cluster)}' has {len(members)} member device(s), {len(firewalls)} with role "
            f"{FIREWALL_ROLE}: a complete pair is exactly {PAIR_SIZE} firewalls"
        )

    vlan = context.get("vlan_id")
    if not vlan:
        gaps.append("no vlan_id (the transit VLAN of its legs is not allocated)")
    exchange = _internet_exchange(context, exchanges)
    if exchange is None:
        gaps.append("no TopologyRoutedExchange between the tenant VRF and INTERNET")
    legs = _legs(context)
    for member in firewalls:
        gaps.extend(_leg_gaps(member, legs, vlan, exchange))

    ports = {
        _id(interface)
        for interface in context.get("capabilities") or []
        if isinstance(interface, dict)
        and interface.get("typename") == PHYSICAL_PORT_KIND
        and interface.get("role") == FIREWALL_ROLE
        and (interface.get("device") or {}).get("role") in CARRIER_ROLES
    }
    if len(ports) < PAIR_SIZE:
        gaps.append(
            f"{len(ports)} border service port(s) tagged with the context, one {FIREWALL_ROLE}-role port of a "
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
        members = _members(domain)
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
    cluster = context.get("cluster")
    return {str(member["id"]) for member in _members(cluster if isinstance(cluster, dict) else {}) if member.get("id")}


def colocation_gaps(footprint: dict[str, Any], cleaned: dict[str, Any]) -> list[Gap]:
    """What a colocation footprint lacks: its zones with firewalls must pass, and something must serve it."""
    metro_id = _id(footprint.get("parent"))
    metro = _name(footprint.get("parent"))
    contexts = [context for context in footprint.get("serving_firewall_contexts") or [] if isinstance(context, dict)]
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


def footprint_gaps(kind: str, footprint: dict[str, Any], cleaned: dict[str, Any]) -> list[Gap]:
    """What an on-prem footprint lacks to send its segments' internet traffic through our firewalls."""
    if kind == COLOCATION_KIND:
        return colocation_gaps(footprint, cleaned)
    context = footprint.get("serving_firewall_context")
    if not isinstance(context, dict):
        return [Gap(None, "no serving firewall context (serving_firewall_context is unset)")]
    contexts = [context]
    exchanges = cleaned.get(EXCHANGE_KIND) or []
    return [gap for context in sorted(contexts, key=_name) for gap in context_gaps(context, exchanges)]


def internet_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The enabled rules that name the internet and a source segment, by name."""
    return sorted(
        (
            rule
            for rule in rules
            if rule_enabled(rule)
            and (rule.get("destination_zone") or {}).get("name") == INTERNET_ZONE
            and _id(rule.get("source_segment"))
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
