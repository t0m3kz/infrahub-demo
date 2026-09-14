from transforms.common import BaseDeviceTransform


class Spine(BaseDeviceTransform):
    query = "spine_config"
    template_subdir = "spines"
    device_role = "spine"
