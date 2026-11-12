"""Border Leaf device configuration transform."""

from transforms.common import BaseDeviceTransform


class BorderLeaf(BaseDeviceTransform):
    """Transform for border leaf device configurations."""

    query = "border_leaf_config"
    template_subdir = "border_leafs"
    # Hyphenated, matching the schema's own role dropdown.
    device_role = "border-leaf"
