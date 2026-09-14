"""L2-Leaf device configuration transform."""

from typing import Any

from transforms.common import BaseDeviceTransform, get_vlans


class L2Leaf(BaseDeviceTransform):
    """Transform for L2-leaf device configurations.

    L2-leafs are pure L2 aggregation switches (middle_rack/mixed deployments,
    connect to leafs) — no VTEP role, no overlay BGP, no OSPF underlay. Only
    VLAN trunking and interface config are relevant.
    """

    query = "leaf_config"
    template_subdir = "l2_leafs"
    device_role = "l2-leaf"

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict[str, Any]:
        return {
            "vlans": get_vlans(activations=data.get("segment_deployments")),
        }
