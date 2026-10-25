"""Unit tests for the leaf templates rendered through BaseDeviceTransform (PBR rules, SONiC configdb, hyper spine)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import jinja2
import pytest
import yaml

from tests.unit.base_device_transform_helpers import _device_data, _make_transform, _render_leaf
from transforms.common import _loopback_name
from transforms.config.border_spine import BorderSpine
from utils.fabric_roles import BORDER_ROLES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestLeafPbrRules:
    @staticmethod
    def _render_pbr(platform: str, rules: list[dict]) -> str:
        """Render the leaf template of ``platform`` with only PBR rules."""
        template_dir = Path(__file__).parents[2] / "templates" / "configs"
        template = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir))).get_template(
            f"leafs/{platform}.j2"
        )
        return template.render(
            hostname="test-leaf",
            name="test-leaf",
            vlans=[],
            interfaces=[],
            loopback_name=_loopback_name([], platform),
            acls=[],
            vxlan=None,
            vrf_routes=[],
            bgp=[],
            ospf=[],
            mlag=None,
            leaf_pbr_rules=rules,
        )

    def test_nxos_template_attaches_one_ordered_policy_per_vlan(self) -> None:
        """NX-OS: bypass, then the firewall redirect, on one SVI, behind `feature pbr`."""
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}

        rendered = self._render_pbr("cisco_nxos", [customer])

        assert rendered.count("ip policy route-map RM-LEAF-PBR-VLAN100") == 1
        assert rendered.index("RM-LEAF-PBR-VLAN100 deny 20") < rendered.index("RM-LEAF-PBR-VLAN100 permit 30")
        assert "set ip next-hop 10.0.0.2" in rendered
        assert rendered.index("feature pbr") < rendered.index("route-map RM-LEAF-PBR-VLAN100")
        assert "TODO(lab): TCAM carving for PBR" in rendered

    def test_nxos_bypass_acl_is_permit_only_and_sequence_is_deny(self) -> None:
        """NX-OS: PBR ACLs hold no deny entries; bypass traffic skips PBR via a route-map deny sequence."""
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}

        rendered = self._render_pbr("cisco_nxos", [customer])

        assert "deny ip" not in rendered
        assert "permit ip any 10.0.2.0/24" in rendered
        assert "route-map RM-LEAF-PBR-VLAN100 permit 20" not in rendered
        assert "route-map RM-LEAF-PBR-VLAN100 deny 20\n  match ip address PBR-BYPASS-VLAN100" in rendered
        assert "documented-not-lab-verified for VXLAN-routed traffic" in rendered

    def test_nxos_without_bypass_prefixes_renders_no_bypass_acl_or_deny_sequence(self) -> None:
        """NX-OS: an empty bypass list must not render an empty ACL or a deny sequence."""
        customer = {"vlan_id": 200, "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"}

        rendered = self._render_pbr("cisco_nxos", [customer])

        assert "PBR-BYPASS-VLAN200" not in rendered
        assert "RM-LEAF-PBR-VLAN200 deny" not in rendered
        assert "RM-LEAF-PBR-VLAN200 permit 30" in rendered

    def test_nxos_template_without_pbr_has_no_pbr_feature_or_removed_stanzas(self) -> None:
        """No PBR rule: no `feature pbr`; the unsupported tcam/CTS lines are gone either way."""
        rendered = self._render_pbr("cisco_nxos", [])

        assert "feature pbr" not in rendered
        assert "tcam" not in rendered
        assert "cts" not in rendered

    def test_eos_template_uses_policy_map_type_pbr(self) -> None:
        """EOS: class-map/policy-map type pbr in bypass, firewall order, attached with service-policy."""
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}

        rendered = self._render_pbr("arista_eos", [customer])

        assert rendered.count("service-policy type pbr input PM-LEAF-PBR-VLAN100") == 1
        assert "route-map RM-LEAF-PBR" not in rendered
        assert "ip policy route-map" not in rendered
        assert "mac security" not in rendered
        assert rendered.index("class CM-PBR-BYPASS-VLAN100") < rendered.index("class CM-PBR-ALL-VLAN100")
        assert "class class-default" not in rendered
        assert "deny ip any any" not in rendered
        assert "set nexthop 10.0.0.2" in rendered
        assert "documented-not-lab-verified" in rendered

    def test_eos_template_without_pbr_renders_no_policy_map(self) -> None:
        """No PBR rule: EOS renders neither PBR nor the removed MACsec stanzas."""
        rendered = self._render_pbr("arista_eos", [])

        assert "policy-map type pbr" not in rendered
        assert "mac security" not in rendered

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf"])
    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_sonic_leaf_accepts_firewall_pbr(self, platform: str, role: str) -> None:
        """SONiC renders the firewall redirect (ConfigDB PBR ACL table), so the rule reaches the template."""
        customer = {"vlan_id": 100, "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"}
        transform = _make_transform(role)

        with (
            patch("transforms.common.get_customer_pbr_rules", return_value=[customer]),
            patch("transforms.common.get_leaf_firewall_transits", return_value=[]),
        ):
            config = transform._extra_config(_device_data(), platform)

        assert config["leaf_pbr_rules"] == [customer]

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "dell_sonic"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_refuses_tenant_svi_with_pbr(self, platform: str, role: str) -> None:
        """No border_leafs template renders PBR: a tenant SVI that needs it must not ship unprotected."""
        customer = {"vlan_id": 100, "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"}
        transform = _make_transform(role)

        with patch("transforms.common.get_customer_pbr_rules", return_value=[customer]):
            with pytest.raises(ValueError, match=rf"{role} cannot render PBR policy for VLANs \[100\]"):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "dell_sonic"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_refuses_tenant_svi_acl_it_cannot_render(self, platform: str, role: str) -> None:
        """No border_leafs template renders the SVI ACL definitions (NX-OS and EOS only bind the name): refuse."""
        transform = _make_transform(role)

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]):
            with pytest.raises(ValueError, match=rf"{role} cannot render the SVI ACL for VLANs \[100\]"):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["cisco_nxos", "sonic"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_without_tenant_policy_renders(self, platform: str, role: str) -> None:
        """A border leaf or spine with no ACL/PBR need (transit legs only) is not refused."""
        config = _make_transform(role)._extra_config(_device_data(), platform)

        assert config["acls"] == []
        assert config["leaf_pbr_rules"] == []

    @staticmethod
    def _gateway_activation(vlan_id: int, *, border_gateway: bool) -> dict:
        """A routed activation (anycast gateway in PROD, L2 VNI 10000 + vlan, L3 VNI 50000)."""
        return {
            "vlan_id": vlan_id,
            "vni": 10000 + vlan_id,
            "segment": {
                "id": f"seg-{vlan_id}",
                "customer_name": f"seg-{vlan_id}",
                "gateway": {
                    "address": f"10.{vlan_id}.0.1/24",
                    "ip_prefix": {"prefix": f"10.{vlan_id}.0.0/24", "ip_namespace": {"name": "PROD", "l3_vni": 50000}},
                },
            },
            **({"border_gateway": True} if border_gateway else {}),
        }

    @staticmethod
    def _policy_for_every_activation() -> tuple:
        """Patches giving each activation handed to the policy helpers a PBR rule and an ACL."""
        return (
            patch(
                "transforms.common.get_customer_pbr_rules",
                side_effect=lambda acts, _ctx: [
                    {"vlan_id": a["vlan_id"], "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"} for a in acts
                ],
            ),
            patch(
                "transforms.common.get_acls",
                side_effect=lambda activations: [
                    {"vlan_id": a["vlan_id"], "name": f"ACL-{a['vlan_id']}"} for a in activations
                ],
            ),
        )

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_gateway_activations_render_without_tenant_policy(self, platform: str, role: str) -> None:
        """A BGW's hostless stitched segments keep their VLAN, VNI and SVI but are no ACL/PBR input."""
        data = _device_data()
        data["segment_deployments"] = [self._gateway_activation(100, border_gateway=True)]
        pbr, acl = self._policy_for_every_activation()

        with pbr, acl:
            config = _make_transform(role)._extra_config(data, platform)

        assert config["leaf_pbr_rules"] == []
        assert config["acls"] == []
        assert [(v["vlan_id"], v["gateway_ip"], v["vrf"], v["acl_name"]) for v in config["vlans"]] == [
            (100, "10.100.0.1/24", "PROD", None)
        ]
        assert [m["vni"] for m in config["vxlan"]["l2_vni_mappings"]] == [10100]

    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_refuses_host_capable_activation_pbr_beside_border_gateway(self, role: str) -> None:
        """Only the BGW activations are exempt: an interface-derived one on the same border still needs PBR."""
        data = _device_data()
        data["segment_deployments"] = [
            self._gateway_activation(100, border_gateway=True),
            self._gateway_activation(101, border_gateway=False),
        ]
        pbr, acl = self._policy_for_every_activation()

        with pbr, acl, pytest.raises(ValueError, match=rf"{role} cannot render PBR policy for VLANs \[101\]"):
            _make_transform(role)._extra_config(data, "cisco_nxos")

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_refuses_host_capable_activation_acl_beside_border_gateway(
        self, platform: str, role: str
    ) -> None:
        """Same for the SVI ACL, on every platform: the border_leafs templates render none."""
        data = _device_data()
        data["segment_deployments"] = [
            self._gateway_activation(100, border_gateway=True),
            self._gateway_activation(101, border_gateway=False),
        ]

        with patch("transforms.common.get_customer_pbr_rules", return_value=[]):
            _, acl = self._policy_for_every_activation()
            with acl, pytest.raises(ValueError, match=rf"{role} cannot render the SVI ACL for VLANs \[101\]"):
                _make_transform(role)._extra_config(data, platform)

    def test_edge_keeps_border_gateway_activations_as_policy_input(self) -> None:
        """The exemption is for border roles only: an edge hands every routed activation to the policy helpers."""
        data = _device_data()
        data["segment_deployments"] = [self._gateway_activation(100, border_gateway=True)]
        pbr, acl = self._policy_for_every_activation()

        with pbr, acl:
            config = _make_transform("edge")._extra_config(data, "cisco_nxos")

        assert [r["vlan_id"] for r in config["leaf_pbr_rules"]] == [100]
        assert [a["vlan_id"] for a in config["acls"]] == [100]

    def test_border_gateway_activations_are_flagged_at_collection(self) -> None:
        """_collect_border_gateway_activations marks what it adds; interface-derived activations stay unflagged."""
        transform = _make_transform("border-leaf")
        deployment = {
            "segment_deployments": [
                {"vni": 10100, "segment": {"id": "seg-100", "stretch_scope": "dc_pair", "vlan_domain": None}}
            ]
        }
        with patch.object(transform, "_own_vlan_domain_segment", return_value={"vlan_id": 100}):
            found = transform._collect_border_gateway_activations(
                deployment, device_id="dev-1", device_capabilities=[], seen=set()
            )

        assert [(a["vlan_id"], a["border_gateway"]) for a in found] == [(100, True)]

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_real_border_spine_class_refuses_unprotected_tenant_svi(self, platform: str) -> None:
        """BorderSpine itself (not just the role string) is refused when its tenant SVI would lose its ACL."""
        transform = BorderSpine.__new__(BorderSpine)
        transform.root_directory = "/fake/root"

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]):
            with pytest.raises(ValueError, match=r"border-spine cannot render the SVI ACL for VLANs \[100\]"):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["sonic"])
    def test_spine_is_not_subject_to_the_border_refusal(self, platform: str) -> None:
        """Failure-path guard: the refusal is for border roles only (a spine renders no tenant SVI)."""
        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]):
            _make_transform("spine")._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_sonic_template_renders_ordered_pbr_table_per_vlan(self, platform: str) -> None:
        """ConfigDB PBR mirrors the route-map order: bypass > firewall redirect."""
        customer = {
            "vlan_id": 100,
            "customer_name": "c001",
            "environment": "prod",
            "bypass_prefixes": ["10.0.2.0/24", "10.0.3.0/24"],
            "fw_nexthop": "10.0.0.2",
        }

        config = json.loads(_render_leaf(platform, leaf_pbr_rules=[customer]))

        assert config["ACL_TABLE"]["PBR-VLAN100"] == {
            "type": "PBR",
            "policy_desc": "Leaf PBR for c001/prod",
            "ports": ["Vlan100"],
            "stage": "ingress",
        }
        rules = {k: v for k, v in config["ACL_RULE"].items() if k.startswith("PBR-VLAN100|")}
        assert rules == {
            "PBR-VLAN100|BYPASS_1": {
                "PRIORITY": "7999",
                "IP_TYPE": "IPV4ANY",
                "DST_IP": "10.0.2.0/24",
                "PACKET_ACTION": "FORWARD",
            },
            "PBR-VLAN100|BYPASS_2": {
                "PRIORITY": "7998",
                "IP_TYPE": "IPV4ANY",
                "DST_IP": "10.0.3.0/24",
                "PACKET_ACTION": "FORWARD",
            },
            "PBR-VLAN100|FW_REDIRECT": {"PRIORITY": "1000", "IP_TYPE": "IPV4ANY", "PACKET_ACTION": "REDIRECT:10.0.0.2"},
        }

    def test_sonic_template_without_pbr_rules_has_no_pbr_table(self) -> None:
        """No rules (or a role that never passes them) adds no PBR table."""
        config = json.loads(_render_leaf("sonic"))

        assert not [k for k in config.get("ACL_TABLE", {}) if k.startswith("PBR-")]


