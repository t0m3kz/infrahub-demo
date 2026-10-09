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
from transforms.helpers.policy import merge_policies, segment_policies
from transforms.helpers.segments import segment_vlan_ids


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


def _collect_segment_policies(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract SecurityPolicy nodes from segment activations.

    Traversal path: activation → segment → security_policy
    """
    seen: dict[str, dict] = {}
    for act in activations:
        seg = act.get("segment") or {}
        for policy in segment_policies(seg):
            name = policy.get("name") or policy.get("id")
            if name and name not in seen:
                seen[name] = policy
    return list(seen.values())


class Firewall(BaseDeviceTransform):
    query = "firewall_config"
    template_subdir = "firewalls"
    resolve_vlan_domain = False

    async def transform(self, data: Any) -> Any:
        device, roots, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device)

        activations = self._collect_activations_from_interfaces(device.get("interfaces") or [])

        ha = get_ha(device.get("capabilities"), device.get("interfaces"), device.get("name"))
        fw_interfaces = _build_fw_interfaces(device.get("interfaces") or [], activations, ha)
        all_policies_data = merge_policies(roots.get("SecurityPolicy"), _collect_segment_policies(activations))

        # Each rule lands in the context (VDOM/vsys) its segments' traffic is
        # redirected to; only rules no context serves stay in the root list.
        contexts = get_firewall_contexts(device.get("interfaces"))
        root_policies_data, context_policies_data = place_policies_in_contexts(
            all_policies_data, contexts, segments=[act.get("segment") or {} for act in activations]
        )
        for context in contexts:
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
