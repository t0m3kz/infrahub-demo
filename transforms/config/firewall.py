from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.firewall import (
    get_firewall_contexts,
    get_firewall_static_routes,
    get_firewall_zones,
    get_zone_policies,
    place_policies_in_contexts,
)
from transforms.helpers.ha import get_ha, inline_addresses
from transforms.helpers.segments import _get_segment_prefix_str, segment_vlan_ids
from utils.exchange_transit import ZONE_BY_NS_TYPE

DEFAULT_ROUTE = "0.0.0.0/0"


def _build_fw_interfaces(
    interfaces: list[dict[str, Any]],
    activations: list[dict[str, Any]],
    ha: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build firewall interface list enriched with security_zone from segment activations.

    The firewall has physical interfaces and virtual (sub-)interfaces.  Zone assignment
    lives on the segment, not the interface.  For each interface we look up the
    segment deployed on it (via activations, by segment id) and attach its security_zone.
    On an HA pair terminating that segment inline, virtual_ip/standby_ip are the
    pair's addresses on it (see transforms.helpers.ha.inline_addresses).

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

        zone: dict | None = None
        vlan_id: int | None = None
        capabilities = iface.get("interface_capabilities") or []
        for cap in capabilities:
            if cap.get("id") in seg_zone and zone is None:
                zone = seg_zone[cap["id"]]
            if cap.get("name") in seg_vlan and vlan_id is None:
                vlan_id = seg_vlan[cap["name"]]

        fw_ifaces.append(
            {
                "name": iface.get("name"),
                "description": iface.get("description"),
                "status": iface.get("status"),
                "vlan_id": vlan_id,
                "parent_interface": iface.get("parent_interface"),
                "ip_address": ip_obj,
                "security_zone": zone,
                **inline_addresses(capabilities, ha),
            }
        )
    return fw_ifaces


def _segment_prefixes(zones_data: list[dict[str, Any]] | None) -> dict[str, tuple[str | None, str]]:
    """Segment id -> (security zone name, IPv4 gateway prefix) from the
    SecurityZone root: where a context's served segments get their prefix and
    their side (PROD-ZONE / NONPROD-ZONE) of a context holding both."""
    prefixes: dict[str, tuple[str | None, str]] = {}
    for zone in zones_data or []:
        for segment in zone.get("network_segments") or []:
            prefix = _get_segment_prefix_str(segment)
            if segment.get("id") and prefix:
                prefixes[segment["id"]] = (zone.get("name"), prefix)
    return prefixes


def apply_context_routes(context: dict[str, Any], segment_prefixes: dict[str, tuple[str | None, str]]) -> None:
    """Fill the routing table and isolation of a context that has transit legs.

    One table per context (VDOM / ASA context / Check Point VS / Junos
    routing-instance / PAN-OS per-vsys virtual router): every served tenant
    segment prefix goes to the border leaves' anycast gateway (.1) of the
    tenant leg in its zone's VRF (a segment with no known zone takes the
    only tenant leg), and 0.0.0.0/0 to the anycast of the INTERNET leg. A
    context holding both PROD and NON-PROD also gets
    an explicit PROD<->NONPROD deny (``isolation``), since the two VRFs must
    never be bridged through it.

    Sets ``routes`` ([{destination, nexthop, interface, vrf}], tenant
    prefixes first), ``isolation`` ([{src_zone, dst_zone}]) and each tenant
    leg's ``prefixes``. A context without legs is left as the legacy shape.
    """
    legs = context.get("legs") or []
    if not legs:
        return
    tenant_legs = [leg for leg in legs if leg["ns_type"] != "internet"]
    for leg in legs:
        leg["prefixes"] = []
    for segment in context.get("segments") or []:
        root_zone, root_prefix = segment_prefixes.get(segment.get("id"), (None, None))
        # The segment's own gateway prefix and zone win; the SecurityZone root
        # only fills what the segment did not carry.
        prefix = _get_segment_prefix_str(segment) or root_prefix
        zone = (segment.get("security_zone") or {}).get("name") or root_zone
        if not prefix:
            continue
        leg = next((x for x in tenant_legs if x["zone"] == zone), None)
        if leg is None and zone is None and len(tenant_legs) == 1:
            leg = tenant_legs[0]
        if leg and prefix not in leg["prefixes"]:
            leg["prefixes"].append(prefix)

    routes = [
        {"destination": prefix, "nexthop": leg["anycast"], "interface": leg["interface"], "vrf": leg["vrf"]}
        for leg in tenant_legs
        for prefix in sorted(leg["prefixes"])
    ]
    internet = next((leg for leg in legs if leg["ns_type"] == "internet"), None)
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

    held = {leg["ns_type"] for leg in legs}
    context["isolation"] = (
        [
            {"src_zone": ZONE_BY_NS_TYPE[a], "dst_zone": ZONE_BY_NS_TYPE[b]}
            for a, b in (("prod", "non_prod"), ("non_prod", "prod"))
        ]
        if {"prod", "non_prod"} <= held
        else []
    )


class Firewall(BaseDeviceTransform):
    query = "firewall_config"
    template_subdir = "firewalls"
    resolve_vlan_domain = False

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
        contexts = get_firewall_contexts(interfaces, ha)
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
            [i for i in device.get("interfaces") or [] if i.get("name") not in leg_interfaces], activations, ha
        )
        segment_prefixes = _segment_prefixes(roots.get("SecurityZone"))
        for context in contexts:
            apply_context_routes(context, segment_prefixes)
            context["policies"] = get_zone_policies(context_policies_data.get(context["id"]))

        zones = get_firewall_zones(roots.get("SecurityZone"))
        config = self._build_config(device, platform_name)
        config.update(
            {
                "fw_interfaces": fw_interfaces,
                "zones": zones,
                "zone_policies": get_zone_policies(root_policies_data),
                "static_routes": get_firewall_static_routes(fw_interfaces, zones),
                "ha": ha,
                "contexts": contexts,
            }
        )
        return self._render(platform_name, config)
