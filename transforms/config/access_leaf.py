from transforms.common import BaseDeviceTransform


class AccessLeaf(BaseDeviceTransform):
    """Transform for access-leaf device configurations."""

    query = "leaf_config"
    template_subdir = "leafs"
    device_role = "access-leaf"
