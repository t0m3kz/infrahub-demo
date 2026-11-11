"""Firewall context helpers: the per-vsys/context list of a firewall and where its policies are placed.

Split out of ``transforms.helpers.firewall`` (zones, static routes and exchange transits stay there). The customer
PBR rules that resolve their nexthop through a context live in ``transforms.helpers.firewall_pbr``.
"""

from ipaddress import ip_interface
from typing import Any

from transforms.helpers.addressing import sub_interface_parent
from transforms.helpers.firewall import _snat_host
from transforms.helpers.ha import leg_virtual_ip
from transforms.helpers.policy import is_internet_rule, policy_key, rule_key, segment_rule_policies
from utils.exchange_transit import (
    FIREWALL_CONTEXT_KIND,
    MANAGEMENT_TYPE,
    TRANSIT_SLOT,
    ZONE_BY_NS_TYPE,
    transit_addresses,
    transit_vlan,
)
from utils.firewall_split import RuleShare, footprint_contexts, is_external, rule_share, steering_contexts
from utils.routing_domain import interface_security_zone
from utils.segment_addressing import IPV4, segment_hosting_candidates, segment_prefix
from utils.service_contexts import contexts_with_legs, leg_interfaces


def member_firewall_contexts(shared_capabilities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The full firewall contexts this device is a member of (its shared capabilities of that kind)."""
    return [cap for cap in shared_capabilities or [] if cap.get("typename") == FIREWALL_CONTEXT_KIND]


def _served_footprint(deployment: dict[str, Any], external: bool) -> dict[str, Any]:
    """One footprint a context serves: its segments and the share of its rules the context holds (a context of the
    Internet edge when ``external``), from the footprint's own list of contexts (utils/firewall_split.py)."""
    contexts = footprint_contexts(deployment)
    return {
        "name": deployment.get("name"),
        "segments": [segment for segment in deployment.get("network_segments") or [] if segment.get("id")],
        "share": rule_share(external, contexts),
        "contexts": contexts,
    }


def _terminated_segments(cap: dict[str, Any], footprints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The segments of the footprints this context is the internal one of that the pair gateways itself
    (``firewall_gateway``): the Internet edge context never terminates one."""
    found: dict[str, dict[str, Any]] = {}
    for footprint in footprints:
        if cap.get("id") not in {context.get("id") for context in steering_contexts(footprint["contexts"])}:
            continue
        for segment in footprint["segments"]:
            if segment.get("firewall_gateway"):
                found.setdefault(segment["id"], segment)
    return list(found.values())


def _new_context(cap: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    """The template-facing shape of one ManagedFirewallContext capability."""
    external = is_external(cap)
    footprints = [_served_footprint(deployment, external) for deployment in cap.get("served_deployments") or []]
    segments: dict[str, dict[str, Any]] = {}
    for footprint in footprints:
        for segment in footprint["segments"]:
            segments.setdefault(segment["id"], segment)
    return {
        "footprints": footprints,
        "edge_customers": _edge_customers(footprints) if external else [],
        "id": cap.get("id"),
        "name": cap.get("name"),
        "context_id": cap.get("context_id"),
        "vlan_id": cap.get("vlan_id"),
        "tenant_name": (cap.get("tenant") or {}).get("name"),
        "segments": list(segments.values()),
        "terminated_segments": _terminated_segments(cap, footprints),
        "snat": _snat_host(cap),
        "legs": legs,
    }


def get_firewall_contexts(device: dict[str, Any], ha: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Build the per-vsys/context list of a firewall: the firewall contexts it is a member of, with their legs.

    The legs of a context are the device's sub-interfaces ``<uplink>.<transit VLAN>``
    (utils/service_contexts.py ``context_legs``) that generators/topology/customer_dc.py created for it on the
    "uplink"-role trunk. A colocation context has no legs at all: it lists with its policies and no legs.

    ``legs`` are the context's exchange transit legs on this device: the
    sub-interfaces whose address is in a VRF namespace of a known type
    (utils/exchange_transit.py ZONE_BY_NS_TYPE), ordered prod, non_prod,
    internet, management. A leg is {interface, parent_interface, vlan (the
    transit VLAN), own_ip, virtual_ip (the HA pair's VIP, None
    without ``ha``), anycast / vip (the border leaves' gateway
    and the firewall VIP, plain IPs of the /29), vrf, ns_type, zone, nameif}. A context holding none renders only its
    policies.

    ``segments`` are the segments terminating on the context: every segment
    of a deployment it serves (served_deployments, written by each
    deployment's own boarding run), de-duplicated by id. ``terminated_segments`` are the ones among them the pair
    gateways itself (``ManagedVxlanSegment.firewall_gateway`` of a segment of the footprint this is the internal context of;
    transforms/helpers/firewall_terminated.py).
    """
    contexts: dict[str, dict[str, Any]] = {}
    legs_by_context: dict[str, list[dict[str, Any]]] = {}
    for cap in member_firewall_contexts(device.get("shared_capabilities")):
        if cap.get("id"):
            contexts[cap["id"]] = _new_context(cap, legs_by_context.setdefault(cap["id"], []))
    for context_id, ifaces in leg_interfaces(device).items():
        context = contexts.get(context_id)
        if context is None:
            continue
        for iface in ifaces:
            ip_obj = iface.get("ip_address") or {}
            namespace = interface_security_zone(iface)
            ns_type = namespace.get("vrf_type")
            address = ip_obj.get("address")
            if ns_type not in ZONE_BY_NS_TYPE or not address or not context.get("vlan_id"):
                continue
            network = ip_interface(address).network
            # IPv4 by design: a context leg is a /29 of the IPv4 transit pools; an IPv6 interface is no leg.
            if network.version != 4:
                continue
            zone = ZONE_BY_NS_TYPE[ns_type]
            addresses = transit_addresses(str(network))
            legs_by_context[context_id].append(
                {
                    "interface": iface.get("name"),
                    "parent_interface": sub_interface_parent(iface) or {},
                    "vlan": transit_vlan(context["vlan_id"], ns_type),
                    "own_ip": address,
                    **leg_virtual_ip(ha, address),
                    "anycast": addresses["anycast"],
                    "vip": addresses["vip"],
                    "vrf": namespace.get("name"),
                    "ns_type": ns_type,
                    "zone": zone,
                    "nameif": zone.removesuffix("-ZONE").lower(),
                }
            )
    for legs in legs_by_context.values():
        legs.sort(key=lambda leg: TRANSIT_SLOT[leg["ns_type"]])
    return sorted(contexts.values(), key=lambda c: c.get("name") or "")


def firewall_context_zone_data(
    contexts: list[dict[str, Any]], management_zones: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Context/endpoint zones, plus the explicitly queried infrastructure management zone."""
    zones: dict[str, dict[str, Any]] = {}

    def add_zone(zone: dict[str, Any]) -> dict[str, Any] | None:
        zone_name = zone.get("name")
        if not zone_name:
            return None
        found = zones.setdefault(zone_name, {"name": zone_name, "network_segments": []})
        for field in ("trust_level", "description"):
            if zone.get(field) is not None:
                found[field] = zone[field]
        return found

    def add_segment(segment: dict[str, Any]) -> None:
        zone = add_zone(segment.get("security_zone") or {})
        if zone is not None:
            members = (segment.get("security_zone") or {}).get("network_segments")
            for member in members if members is not None else [segment]:
                if member not in zone["network_segments"]:
                    zone["network_segments"].append(member)

    for context in contexts:
        for segment in context.get("segments") or []:
            add_segment(segment)
        for segment in context.get("terminated_segments") or []:
            add_segment(segment)
        for policy in _context_policies(context):
            for rule in policy["rules"]:
                for end in ("source", "destination"):
                    add_segment(rule.get(f"{end}_segment") or {})
    for management in management_zones or []:
        if management.get("name") == ZONE_BY_NS_TYPE[MANAGEMENT_TYPE]:
            zone = add_zone(management)
            if zone is not None:
                zone["network_segments"].extend(management.get("network_segments") or [])
    return list(zones.values())


def _edge_customers(footprints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The footprints an Internet edge context serves, by name, each with its prefixes and its public address.

    A footprint whose internal context is a dedicated one (``tenant`` set) is translated to that context's
    ``snat_address`` here; a footprint on a shared context (or with no internal context) has no address of its own
    and takes the edge's.
    """
    customers = []
    for footprint in sorted(footprints, key=lambda f: str(f.get("name"))):
        dedicated = next(
            (
                context
                for context in footprint["contexts"]
                if not is_external(context) and (context.get("tenant") or {}).get("id")
            ),
            None,
        )
        customers.append(
            {
                "name": footprint.get("name"),
                # The translated sources: NAT is IPv4 only by design (an IPv6 source is not translated, AGENTS.md).
                "prefixes": sorted(
                    {prefix for segment in footprint["segments"] if (prefix := segment_prefix(segment, IPV4))}
                ),
                "address": _snat_host(dedicated) if dedicated else None,
            }
        )
    return customers


def _zone_member_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every segment in a zone one of `segments` (those on this firewall's
    interfaces) belongs to: the firewall routes that zone's CIDRs through the
    leg (get_firewall_static_routes), so it is where their rules hold."""
    return [
        member for segment in segments for member in (segment.get("security_zone") or {}).get("network_segments") or []
    ]


def _context_policies(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The policies of one context's table: its segments' rules, each footprint's share of them.

    Each served footprint places on this context the share of its rules its list of contexts gives it
    (utils/firewall_split.py ``rule_share``): on an Internet edge context only the rules that cross the internet (to
    it or from it, ``is_internet_rule``) of a footprint that also has an internal context, on an internal context the
    rest of them; a footprint with one kind of context places every rule on it. The table is the union of the shares.
    """
    policies = segment_rule_policies(context.get("segments") or [])
    footprints = context.get("footprints") or []
    if all(footprint["share"] is RuleShare.ALL for footprint in footprints):
        return policies
    held = {
        rule_key(policy_key(policy), rule)
        for footprint in footprints
        for policy in segment_rule_policies(footprint["segments"])
        for rule in policy["rules"]
        if footprint["share"].holds(is_internet_rule(rule))
    }
    split = [
        {**policy, "rules": [rule for rule in policy["rules"] if rule_key(policy_key(policy), rule) in held]}
        for policy in policies
    ]
    return [policy for policy in split if policy["rules"]]


def place_policies_in_contexts(
    contexts: list[dict[str, Any]],
    segments: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """This firewall's policies: the rules of the segments it serves, per table.

    Each context's table holds its segments' own policies (their egress
    rules) and the rules into them (the ingress leg: traffic between two
    tenants crosses both contexts and each one denies by default), see
    segment_rule_policies. The root table holds the rules of the segments
    the firewall carries on its interfaces (``segments``) that no context
    already holds; a firewall with no contexts also takes the rules of every
    segment in a zone bound on one of its interfaces. A rule of a segment
    served nowhere here is on the firewall serving it, not on this one.

    Returns (root policies, context id -> policies), each policy holding
    only the rules placed in that table.
    """
    by_context = {ctx["id"]: _context_policies(ctx) for ctx in contexts}
    placed = {
        str(rule.get("id"))
        for policies in by_context.values()
        for policy in policies
        for rule in policy["rules"]
        if rule.get("id")
    }
    own = list(segments or [])
    if not contexts:
        own += _zone_member_segments(own)
    return segment_rule_policies(own, exclude=placed), by_context


def _flatten_deployment_firewall_contexts(deployment: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten the device-scoped FirewallContext traversal into the flat
    list get_customer_pbr_rules() expects.

    `deployment` is this device's own `deployment` field (already cleaned),
    shaped by queries/fragments/firewall_contexts.gql's
    SwitchDeploymentFields fragment: deployment.devices
    (pre-filtered to the firewall roles) -> each device's shared capabilities
    of typename ManagedFirewallContext (the contexts it is a member of), each with the ``legs`` its members hold
    (utils/service_contexts.py contexts_with_legs).
    Pod-tier devices (leaf/tor/access-leaf) have their deployment set to the
    POD, not the DC — deployment_id=self.data["deployment"]["id"] in
    generators/topology/rack.py vs deployment_id=dc_id in
    generators/topology/dc.py — so the fragment also nests one `parent` hop
    for that case; both paths are flattened here, deduped by context id.
    """
    if not deployment:
        return []

    members: dict[str, dict[str, Any]] = {}
    for hosting in segment_hosting_candidates(deployment):
        for device in hosting.get("devices") or []:
            if isinstance(device, dict):
                members.setdefault(str(device.get("name")), device)
    contexts = [
        ctx for ctx in contexts_with_legs(list(members.values())) if ctx.get("typename") == FIREWALL_CONTEXT_KIND
    ]
    roles: dict[str, list[dict[str, Any]]] = {}
    for member in members.values():
        for cap in member_firewall_contexts(member.get("shared_capabilities")):
            roles.setdefault(str(cap.get("id")), []).append({"role": member.get("role")})
    for ctx in contexts:
        ctx["shared_devices"] = roles.get(str(ctx.get("id")), [])
    return _steering_only(contexts)


def _steering_only(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The contexts as the leaf PBR steers to them: each footprint to its internal context, else to its external one
    (utils/firewall_split.py ``steering_contexts``, per footprint).

    An internal context is kept as it is. An external (Internet edge) context keeps only the footprints it is the
    steering context of (they have no internal context here), under ``served_deployments``, and goes when none is left:
    the edge is then no PBR next hop of any segment.
    """
    by_footprint: dict[str, list[dict[str, Any]]] = {}
    for ctx in contexts:
        for footprint in ctx.get("served_deployments") or []:
            if footprint.get("id"):
                by_footprint.setdefault(str(footprint["id"]), []).append(ctx)
    steered: dict[str, set[str]] = {}
    for footprint_id, served_by in by_footprint.items():
        for ctx in steering_contexts(served_by):
            steered.setdefault(str(ctx.get("id")), set()).add(footprint_id)
    kept: list[dict[str, Any]] = []
    for ctx in contexts:
        if not is_external(ctx):
            kept.append(ctx)
            continue
        own = steered.get(str(ctx.get("id")), set())
        served = [footprint for footprint in ctx.get("served_deployments") or [] if footprint.get("id") in own]
        if served:
            kept.append({**ctx, "served_deployments": served})
    return kept
