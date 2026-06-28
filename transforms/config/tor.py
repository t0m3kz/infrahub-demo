from transforms.common import BaseDeviceTransform


class ToR(BaseDeviceTransform):
    query = "leaf_config"
    template_subdir = "leafs"
    device_role = "tor"
