"""Distribution-switch device configuration transform (border and map-server of an office's LISP site)."""

from transforms.common import BaseDeviceTransform


class DistributionSwitch(BaseDeviceTransform):
    """Transform for an office distribution-switch: the co-located border and map-server/map-resolver."""

    query = "distribution_switch_config"
    template_subdir = "distribution_switches"
    device_role = "distribution-switch"