@pytest.mark.parametrize("role", ["leafs", "border_leafs", "l2_leafs", "spines", "super_spines"])
@pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
def test_sonic_templates_render_pure_configdb(role: str, platform: str) -> None:
    """Every SONiC switch role renders one JSON ConfigDB with the empty NTP/SYSLOG tables."""
    template_dir = Path(__file__).parents[2] / "templates" / "configs"
    template = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir))).get_template(
        f"{role}/{platform}.j2"
    )
    rendered = template.render(
        hostname="test-switch",
        vlans=[],
        interfaces=[],
        acls=[],
        vxlan=None,
        vrf_routes=[],
        bgp=[],
        ospf=[],
        mlag=None,
    )
    config = json.loads(rendered)
    assert config["NTP_SERVER"] == {}
    assert config["SYSLOG_SERVER"] == {}


def test_hyper_spine_artifact_uses_super_spine_transform(root_dir: Path) -> None:
    """Hyper-spines share the super-spine rendering path but need their own target group."""
    registry = yaml.safe_load((root_dir / ".infrahub.yml").read_text())
    artifacts = registry["artifact_definitions"]
    assert any(
        artifact["targets"] == "hyper-spines"
        and artifact["transformation"] == "super_spine"
        and artifact["parameters"] == {"device": "name__value"}
        for artifact in artifacts
    )
