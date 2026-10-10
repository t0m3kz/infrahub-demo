"""Border Leaf device configuration transform."""

from transforms.common import BaseDeviceTransform


class BorderLeaf(BaseDeviceTransform):
    """Transform for border leaf device configurations."""

    query = "border_leaf_config"
    template_subdir = "border_leafs"
    # Hyphenated, matching the schema's own role dropdown. It used to be
    # "border_leaf", which no consumer of device_role actually accepted:
    # get_vxlan_config only tolerated it via a defensive underscore alias, and
    # transforms/helpers/bgp.py's leaf-RR check (`device_role in ("leaf",
    # "border-leaf")`) could never match it at all.
    device_role = "border-leaf"
