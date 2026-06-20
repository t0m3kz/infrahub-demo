"""Transform for super-spine device configurations."""

from typing import Any

from .common import BaseDeviceTransform, get_vlans, get_vxlan_config


class SuperSpine(BaseDeviceTransform):
    """Generate configuration for super-spine devices."""

    query = "super_spine_config"
    template_subdir = "super_spines"
    device_role = "super_spine"

    def _filter_segment_deployments(self, activations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep only stretched segments — those deployed in more than one DC.

        A stretched VXLAN segment spans multiple data centres (e.g. DC1 ↔ DC2 DCI).
        The super-spine only needs VLAN/VNI config for segments that cross DC boundaries.
        Local (single-DC) segments are handled entirely by the leaf/border-leaf layer.
        """
        stretched = []
        for act in activations:
            seg = act.get("segment") or {}
            deployments = seg.get("deployments") or []
            if len(deployments) > 1:
                stretched.append(act)
        return stretched

    def _build_config(self, data: dict, platform_name: str) -> dict:
        """Extend base config by injecting stretched VLAN IDs into all uplink interfaces.

        Super-spine uplinks are always fabric-facing — there is no per-interface
        segment assignment in the data model. Instead, every physical non-loopback
        interface automatically trunks all stretched VLANs.
        """
        config = super()._build_config(data, platform_name)

        activations = data.get("segment_deployments") or []
        stretched_vlan_ids = [act["vlan_id"] for act in activations if act.get("vlan_id")]

        if stretched_vlan_ids:
            for iface in config.get("interfaces", []):
                name = iface.get("name", "").lower()
                if "loopback" in name or "vlan" in name or "management" in name:
                    continue
                iface["vlans"] = stretched_vlan_ids

        return config

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict[str, Any]:
        activations = data.get("segment_deployments")
        return {
            "vlans": get_vlans(activations=activations),
            "vxlan": get_vxlan_config(data, platform_name, device_role=self.device_role, activations=activations),
        }
