"""Transform for super-spine device configurations."""

from transforms.common import BaseDeviceTransform


class SuperSpine(BaseDeviceTransform):
    """Generate configuration for super-spine devices."""

    query = "super_spine_config"
    template_subdir = "super_spines"
    device_role = "super-spine"
