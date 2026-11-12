"""Border Spine device configuration transform."""

from transforms.common import BaseDeviceTransform


class BorderSpine(BaseDeviceTransform):
    """Transform for border-spine device configurations."""

    query = "border_spine_config"
    template_subdir = "border_leafs"
    device_role = "border-spine"
