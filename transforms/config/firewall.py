from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.addressing import sub_interface_parent
from transforms.helpers.firewall import get_firewall_static_routes, get_firewall_zones, get_zone_policies
from transforms.helpers.firewall_contexts import (
    firewall_context_zone_data,
    get_firewall_contexts,
    member_firewall_contexts,
    place_policies_in_contexts,
)
from transforms.helpers.firewall_terminated import terminated_interfaces
from transforms.helpers.ha import get_ha
from transforms.helpers.segments import interface_segments, segment_vlan_ids
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    FIREWALL_CONTEXT_KIND,
    INTERNET_TYPE,
    MANAGEMENT_TYPE,
    ZONE_BY_NS_TYPE,
)
from utils.routing_domain import interface_security_zone
from utils.segment_addressing import segment_prefix

DEFAULT_ROUTE = "0.0.0.0/0"


def _default_vr_interfaces(fw_interfaces: list[dict[str, Any]]) -> list[str]:
    """Names of the L3 interfaces (those with a namespace) that belong to the single ``default`` virtual router."""
    return sorted({i["name"] for i in fw_interfaces if i.get("name") and interface_security_zone(i).get("name")})


def _build_fw_interfaces(
    interfaces: list[dict[str, Any]],
    activations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build firewall interfaces with explicit interface or segment routing ownership.

    Only interfaces that have an ip_address are included — transit/management
    interfaces without an IP are skipped for zone rendering purposes.
    """
    seg_zone: dict[str, dict] = {}
    for act in activations:
        seg = act.get("segment") or {}
        if seg.get("id") and seg.get("security_zone"):
            seg_zone[seg["id"]] = seg["security_zone"]
    seg_vlan = segment_vlan_ids(activations)

    fw_ifaces: list[dict[str, Any]] = []
    for iface in interfaces:
        ip_obj = iface.get("ip_address") or {}
        if not ip_obj.get("address"):
            continue

        zone: dict | None = interface_security_zone(iface) or None
        vlan_id: int | None = None
        parent = sub_interface_parent(iface)
        segments = interface_segments(iface)
        for seg in segments:
            if seg.get("id") in seg_zone and zone is None:
                zone = seg_zone[seg["id"]]
            if seg.get("name") in seg_vlan and vlan_id is None:
                vlan_id = seg_vlan[seg["name"]]
        if vlan_id is None and parent:
            vlan_id = int(str(iface["name"]).rsplit(".", 1)[1])
        context_caps = {
            cap.get("id"): cap
            for cap in iface.get("capabilities") or []
            if cap.get("typename") == FIREWALL_CONTEXT_KIND
        }
        if len(context_caps) > 1:
            raise ValueError(f"firewall interface {iface.get('name')!r} belongs to multiple contexts")
        context = next(iter(context_caps.values()), None)
        if context and (not context.get("id") or not context.get("name")):
            raise ValueError(f"firewall interface {iface.get('name')!r} has an incomplete context owner")

        fw_ifaces.append(
            {
                "name": iface.get("name"),
                "description": iface.get("description"),
                "status": iface.get("status"),
                "vlan_id": vlan_id,
                "parent_interface": parent,
                "ip_address": ip_obj,
                "vrf": iface.get("vrf"),
                "security_zone": zone,
                "context_name": context.get("name") if context else None,
            }
        )
    return fw_ifaces


def _segment_prefixes(zones_data: list[dict[str, Any]] | None) -> dict[str, tuple[str | None, str]]:
    """Context-local segment and endpoint prefixes, keyed by CIDR and segment id."""
    prefixes: dict[str, tuple[str | None, str]] = {
        prefix: (zone["name"], prefix) for zone in get_firewall_zones(zones_data) for prefix in zone["member_cidrs"]
    }
    for zone in zones_data or []:
        for segment in zone.get("network_segments") or []:
            prefix = segment_prefix(segment)
            if segment.get("id") and prefix:
                prefixes[segment["id"]] = (zone.get("name"), prefix)
    return prefixes


def apply_context_routes(context: dict[str, Any], segment_prefixes: dict[str, tuple[str | None, str]]) -> None:
    """Fill the routing table and isolation of a context that has transit legs.

    One table per context (Check Point VS / Junos
    routing-instance / PAN-OS per-vsys virtual router): every served tenant
    segment prefix (but a segment the pair terminates itself, which is connected) goes to the border leaves' anycast
    gateway (.1) of the tenant leg in its zone's VRF (a segment with no known zone takes the
    only tenant leg), the CIDRs of the MANAGEMENT zone to the anycast of the
    MANAGEMENT leg, and 0.0.0.0/0 to the anycast of the INTERNET leg. A
    context holding both PROD and NON-PROD also gets an explicit PROD<->NONPROD
    deny (``isolation``), since the two VRFs must never be bridged through it;
    one with a MANAGEMENT leg also denies MANAGEMENT towards each tenant
    zone it holds (the services answer, they never initiate).

    Sets ``routes`` ([{destination, nexthop, interface, vrf}], tenant
    prefixes first), ``isolation`` ([{src_zone, dst_zone}]), ``nat`` (see
    _nat_entries) and each leg's ``prefixes``. A context without legs
    (colocation) is left as is.
    """
    legs = context.get("legs") or []
    if not legs:
        return
    # Tenant legs are the legs of a tenant VRF (a key of EXCHANGE_PEERS); an
    # exchange peer (INTERNET, MANAGEMENT) is never taken for one.
    tenant_legs = [leg for leg in legs if leg["ns_type"] in EXCHANGE_PEERS]
    for leg in legs:
        leg["prefixes"] = []
    terminated = {segment.get("id") for segment in context.get("terminated_segments") or []}
    for segment in context.get("segments") or []:
        # A segment the pair terminates is connected to it: no route to the border leaves' gateway.
        if segment.get("id") in terminated:
            continue
        root_zone, root_prefix = segment_prefixes.get(segment.get("id"), (None, None))
        # A referenced policy endpoint can supply the same segment's prefix.
        prefix = segment_prefix(segment) or root_prefix
        zone = (segment.get("security_zone") or {}).get("name") or root_zone
        if not prefix:
            continue
        leg = next((x for x in tenant_legs if x["zone"] == zone), None)
        if leg is None and zone is None and len(tenant_legs) == 1:
            leg = tenant_legs[0]
        if leg and prefix not in leg["prefixes"]:
            leg["prefixes"].append(prefix)

    # An exchange peer (INTERNET, MANAGEMENT) is reached through its own leg:
    # INTERNET by the default route, any other peer by the CIDRs of the
    # segments and policy endpoints reached through this context.
    peer_legs = [leg for leg in legs if leg["ns_type"] not in EXCHANGE_PEERS and leg["ns_type"] != INTERNET_TYPE]
    for leg in peer_legs:
        zone_prefixes = {prefix for zone, prefix in segment_prefixes.values() if zone == leg["zone"]}
        zone_prefixes.update(
            prefix
            for segment in context.get("segments") or []
            if (segment.get("security_zone") or {}).get("name") == leg["zone"] and (prefix := segment_prefix(segment))
        )
        leg["prefixes"] = sorted(zone_prefixes)

    routes = [
        {"destination": prefix, "nexthop": leg["anycast"], "interface": leg["interface"], "vrf": leg["vrf"]}
        for leg in (*tenant_legs, *peer_legs)
        for prefix in sorted(leg["prefixes"])
    ]
    internet = next((leg for leg in legs if leg["ns_type"] == INTERNET_TYPE), None)
    if internet:
        routes.append(
            {
                "destination": DEFAULT_ROUTE,
                "nexthop": internet["anycast"],
                "interface": internet["interface"],
                "vrf": internet["vrf"],
            }
        )
    context["routes"] = routes
    context["nat"] = _nat_entries(context, legs, tenant_legs)

    held = {leg["ns_type"] for leg in legs}
    pairs = [("prod", "non_prod"), ("non_prod", "prod")] if {"prod", "non_prod"} <= held else []
    # Tenant-initiated only: the MANAGEMENT zone never opens a session towards a tenant.
    if MANAGEMENT_TYPE in held:
        pairs += [(MANAGEMENT_TYPE, ns_type) for ns_type in EXCHANGE_PEERS if ns_type in held]
    context["isolation"] = [{"src_zone": ZONE_BY_NS_TYPE[a], "dst_zone": ZONE_BY_NS_TYPE[b]} for a, b in pairs]


def _nat_entries(
    context: dict[str, Any], legs: list[dict[str, Any]], tenant_legs: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The context's source-NAT entries, one per egress leg; every vendor renders them with one loop.

    The tenant legs' traffic leaves

    - the INTERNET leg as the context's one public SNAT address, which the
      firewall has to own itself (``own_address``: PAN-OS secondary IP); no
      address, no entry;
    - the MANAGEMENT leg as that leg's VIP, which it already owns as its
      interface address (``own_address`` false), so the services see one
      source and need no tenant return route.

    An entry is {suffix, egress (the leg), address (plain IP), own_address,
    tenant_legs}. ``suffix`` is appended to the vendor's object names so two
    entries never collide; INTERNET's is empty (its names predate the list).
    """
    candidates = [
        (next((leg for leg in legs if leg["ns_type"] == ns_type), None), ns_type)
        for ns_type in (INTERNET_TYPE, MANAGEMENT_TYPE)
    ]
    entries: list[dict[str, Any]] = []
    for egress, ns_type in candidates:
        own_address = ns_type == INTERNET_TYPE
        if own_address and egress and tenant_legs:
            # The customers of an Internet edge that have a public address of their own (a dedicated context): their
            # prefixes are translated to it, ahead of the entry for everyone else.
            entries += [
                {
                    "suffix": f"-{customer['name']}",
                    "egress": egress,
                    "address": customer["address"],
                    "own_address": True,
                    "tenant_legs": tenant_legs,
                    "sources": customer["prefixes"],
                }
                for customer in context.get("edge_customers") or []
                if customer.get("address") and customer.get("prefixes")
            ]
        address = context.get("snat") if own_address else (egress or {}).get("vip")
        if egress and address and tenant_legs:
            entries.append(
                {
                    "suffix": "" if own_address else f"-{egress['nameif'].upper()}",
                    "egress": egress,
                    "address": address,
                    "own_address": own_address,
                    "tenant_legs": tenant_legs,
                }
            )
    return entries


