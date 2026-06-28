from typing import Any

from transforms.common import BaseDeviceTransform, get_firewall_static_routes, get_firewall_zones, get_zone_policies
from transforms.helpers.ha import get_ha
from utils.data_cleaning import clean_data


def _build_fw_interfaces(
    interfaces: list[dict[str, Any]],
    activations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build firewall interface list enriched with security_zone from segment activations.

    The firewall has physical interfaces and virtual (sub-)interfaces.  Zone assignment
    now lives on the segment, not the interface.  For each interface we look up the
    segment deployed on it (via activations) and attach its security_zone.

    Only interfaces that have an ip_address are included — transit/management
    interfaces without an IP are skipped for zone rendering purposes.
    """
    # Build segment-name → security_zone lookup from activations
    seg_zone: dict[str, dict] = {}
    for act in activations:
        seg = act.get("segment") or {}
        seg_name = seg.get("name")
        zone = seg.get("security_zone")
        if seg_name and zone:
            seg_zone[seg_name] = zone

    # Build segment-name → vlan_id lookup for sub-interface rendering
    seg_vlan: dict[str, int] = {}
    for act in activations:
        seg = act.get("segment") or {}
        seg_name = seg.get("name")
        vlan_id = act.get("vlan_id")
        if seg_name and vlan_id:
            seg_vlan[seg_name] = vlan_id

    fw_ifaces: list[dict[str, Any]] = []
    for iface in interfaces:
        ip_obj = iface.get("ip_address") or {}
        if not ip_obj.get("address"):
            continue

        zone: dict | None = None
        vlan_id: int | None = None
        for cap in iface.get("interface_capabilities") or []:
            cap_name = cap.get("name")
            if not cap_name:
                continue
            if cap_name in seg_zone and zone is None:
                zone = seg_zone[cap_name]
            if cap_name in seg_vlan and vlan_id is None:
                vlan_id = seg_vlan[cap_name]

        fw_ifaces.append(
            {
                "name": iface.get("name"),
                "description": iface.get("description"),
                "status": iface.get("status"),
                "vlan_id": vlan_id,
                "parent_interface": iface.get("parent_interface"),
                "ip_address": ip_obj,
                "security_zone": zone,
            }
        )
    return fw_ifaces


def _collect_segment_policies(activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract SecurityPolicy nodes from segment activations.

    Traversal path: activation → segment → security_policies
    """
    seen: dict[str, dict] = {}
    for act in activations:
        seg = act.get("segment") or {}
        for policy in seg.get("security_policies") or []:
            name = policy.get("name") or policy.get("id")
            if name and name not in seen:
                seen[name] = policy
    return list(seen.values())


def _merge_policies(
    global_policies: list[dict[str, Any]],
    segment_policies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge global (zone-level) and segment-scoped policies, deduplicating by name.

    Segment-scoped policies take precedence on name collision.
    """
    merged: dict[str, dict] = {}
    for policy in global_policies:
        name = policy.get("name") or policy.get("id")
        if name:
            merged[name] = policy
    for policy in segment_policies:
        name = policy.get("name") or policy.get("id")
        if name:
            merged[name] = policy
    return list(merged.values())


class Firewall(BaseDeviceTransform):
    query = "firewall_config"
    template_subdir = "firewalls"

    async def transform(self, data: Any) -> Any:
        cleaned = clean_data(data)

        devices = cleaned.get("DcimPhysicalDevice") or []
        device = devices[0] if devices else {}

        zones_data = cleaned.get("SecurityZone") or []
        global_policies_data = cleaned.get("SecurityPolicy") or []

        platform = device.get("platform") or {}
        platform_name = platform.get("netmiko_device_type")

        if not platform_name:
            device_name = device.get("name", "Unknown Device")
            return (
                f"! Device {device_name} has no platform with "
                f"netmiko_device_type defined.\n! No configuration generated.\n"
            )

        activations = self._collect_activations_from_interfaces(device.get("interfaces") or [])

        fw_interfaces = _build_fw_interfaces(device.get("interfaces") or [], activations)
        segment_policies_data = _collect_segment_policies(activations)
        all_policies_data = _merge_policies(global_policies_data, segment_policies_data)

        zones = get_firewall_zones(zones_data)
        config = self._build_config(device, platform_name)
        ha_config = get_ha(device.get("capabilities"), device.get("interfaces"))
        config.update(
            {
                "fw_interfaces": fw_interfaces,
                "zones": zones,
                "zone_policies": get_zone_policies(all_policies_data),
                "static_routes": get_firewall_static_routes(fw_interfaces, zones),
                "ha": ha_config,
            }
        )

        template = self._load_template(platform_name)
        return template.render(**config)
