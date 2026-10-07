from transforms.common import BaseDeviceTransform


class Edge(BaseDeviceTransform):
    query = "edge_config"
    template_subdir = "edges"
    # A VTEP only where the edge is its colocation site's EVPN Multi-Site
    # border gateway: get_vxlan_config returns None without stretched-segment
    # activations, so every other edge renders exactly as before.
    device_role = "edge"
