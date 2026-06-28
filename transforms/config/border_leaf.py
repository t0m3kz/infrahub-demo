"""Border Leaf device configuration transform."""

from transforms.common import BaseDeviceTransform


class BorderLeaf(BaseDeviceTransform):
    """Transform for border leaf device configurations."""

    query = "border_leaf_config"
    template_subdir = "border_leafs"
    device_role = "border_leaf"
