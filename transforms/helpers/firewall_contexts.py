"""Firewall context helpers: the per-vsys/context list of a firewall and where its policies are placed.

Split out of ``transforms.helpers.firewall`` (zones, static routes and exchange transits stay there). The customer
PBR rules that resolve their nexthop through a context live in ``transforms.helpers.firewall_pbr``.
"""

from ipaddress import ip_interface
from typing import Any

from transforms.helpers.addressing import sub_interface_parent
from transforms.helpers.firewall import _snat_host
from transforms.helpers.ha import leg_virtual_ip
from transforms.helpers.policy import is_internet_rule, segment_rule_policies
from utils.exchange_transit import (
    FIREWALL_CONTEXT_KIND,
    INTERNET_TYPE,
    MANAGEMENT_TYPE,
    TRANSIT_SLOT,
    ZONE_BY_NS_TYPE,
    namespace_type_for_environment,
    transit_addresses,
    transit_vlan,
)
from utils.routing_domain import interface_security_zone
from utils.segment_addressing import segment_hosting_candidates, segment_prefix
from utils.service_contexts import contexts_with_legs, leg_interfaces


def member_firewall_contexts(shared_capabilities: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The full firewall contexts this device is a member of (its shared capabilities of that kind)."""
    return [cap for cap in shared_capabilities or [] if cap.get("typename") == FIREWALL_CONTEXT_KIND]


def _new_context(cap: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    """The template-facing shape of one ManagedFirewallContext capability."""
    segments: dict[str, dict[str, Any]] = {}
    edge_hosted = False
    for deployment in cap.get("served_deployments") or []:
        for segment in deployment.get("network_segments") or []:
            if segment.get("id"):
                segments.setdefault(segment["id"], segment)
        # A served footprint whose DC names an Internet edge: this context keeps none of its Internet rules.
        edge_hosted = edge_hosted or bool((deployment.get("parent") or {}).get("internet_edge_contexts"))
    return {
        "edge_hosted": edge_hosted,
        "edge_hostings": cap.get("internet_edge_hostings") or [],
        "edge_customers": [],
        "id": cap.get("id"),
        "name": cap.get("name"),
        "context_id": cap.get("context_id"),
        "vlan_id": cap.get("vlan_id"),
        "tenant_name": (cap.get("tenant") or {}).get("name"),
        "segments": list(segments.values()),
        "terminated_segments": cap.get("terminated_segments") or [],
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
    gateways itself (``ManagedVxlanSegment.terminated_on``; transforms/helpers/firewall_terminated.py).
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
    for context in contexts.values():
        if context["edge_hostings"]:
            _attach_edge_customers(context)
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


def _attach_edge_customers(context: dict[str, Any]) -> None:
    """The DC footprints an Internet edge context serves: their segments and, per footprint, the public address.

    The context is the Internet edge of the topologies in ``edge_hostings`` (``internet_edge_hostings``) for the
    environment of its tenant leg: the footprints of that environment, their segments (whose Internet rules it holds)
    and, for a footprint served by a dedicated context, that context's ``snat_address``, the address the footprint's
    prefixes are translated to here. A footprint on a shared context has no address of its own and takes the edge's.
    """
    tenant_type = next(
        (leg["ns_type"] for leg in context["legs"] if leg["ns_type"] not in (INTERNET_TYPE, MANAGEMENT_TYPE)), None
    )
    segments: dict[str, dict[str, Any]] = {}
    customers: list[dict[str, Any]] = []
    for hosting in context["edge_hostings"]:
        for footprint in sorted(hosting.get("children") or [], key=lambda f: str(f.get("name"))):
            if "network_segments" not in footprint:
                continue  # a pod, not a customer footprint
            if namespace_type_for_environment(str(footprint.get("environment") or "")) != tenant_type:
                continue
            owned = [segment for segment in footprint["network_segments"] if segment.get("id")]
            for segment in owned:
                segments.setdefault(segment["id"], segment)
            serving = footprint.get("serving_firewall_context") or {}
            dedicated = bool((serving.get("tenant") or {}).get("id"))
            customers.append(
                {
                    "name": footprint.get("name"),
                    "prefixes": sorted({prefix for segment in owned if (prefix := segment_prefix(segment))}),
                    "address": _snat_host(serving) if dedicated else None,
                }
            )
    context["segments"] = list(segments.values())
    context["edge_customers"] = customers


def _zone_member_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every segment in a zone one of `segments` (those on this firewall's
    interfaces) belongs to: the firewall routes that zone's CIDRs through the
    leg (get_firewall_static_routes), so it is where their rules hold."""
    return [
        member for segment in segments for member in (segment.get("security_zone") or {}).get("network_segments") or []
    ]


def _context_policies(context: dict[str, Any]) -> list[dict[str, Any]]:
    """The policies of one context's table: its segments' rules, split by where the Internet is enforced.

    An Internet edge context holds only the rules that cross the internet, to it or from it (``is_internet_rule``; the
    east-west rules and the rules between segments stay on the internal pair); a context whose DC has an Internet edge
    holds none of them (it has no INTERNET zone).
    """
    policies = segment_rule_policies(context.get("segments") or [])
    if not context.get("edge_hostings") and not context.get("edge_hosted"):
        return policies
    keep_internet = bool(context.get("edge_hostings"))
    split = [
        {**policy, "rules": [rule for rule in policy["rules"] if is_internet_rule(rule) == keep_internet]}
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
    (pre-filtered to role="internal-firewall") -> each device's shared capabilities
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
    return [ctx for ctx in contexts_with_legs(list(members.values())) if ctx.get("typename") == FIREWALL_CONTEXT_KIND]
