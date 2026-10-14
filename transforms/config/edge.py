from typing import Any

from transforms.common import BaseDeviceTransform
from transforms.helpers.bgp import get_external_peer_prefixes, get_internet_prefixes, get_onprem_prefixes


class Edge(BaseDeviceTransform):
    query = "edge_config"
    template_subdir = "edges"
    # A VTEP only where the edge is its colocation site's EVPN Multi-Site border
    # gateway: get_vxlan_config returns None without stretched-segment
    # activations, so every other edge renders exactly as before.
    device_role = "edge"
    # The shared firewall context's tenant 0/0 -> VIP stays a local static: each
    # edge resolves the VIP over the transit L2VNI, and exporting it as type-5
    # would let a DC import another site's PROD default via a stretched segment.
    export_tenant_default_to_evpn = False

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict[str, Any]:
        config = super()._extra_config(data, platform_name, extra_roots=extra_roots)
        roots = extra_roots or {}
        # The public space an upstream transit session may announce.
        config["internet_prefixes"] = get_internet_prefixes(roots.get("IpamPrefix"))
        # External tenant-VRF peers (session.external): what each may send in
        # (by handoff sub-interface) and what its VRF announces out.
        config["external_peer_prefixes"] = get_external_peer_prefixes(
            data.get("interfaces"),
            roots.get("CloudVirtualInterface"),
            data.get("name") or "",
            device_capabilities=data.get("capabilities"),
            offices=roots.get("TopologyCustomerOffice"),
        )
        config["onprem_prefixes"] = get_onprem_prefixes(roots.get("ManagedNetworkSegment"))
        return config
