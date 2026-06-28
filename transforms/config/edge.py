from typing import Any

from transforms.common import BaseDeviceTransform, get_vlans


class Edge(BaseDeviceTransform):
    query = "edge_config"
    template_subdir = "edges"

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict[str, Any]:
        return {
            "vlans": get_vlans(activations=data.get("segment_deployments")),
        }
