"""Access-switch device configuration transform (the fabric edge of an office's LISP site)."""

from transforms.common import BaseDeviceTransform


class AccessSwitch(BaseDeviceTransform):
    """Transform for an office access-switch: the LISP edge (ITR/ETR) its hosts attach to."""

    query = "access_switch_config"
    template_subdir = "access_switches"
    device_role = "access-switch"
