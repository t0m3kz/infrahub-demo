"""Transform for super-spine device configurations."""

from transforms.common import BaseDeviceTransform


class SuperSpine(BaseDeviceTransform):
    """Generate configuration for super-spine devices.

    Super-spine is underlay-routing + BGP L2VPN EVPN route-reflector only —
    never a VTEP, never terminates VXLAN — so this is a bare passthrough,
    same shape as transforms/config/spine.py. get_vxlan_config()'s own
    VTEP-role gate (transforms/helpers/vxlan.py) keeps BaseDeviceTransform's
    default _extra_config() safe for this role without any override here.
    """

    query = "super_spine_config"
    template_subdir = "super_spines"
    device_role = "super_spine"
