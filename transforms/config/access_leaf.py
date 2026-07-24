from transforms.common import BaseDeviceTransform


class AccessLeaf(BaseDeviceTransform):
    """Transform for access-leaf device configurations.

    access-leafs sit below leafs (same position as l2-leaf) but are routed
    VTEPs — full VLANs, VXLAN-EVPN, BGP/OSPF, same as leaf/tor. Reuses leaf's
    templates and default _extra_config (no override, unlike l2_leaf which
    strips VXLAN/BGP/OSPF out).
    """

    query = "leaf_config"
    template_subdir = "leafs"
    device_role = "access-leaf"
