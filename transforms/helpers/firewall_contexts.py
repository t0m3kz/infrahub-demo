"""Firewall context helpers: the per-VDOM/vsys/context list of a firewall and where its policies are placed.

Split out of ``transforms.helpers.firewall`` (zones, static routes and exchange transits stay there). The customer
PBR rules that resolve their nexthop through a context live in ``transforms.helpers.firewall_pbr``.
"""

from ipaddress import ip_interface
from typing import Any

from transforms.helpers.firewall import FIREWALL_CONTEXT_KIND, _snat_host
from transforms.helpers.ha import inline_addresses
from transforms.helpers.policy import segment_rule_policies
from transforms.helpers.segments import segment_hosting_candidates
from utils.exchange_transit import TRANSIT_SLOT, ZONE_BY_NS_TYPE, transit_addresses, transit_vlan


def _new_context(cap: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    """The template-facing shape of one ManagedFirewallContext capability."""
    segments: dict[str, dict[str, Any]] = {}
    for deployment in cap.get("served_deployments") or []:
        for segment in deployment.get("network_segments") or []:
            if segment.get("id"):
                segments.setdefault(segment["id"], segment)
    return {
        "id": cap.get("id"),
        "name": cap.get("name"),
        "context_id": cap.get("context_id"),
        "vlan_id": cap.get("vlan_id"),
        "tenant_name": (cap.get("tenant") or {}).get("name"),
        "segments": list(segments.values()),
        "snat": _snat_host(cap),
        "legs": legs,
    }


def get_firewall_contexts(
    interfaces: list[dict[str, Any]] | None,
    ha: dict[str, Any] | None = None,
    cluster_contexts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the per-VDOM/vsys/context list from this firewall's own sub-interfaces.

    A ManagedFirewallContext shows up as an capabilities entry on
    the DcimVirtualInterface(s) generators/topology/customer_dc.py created for
    it on the cluster's "uplink"-role trunk. A colocation context has no
    legs at all, hence no interface on a member: it comes from
    ``cluster_contexts`` (the contexts of the device's ManagedFirewallHA) and
    lists with its policies and no legs.

    ``legs`` are the context's exchange transit legs on this device: the
    sub-interfaces whose address is in a VRF namespace of a known type
    (utils/exchange_transit.py ZONE_BY_NS_TYPE), ordered prod, non_prod,
    internet, management. A leg is {interface, parent_interface, vlan (the
    transit VLAN), own_ip, virtual_ip / standby_ip (the HA pair's VIP and
    standby, None without ``ha``), anycast / vip (the border leaves' gateway
    and the firewall VIP, plain IPs of the /29), vrf, ns_type, zone, nameif}. A context holding none renders only its
    policies.

    ``segments`` are the segments terminating on the context: every segment
    of a deployment it serves (served_deployments, written by each
    deployment's own boarding run), de-duplicated by id.
    """
    contexts: dict[str, dict[str, Any]] = {}
    legs_by_context: dict[str, list[dict[str, Any]]] = {}
    for iface in interfaces or []:
        ip_obj = iface.get("ip_address") or {}
        parent_iface = iface.get("parent_interface") or {}
        for cap in iface.get("capabilities") or []:
            if cap.get("typename") != FIREWALL_CONTEXT_KIND:
                continue
            context_id = cap.get("id")
            if not context_id:
                continue
            context = contexts.get(context_id)
            if context is None:
                context = contexts[context_id] = _new_context(cap, legs_by_context.setdefault(context_id, []))
            namespace = ip_obj.get("ip_namespace") or {}
            ns_type = namespace.get("namespace_type")
            address = ip_obj.get("address")
            if ns_type not in ZONE_BY_NS_TYPE or not address or not cap.get("vlan_id"):
                continue
            network = ip_interface(address).network
            if network.version != 4:
                continue
            zone = ZONE_BY_NS_TYPE[ns_type]
            addresses = transit_addresses(str(network))
            legs_by_context[context_id].append(
                {
                    "interface": iface.get("name"),
                    "parent_interface": parent_iface,
                    "vlan": transit_vlan(cap["vlan_id"], ns_type),
                    "own_ip": address,
                    **inline_addresses(None, ha, address),
                    "anycast": addresses["anycast"],
                    "vip": addresses["vip"],
                    "vrf": namespace.get("name"),
                    "ns_type": ns_type,
                    "zone": zone,
                    "nameif": zone.removesuffix("-ZONE").lower(),
                }
            )
    for cap in cluster_contexts or []:
        if cap.get("id") and cap["id"] not in contexts:
            contexts[cap["id"]] = _new_context(cap, [])
    for legs in legs_by_context.values():
        legs.sort(key=lambda leg: TRANSIT_SLOT[leg["ns_type"]])
    return sorted(contexts.values(), key=lambda c: c.get("name") or "")


def _zone_member_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every segment in a zone one of `segments` (those on this firewall's
    interfaces) belongs to: the firewall routes that zone's CIDRs through the
    leg (get_firewall_static_routes), so it is where their rules hold."""
    return [
        member for segment in segments for member in (segment.get("security_zone") or {}).get("network_segments") or []
    ]


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
    by_context = {ctx["id"]: segment_rule_policies(ctx.get("segments") or []) for ctx in contexts}
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
    FirewallContextsOnDeploymentFields fragment: deployment.devices
    (pre-filtered to role="firewall") -> each device's capabilities (keep
    typename == "ManagedFirewallHA") -> contexts -> [ManagedFirewallContext, ...].
    Pod-tier devices (leaf/tor/access-leaf) have their deployment set to the
    POD, not the DC — deployment_id=self.data["pod"]["id"] in
    generators/topology/rack.py vs deployment_id=dc_id in
    generators/topology/dc.py — so the fragment also nests one `parent` hop
    for that case; both paths are flattened here, deduped by context id.
    """
    if not deployment:
        return []

    def _contexts_from_device_hosting(hosting: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for device in hosting.get("devices") or []:
            for cap in device.get("capabilities") or []:
                if cap.get("typename") != "ManagedFirewallHA":
                    continue
                found.extend(cap.get("contexts") or [])
        return found

    contexts = [
        ctx for hosting in segment_hosting_candidates(deployment) for ctx in _contexts_from_device_hosting(hosting)
    ]

    deduped: dict[str, dict[str, Any]] = {}
    for ctx in contexts:
        ctx_id = ctx.get("id")
        if ctx_id and ctx_id not in deduped:
            deduped[ctx_id] = ctx
    return list(deduped.values())
