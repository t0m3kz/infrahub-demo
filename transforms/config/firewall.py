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
        contexts = get_firewall_contexts(interfaces)
        root, by_context = place_policies_in_contexts(contexts, [act.get("segment") or {} for act in activations])
        return activations, contexts, root, by_context

    async def transform(self, data: Any) -> Any:
        device, roots, platform_name = self._device_and_platform(data)
        if not platform_name:
            return self._no_platform_config(device)

        activations, contexts, root_policies_data, context_policies_data = self.collect_policies(device)

        ha = get_ha(device.get("capabilities"), device.get("interfaces"), device.get("name"))
        fw_interfaces = _build_fw_interfaces(device.get("interfaces") or [], activations, ha)
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
