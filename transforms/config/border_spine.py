"""Border Spine device configuration transform."""

from transforms.common import BaseDeviceTransform


class BorderSpine(BaseDeviceTransform):
    """Transform for border-spine device configurations.

    border-spine collapses spine + border-leaf into one device for
    micro-fabrics (see role: border-spine in
    schemas/extensions/topology/topology_dc.yml) — it terminates VLANs/
    VXLAN/ACLs/VRF gateways locally like a border-leaf, so it reuses the
    border-leaf query/templates rather than the spine's bare passthrough.
    """

    query = "border_spine_config"
    template_subdir = "border_leafs"
    device_role = "border-spine"