class Firewall(BaseDeviceTransform):
    query = "firewall_config"
    template_subdir = "firewalls"
    resolve_vlan_row = False

    def collect_policies(
        self, device: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """(activations, contexts, root policies, context id -> policies) of a
        firewall: the rules of the segments it serves, placed in the table of
        the context they terminate on (place_policies_in_contexts). Shared
        with checks/firewall.py, which validates exactly these rules."""
        interfaces = device.get("interfaces") or []
        activations = self._collect_activations_from_interfaces(interfaces)
        ha = get_ha(device.get("capabilities"), interfaces, device.get("name"))
        contexts = get_firewall_contexts(interfaces, ha, member_firewall_contexts(device.get("shared_capabilities")))
        root, by_context = place_policies_in_contexts(contexts, [act.get("segment") or {} for act in activations])
        return activations, contexts, root, by_context

    async def transform(self, data: Any) -> Any:
        device, roots, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device)

        activations, contexts, root_policies_data, context_policies_data = self.collect_policies(device)

        ha = get_ha(device.get("capabilities"), device.get("interfaces"), device.get("name"))
        # A transit leg renders inside its context, not as a root interface.
        leg_interfaces = {leg["interface"] for context in contexts for leg in context["legs"]}
        fw_interfaces = _build_fw_interfaces(
            [i for i in device.get("interfaces") or [] if i.get("name") not in leg_interfaces], activations
        )
        for context in contexts:
            for interface in terminated_interfaces(
                [context], device.get("interfaces") or [], ha, device.get("id"), device.get("name") or ""
            ):
                interface["context_name"] = context["name"]
                fw_interfaces.append(interface)
        for context in contexts:
            context_zones = firewall_context_zone_data([context], roots.get("SecurityZone"))
            apply_context_routes(context, _segment_prefixes(context_zones))
            authored_interfaces = [iface for iface in fw_interfaces if iface.get("context_name") == context["name"]]
            context["authored_interfaces"] = [iface["name"] for iface in authored_interfaces]
            authored_routes = get_firewall_static_routes(authored_interfaces, get_firewall_zones(context_zones))
            context.setdefault("routes", []).extend(authored_routes)
            context["policies"] = get_zone_policies(context_policies_data.get(context["id"]))

        zone_data = firewall_context_zone_data(
            [*contexts, {"segments": [act.get("segment") or {} for act in activations]}],
            roots.get("SecurityZone"),
        )
        zones = get_firewall_zones(zone_data)
        for zone in zones:
            zone["member_cidrs"] = sorted(set(zone["member_cidrs"]))
        # Contexts are visited by name; retain the environment order for equal-trust zones.
        zone_order = {name: rank for rank, name in enumerate(ZONE_BY_NS_TYPE.values())}
        zones.sort(key=lambda zone: (-zone["trust_level"], zone_order.get(zone["name"], 0)))
        config = self._build_config(device, platform_name)
        root_interfaces = [iface for iface in fw_interfaces if not iface.get("context_name")]
        config.update(
            {
                "fw_interfaces": fw_interfaces,
                "zones": zones,
                "zone_policies": get_zone_policies(root_policies_data),
                "static_routes": get_firewall_static_routes(root_interfaces, zones),
                "default_vr_interfaces": _default_vr_interfaces(root_interfaces),
                "ha": ha,
                "contexts": contexts,
            }
        )
        return self._render(platform_name, config)
