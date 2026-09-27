"""Border Leaf device configuration transform."""

from transforms.common import (
    BaseDeviceTransform,
    _flatten_deployment_firewall_contexts,
    _flatten_deployment_segment_activations,
    get_border_leaf_pbr_rules,
)


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

    def _extra_config(self, data: dict, platform_name: str, extra_roots: dict | None = None) -> dict:
        """Same base config as every other device role, plus border-leaf's
        own PBR rules — replaces the base class's customer_pbr_rules (always
        empty here: border-leaf has no segment on its own interfaces, SGT
        travels in-band inside the VXLAN header from the originating leaf
        instead — see .dev/scenariusze.txt's border-leaf sections)."""
        config = super()._extra_config(data, platform_name, extra_roots=extra_roots)
        config.pop("customer_pbr_rules", None)

        dc_activations = _flatten_deployment_segment_activations(data.get("deployment"))
        firewall_contexts = _flatten_deployment_firewall_contexts(data.get("deployment"))
        config["border_leaf_pbr_rules"] = get_border_leaf_pbr_rules(dc_activations, firewall_contexts, platform_name)
        return config
