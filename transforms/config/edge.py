from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.bgp import get_internet_prefixes


class Edge(BaseDeviceTransform):
    query = "edge_config"
    template_subdir = "edges"
    # A VTEP only where the edge is its colocation site's EVPN Multi-Site
    # border gateway: get_vxlan_config returns None without stretched-segment
    # activations, so every other edge renders exactly as before.
    device_role = "edge"

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict[str, Any]:
        config = super()._extra_config(data, platform_name, extra_roots=extra_roots)
        # The public space an upstream transit session may announce.
        config["internet_prefixes"] = get_internet_prefixes((extra_roots or {}).get("IpamPrefix"))
        return config
