"""Unit tests for transforms/config/border_leaf.py and
_flatten_deployment_segment_activations() (transforms/helpers/segments.py).

Covers:
  - _flatten_deployment_segment_activations() — deployment.segment_deployments
    -> activations shape, used by border-leaf instead of the per-interface
    traversal a leaf uses (border-leaf has no segment capability on its own
    interfaces — SGT travels in-band inside the VXLAN header instead).
    Keyed on `vni` now, not `vlan_id` (local VLAN ID is per-VLAN-domain, not
    DC-wide — see ManagedVlanDomainSegment).
  - BorderLeaf._extra_config()      — replaces customer_pbr_rules with
    border_leaf_pbr_rules, sourced from the DC-wide traversal above.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2

from transforms.config.border_leaf import BorderLeaf
from transforms.helpers.segments import _flatten_deployment_segment_activations

_TEMPLATES_CONFIGS_DIR = Path(__file__).parent.parent.parent / "templates" / "configs"


def _minimal_ctx(**overrides: Any) -> dict[str, Any]:
    """Minimal rendering context — mirrors test_vxlan_transforms.py's own
    _minimal_ctx, plus the border-leaf-specific keys BaseDeviceTransform's
    _extra_config always sets (sgt_rules, customer_pbr_rules replaced by
    border_leaf_pbr_rules, lb_backend_pbr_rules)."""
    ctx: dict[str, Any] = {
        "hostname": "test-border-leaf",
        "name": "test-border-leaf",
        "vlans": [],
        "acls": [],
        "interfaces": [],
        "ospf": [],
        "bgp": [],
        "vxlan": {"enabled": False},
        "vrf_gateways": {},
        "sgt_rules": [],
        "border_leaf_pbr_rules": [],
        "lb_backend_pbr_rules": [],
        "ntp": None,
        "syslog": None,
        "snmp": None,
        "aaa": None,
        "mlag": None,
    }
    ctx.update(overrides)
    return ctx


def _pbr_rule(
    *,
    match_by_tag: bool = True,
    sgt: int | None = 10,
    sgt_name: str | None = "web-tier",
    acl_name: str | None = None,
    source_prefixes: list[str] | None = None,
    fw_nexthop: str = "10.65.0.0",
    customer_name: str | None = "web",
    environment: str | None = None,
) -> dict[str, Any]:
    return {
        "match_by_tag": match_by_tag,
        "sgt": sgt,
        "sgt_name": sgt_name if match_by_tag else None,
        "acl_name": acl_name if not match_by_tag else None,
        "source_prefixes": source_prefixes or [],
        "fw_nexthop": fw_nexthop,
        "customer_name": customer_name,
        "environment": environment,
    }


# ===========================================================================
# _flatten_deployment_segment_activations()
# ===========================================================================


class TestFlattenDeploymentSegmentActivations:
    def test_none_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_segment_activations(None) == []

    def test_empty_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_segment_activations({}) == []

    def test_no_segment_deployments_key_returns_empty(self) -> None:
        assert _flatten_deployment_segment_activations({"name": "DC10"}) == []

    def test_single_activation_extracted(self) -> None:
        deployment = {
            "segment_deployments": [
                {"vni": 10100, "segment": {"id": "seg-1", "customer_name": "web"}},
            ]
        }
        result = _flatten_deployment_segment_activations(deployment)
        assert result == [{"vni": 10100, "segment": {"id": "seg-1", "customer_name": "web"}}]

    def test_multiple_activations_all_extracted(self) -> None:
        deployment = {
            "segment_deployments": [
                {"vni": 10100, "segment": {"id": "seg-1"}},
                {"vni": 10200, "segment": {"id": "seg-2"}},
            ]
        }
        result = _flatten_deployment_segment_activations(deployment)
        assert [a["vni"] for a in result] == [10100, 10200]

    def test_entry_missing_vni_is_skipped(self) -> None:
        deployment = {"segment_deployments": [{"vni": None, "segment": {"id": "seg-1"}}]}
        assert _flatten_deployment_segment_activations(deployment) == []

    def test_entry_missing_segment_is_skipped(self) -> None:
        deployment = {"segment_deployments": [{"vni": 10100, "segment": None}]}
        assert _flatten_deployment_segment_activations(deployment) == []


# ===========================================================================
# BorderLeaf._extra_config()
# ===========================================================================


def _minimal_device_data(*, deployment: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": "bl-dc10101",
        "role": "border_leaf",
        "interfaces": [],
        "capabilities": [],
        "deployment": deployment or {},
    }


class TestBorderLeafExtraConfig:
    def test_customer_pbr_rules_removed_from_base_config(self) -> None:
        """The base class's customer_pbr_rules (always empty for border-leaf —
        no segment on its own interfaces) must not leak into the template
        context; border_leaf_pbr_rules replaces it."""
        transform = BorderLeaf.__new__(BorderLeaf)
        config = transform._extra_config(_minimal_device_data(), "cisco_nxos")
        assert "customer_pbr_rules" not in config
        assert "border_leaf_pbr_rules" in config

    def test_no_deployment_yields_no_pbr_rules(self) -> None:
        transform = BorderLeaf.__new__(BorderLeaf)
        config = transform._extra_config(_minimal_device_data(deployment=None), "cisco_nxos")
        assert config["border_leaf_pbr_rules"] == []

    def test_dc_wide_segment_activation_produces_pbr_rule(self) -> None:
        """Segment activated in this DC (via deployment.segment_deployments),
        not on this device's own interfaces — the whole point of the
        border-leaf-specific traversal."""
        deployment = {
            "segment_deployments": [
                {
                    "vni": 10100,
                    "segment": {
                        "id": "seg-1",
                        "customer_name": "web",
                        "security_tag": {"name": "web-tier", "group_id": 10},
                        "customer_deployments": [{"id": "dep-a"}],
                    },
                }
            ],
            "devices": [
                {
                    "capabilities": [
                        {
                            "typename": "ManagedFirewallHA",
                            "contexts": [
                                {
                                    "id": "ctx-1",
                                    "tenant": {"id": "dep-a"},
                                    "interface_capabilities": [
                                        {"device": {"role": "firewall"}, "ip_address": {"address": "10.65.0.0/30"}}
                                    ],
                                }
                            ],
                        }
                    ]
                }
            ],
        }
        transform = BorderLeaf.__new__(BorderLeaf)
        config = transform._extra_config(_minimal_device_data(deployment=deployment), "cisco_nxos")
        rules = config["border_leaf_pbr_rules"]
        assert len(rules) == 1
        assert rules[0]["match_by_tag"] is True
        assert rules[0]["sgt"] == 10
        assert rules[0]["fw_nexthop"] == "10.65.0.0"


# ===========================================================================
# border_leafs/cisco_nxos.j2 — border_leaf_pbr_rules rendering
# ===========================================================================


class TestCiscoNxosBorderLeafPbrTemplate:
    def _env(self) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)), undefined=jinja2.Undefined
        )

    def test_no_rules_renders_no_pbr_block(self) -> None:
        ctx = _minimal_ctx(border_leaf_pbr_rules=[])
        rendered = self._env().get_template("border_leafs/cisco_nxos.j2").render(**ctx)
        assert "feature pbr" not in rendered
        assert "route-map RM-BORDER-LEAF-PBR" not in rendered

    def test_tag_matched_rule_renders_match_cts_sgt(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[_pbr_rule(match_by_tag=True, sgt=10, fw_nexthop="10.65.0.0")],
            interfaces=[{"name": "Ethernet1/1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/cisco_nxos.j2").render(**ctx)
        assert "feature pbr" in rendered
        assert "system routing tcam security-group-acl" in rendered
        assert "match cts sgt 10" in rendered
        assert "set ip next-hop 10.65.0.0" in rendered
        assert "ip access-list" not in rendered

    def test_prefix_matched_rule_renders_acl_and_match_ip_address(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[
                _pbr_rule(
                    match_by_tag=False,
                    sgt=None,
                    acl_name="PBR-REDIRECT-web",
                    source_prefixes=["10.10.1.0/24"],
                    fw_nexthop="10.65.0.0",
                )
            ],
            interfaces=[{"name": "Ethernet1/1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/cisco_nxos.j2").render(**ctx)
        assert "ip access-list PBR-REDIRECT-web" in rendered
        assert "permit ip 10.10.1.0/24 any" in rendered
        assert "match ip address PBR-REDIRECT-web" in rendered
        assert "match cts sgt" not in rendered

    def test_multiple_prefixes_same_rule_render_multiple_permit_lines(self) -> None:
        """Same-customer segments merged into one rule — one ACL, multiple
        permit lines, one route-map sequence (TCAM-saving grouping)."""
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[
                _pbr_rule(
                    match_by_tag=False,
                    sgt=None,
                    acl_name="PBR-REDIRECT-web",
                    source_prefixes=["10.10.1.0/24", "10.10.2.0/24"],
                )
            ],
            interfaces=[{"name": "Ethernet1/1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/cisco_nxos.j2").render(**ctx)
        assert rendered.count("permit ip 10.10.1.0/24 any") == 1
        assert rendered.count("permit ip 10.10.2.0/24 any") == 1
        assert rendered.count("route-map RM-BORDER-LEAF-PBR permit") == 1

    def test_route_map_applied_only_on_uplink_interfaces(self) -> None:
        """The PBR block re-opens 'interface Ethernet1/1' a second time (NX-OS
        merges repeated interface blocks) to attach the route-map — it must
        never touch the firewall-role interface's own block."""
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[_pbr_rule()],
            interfaces=[
                {"name": "Ethernet1/1", "role": "uplink", "status": "active", "description": None},
                {"name": "Ethernet1/25", "role": "firewall", "status": "active", "description": None},
            ],
        )
        rendered = self._env().get_template("border_leafs/cisco_nxos.j2").render(**ctx)
        assert rendered.count("interface Ethernet1/1\n") == 2
        assert rendered.count("interface Ethernet1/25\n") == 1
        pbr_iface_block = rendered.split("ip policy route-map RM-BORDER-LEAF-PBR")[0].rsplit("interface ", 1)[1]
        assert pbr_iface_block.startswith("Ethernet1/1")

    def test_multiple_rules_get_increasing_sequence_numbers(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[
                _pbr_rule(customer_name="web", sgt=10),
                _pbr_rule(customer_name="db", sgt=20),
            ],
            interfaces=[{"name": "Ethernet1/1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/cisco_nxos.j2").render(**ctx)
        assert "route-map RM-BORDER-LEAF-PBR permit 10" in rendered
        assert "route-map RM-BORDER-LEAF-PBR permit 20" in rendered


# ===========================================================================
# border_leafs/arista_eos.j2 — border_leaf_pbr_rules rendering (MSS-G)
# ===========================================================================


class TestAristaEosBorderLeafPbrTemplate:
    def _env(self) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)), undefined=jinja2.Undefined
        )

    def test_no_rules_renders_no_pbr_block(self) -> None:
        ctx = _minimal_ctx(border_leaf_pbr_rules=[])
        rendered = self._env().get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "hardware macro-segmentation Service-Group" not in rendered
        assert "route-map RM-BORDER-LEAF-PBR" not in rendered

    def test_tag_matched_rule_renders_security_group_by_name(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[_pbr_rule(match_by_tag=True, sgt=10, sgt_name="web-tier", fw_nexthop="10.65.0.0")],
            interfaces=[{"name": "Ethernet1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "hardware macro-segmentation Service-Group" in rendered
        assert "security-group web-tier" in rendered
        assert "id 10" in rendered
        assert "match security-group web-tier" in rendered
        assert "set ip next-hop 10.65.0.0" in rendered

    def test_prefix_matched_rule_renders_acl_fallback(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[
                _pbr_rule(
                    match_by_tag=False,
                    sgt=None,
                    sgt_name=None,
                    acl_name="PBR-REDIRECT-web",
                    source_prefixes=["10.10.1.0/24"],
                )
            ],
            interfaces=[{"name": "Ethernet1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/arista_eos.j2").render(**ctx)
        assert "ip access-list PBR-REDIRECT-web" in rendered
        assert "permit ip 10.10.1.0/24 any" in rendered
        assert "match ip address PBR-REDIRECT-web" in rendered
        assert "match security-group" not in rendered

    def test_route_map_applied_only_on_uplink_interfaces(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[_pbr_rule()],
            interfaces=[
                {"name": "Ethernet1", "role": "uplink", "status": "active", "description": None},
                {"name": "Ethernet25", "role": "firewall", "status": "active", "description": None},
            ],
        )
        rendered = self._env().get_template("border_leafs/arista_eos.j2").render(**ctx)
        pbr_iface_block = rendered.split("ip policy route-map RM-BORDER-LEAF-PBR")[0].rsplit("interface ", 1)[1]
        assert pbr_iface_block.startswith("Ethernet1")


# ===========================================================================
# border_leafs/dell_sonic.j2 — border_leaf_pbr_rules rendering (prefix-only)
# ===========================================================================


class TestDellSonicBorderLeafPbrTemplate:
    """Dell SONiC has no hardware SGT/security-group primitive — every rule
    matches by source prefix, same as .dev/scenariusze.txt's own
    SONiC-BORDER-LEAF section (which uses IP-ACL matching exclusively)."""

    def _env(self) -> jinja2.Environment:
        return jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)), undefined=jinja2.Undefined
        )

    def test_no_rules_renders_no_pbr_block(self) -> None:
        ctx = _minimal_ctx(border_leaf_pbr_rules=[])
        rendered = self._env().get_template("border_leafs/dell_sonic.j2").render(**ctx)
        assert "route-map RM-BORDER-LEAF-PBR" not in rendered

    def test_prefix_matched_rule_renders_acl_and_route_map(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[
                _pbr_rule(
                    match_by_tag=False,
                    sgt=None,
                    sgt_name=None,
                    acl_name="PBR-REDIRECT-web",
                    source_prefixes=["10.10.1.0/24"],
                )
            ],
            interfaces=[{"name": "Ethernet1", "role": "uplink", "status": "active", "description": None}],
        )
        rendered = self._env().get_template("border_leafs/dell_sonic.j2").render(**ctx)
        assert "ip access-list PBR-REDIRECT-web" in rendered
        assert "seq 10 permit ip 10.10.1.0/24 any" in rendered
        assert "match ip address PBR-REDIRECT-web" in rendered
        assert "set ip next-hop 10.65.0.0" in rendered

    def test_route_map_applied_only_on_uplink_interfaces(self) -> None:
        ctx = _minimal_ctx(
            border_leaf_pbr_rules=[
                _pbr_rule(
                    match_by_tag=False,
                    sgt=None,
                    sgt_name=None,
                    acl_name="PBR-REDIRECT-web",
                    source_prefixes=["10.10.1.0/24"],
                )
            ],
            interfaces=[
                {"name": "Ethernet1", "role": "uplink", "status": "active", "description": None},
                {"name": "Ethernet25", "role": "firewall", "status": "active", "description": None},
            ],
        )
        rendered = self._env().get_template("border_leafs/dell_sonic.j2").render(**ctx)
        pbr_iface_block = rendered.split("ip policy route-map RM-BORDER-LEAF-PBR")[0].rsplit("interface ", 1)[1]
        assert pbr_iface_block.startswith("Ethernet1")
