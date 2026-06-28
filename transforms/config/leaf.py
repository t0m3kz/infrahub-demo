from transforms.common import BaseDeviceTransform


class Leaf(BaseDeviceTransform):
    query = "leaf_config"
    template_subdir = "leafs"
    device_role = "leaf"
