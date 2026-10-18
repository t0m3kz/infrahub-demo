"""Unit tests for BaseDeviceTransform and ToR transform.

Covers:
- transform()  – data routing, platform detection, activation injection
- _build_config() – base context keys (interfaces, bgp, ospf, capabilities)
- _extra_config() – vlans/vxlan/acls/vrf_routes when device_role is set
- ToR class attributes (device_role="tor", template_subdir="leafs")
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import pytest
import yaml

from transforms.common import BaseDeviceTransform, _loopback_name, get_capabilities
from transforms.config.border_spine import BorderSpine
from transforms.config.tor import ToR
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

    def test_unsupported_leaf_refuses_firewall_only_pbr(self) -> None:
        """An SR OS leaf must not silently omit a firewall redirect."""
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_customer_pbr_rules", return_value=[customer]),
            patch("transforms.common.get_leaf_firewall_transits", return_value=[]),
        ):
            with pytest.raises(ValueError, match="cannot render PBR policy"):
                transform._extra_config(_device_data(), "nokia_sros")

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

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "dell_sonic", "nokia_sros"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_refuses_tenant_svi_with_pbr(self, platform: str, role: str) -> None:
        """No border_leafs template renders PBR: a tenant SVI that needs it must not ship unprotected."""
        customer = {"vlan_id": 100, "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"}
        transform = _make_transform(role)

        with patch("transforms.common.get_customer_pbr_rules", return_value=[customer]):
            with pytest.raises(ValueError, match=rf"{role} cannot render PBR policy for VLANs \[100\]"):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "dell_sonic", "nokia_sros"])
    @pytest.mark.parametrize("role", sorted(BORDER_ROLES))
    def test_border_role_refuses_tenant_svi_acl_it_cannot_render(self, platform: str, role: str) -> None:
        """No border_leafs template renders the SVI ACL definitions (NX-OS and EOS only bind the name): refuse."""
        transform = _make_transform(role)

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]):
            with pytest.raises(ValueError, match=rf"{role} cannot render the SVI ACL for VLANs \[100\]"):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["cisco_nxos", "sonic", "nokia_sros"])
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

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "nokia_sros"])
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

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "nokia_sros"])
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

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic", "nokia_sros"])
    def test_real_border_spine_class_refuses_unprotected_tenant_svi(self, platform: str) -> None:
        """BorderSpine itself (not just the role string) is refused when its tenant SVI would lose its ACL."""
        transform = BorderSpine.__new__(BorderSpine)
        transform.root_directory = "/fake/root"

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]):
            with pytest.raises(ValueError, match=r"border-spine cannot render the SVI ACL for VLANs \[100\]"):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["sonic", "nokia_sros"])
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


def _render_leaf(platform: str, **overrides: object) -> str:
    """Render leafs/<platform>.j2 with an empty leaf context plus overrides."""
    template_dir = Path(__file__).parents[2] / "templates" / "configs"
    template = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir))).get_template(
        f"leafs/{platform}.j2"
    )
    context: dict[str, object] = {
        "hostname": "test-leaf",
        "vlans": [],
        "interfaces": [],
        "loopback_name": _loopback_name([], platform),
        "acls": [],
        "vxlan": None,
        "vrf_routes": [],
        "bgp": [],
        "ospf": [],
        "mlag": None,
        **overrides,
    }
    return template.render(**context)


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


def _make_transform(device_role: str = "") -> BaseDeviceTransform:
    """Instantiate BaseDeviceTransform bypassing InfrahubTransform __init__."""
    t = BaseDeviceTransform.__new__(BaseDeviceTransform)
    t.device_role = device_role
    t.template_subdir = "leafs"
    t.root_directory = "/fake/root"
    return t


def _make_tor() -> ToR:
    t = ToR.__new__(ToR)
    t.root_directory = "/fake/root"
    return t


def _device_data(
    *,
    platform: str | None = "arista_eos",
    name: str = "leaf-01",
    role: str = "leaf",
    interfaces: list | None = None,
    device_capabilities: list | None = None,
) -> dict:
    return {
        "name": name,
        "role": role,
        "platform": {"netmiko_device_type": platform} if platform else None,
        "interfaces": interfaces or [],
        "capabilities": device_capabilities or [],
    }


# ---------------------------------------------------------------------------
# get_capabilities (already covered in test_get_capabilities.py, complementary)
# ---------------------------------------------------------------------------


class TestGetCapabilitiesEdgeCases:
    def test_empty_dict_returns_false_false(self) -> None:
        result = get_capabilities({})
        assert result == {"bgp_enabled": False, "ospf_enabled": False}

    def test_unknown_service_type_ignored(self) -> None:
        result = get_capabilities({"capabilities": [{"typename": "UnknownService"}]})
        assert result["bgp_enabled"] is False
        assert result["ospf_enabled"] is False

    def test_multiple_bgp_entries_still_true_once(self) -> None:
        result = get_capabilities(
            {
                "capabilities": [
                    {"typename": "ManagedBGP"},
                    {"typename": "ManagedBGP"},
                ]
            }
        )
        assert result["bgp_enabled"] is True


# ---------------------------------------------------------------------------
# _build_config
# ---------------------------------------------------------------------------


class TestBuildConfig:
    def test_returns_required_keys(self) -> None:
        t = _make_transform()
        data = _device_data()
        cfg = t._build_config(data, "arista_eos")
        for key in ("name", "hostname", "device_role", "interfaces", "bgp", "ospf", "capabilities"):
            assert key in cfg

    def test_name_and_hostname_match_device_name(self) -> None:
        t = _make_transform()
        data = _device_data(name="spine-01")
        cfg = t._build_config(data, "arista_eos")
        assert cfg["name"] == "spine-01"
        assert cfg["hostname"] == "spine-01"

    def test_device_role_from_data(self) -> None:
        t = _make_transform()
        data = _device_data(role="spine")
        cfg = t._build_config(data, "arista_eos")
        assert cfg["device_role"] == "spine"

    def test_capabilities_bgp_true_when_service_present(self) -> None:
        t = _make_transform()
        data = _device_data(
            device_capabilities=[
                {
                    "typename": "ManagedBGP",
                    "local_as": {"asn": 65001},
                    "router_id": {"address": "10.0.0.1/32"},
                    "peerings": [],
                }
            ]
        )
        cfg = t._build_config(data, "arista_eos")
        assert cfg["capabilities"]["bgp_enabled"] is True
        assert cfg["capabilities"]["ospf_enabled"] is False

    def test_capabilities_both_false_when_no_services(self) -> None:
        t = _make_transform()
        data = _device_data()
        cfg = t._build_config(data, "arista_eos")
        assert cfg["capabilities"] == {"bgp_enabled": False, "ospf_enabled": False}


# ---------------------------------------------------------------------------
# _extra_config
# ---------------------------------------------------------------------------


class TestExtraConfig:
    def test_no_device_role_returns_empty(self) -> None:
        t = _make_transform(device_role="")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result == {}

    def test_with_device_role_returns_required_keys(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        for key in ("vlans", "vxlan", "acls", "vrf_routes"):
            assert key in result

    def test_no_activations_yields_empty_vlans(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result["vlans"] == []

    def test_no_activations_yields_empty_acls(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result["acls"] == []

    def test_no_activations_vrf_routes_empty(self) -> None:
        t = _make_transform(device_role="leaf")
        result = t._extra_config(_device_data(), "arista_eos")
        assert result["vrf_routes"] == []


# ---------------------------------------------------------------------------
# transform() – data routing
# ---------------------------------------------------------------------------


class TestTransformDataRouting:
    @pytest.mark.asyncio
    async def test_no_platform_returns_comment(self) -> None:
        t = _make_transform()
        # Clean data with no platform
        with patch("transforms.common.clean_data") as mock_clean:
            mock_clean.return_value = {"DcimPhysicalDevice": [_device_data(platform=None, name="no-platform-dev")]}
            result = await t.transform({"raw": "data"})
        assert "no-platform-dev" in result
        assert "No configuration generated" in result

    @pytest.mark.asyncio
    async def test_activation_from_capabilities(self) -> None:
        """Activations on segment.segment_deployments within capabilities are collected."""
        t = _make_transform(device_role="leaf")
        interfaces = [
            {
                "name": "Ethernet10",
                "capabilities": [
                    {
                        "id": "seg-1",
                        "typename": "ManagedVxlanSegment",
                        "name": "seg-100",
                        "customer_name": "seg-100",
                        "segment_deployments": [{"vlan_id": 100, "vni": 10100}],
                    }
                ],
            }
        ]
        device = _device_data(interfaces=interfaces)

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        rendered_kwargs = fake_template.render.call_args[1]
        assert rendered_kwargs.get("vlans") is not None

    @pytest.mark.asyncio
    async def test_vlan_segment_activation_collected(self) -> None:
        """ManagedVlanSegment.vlan_id is a plain manual attribute directly on
        the segment (no realization record). An active VLAN activation is
        still collected."""
        t = _make_transform(device_role="leaf")
        interfaces = [
            {
                "name": "Ethernet10",
                "capabilities": [
                    {
                        "id": "seg-1",
                        "typename": "ManagedVlanSegment",
                        "name": "seg-100",
                        "customer_name": "seg-100",
                        "status": "active",
                        "vlan_id": 100,
                    }
                ],
            }
        ]
        device = _device_data(interfaces=interfaces)

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        rendered_kwargs = fake_template.render.call_args[1]
        assert rendered_kwargs.get("vlans")

    @pytest.mark.asyncio
    async def test_vlan_segment_non_active_status_excluded(self) -> None:
        """A VLAN segment whose status is not active/provisioning (e.g.
        decommissioned) is excluded — VlanSegment has no server-side status
        filter (it's a plain attribute, not a filterable relationship), so
        this must be enforced client-side."""
        t = _make_transform(device_role="leaf")
        interfaces = [
            {
                "name": "Ethernet10",
                "capabilities": [
                    {
                        "id": "seg-1",
                        "typename": "ManagedVlanSegment",
                        "name": "seg-100",
                        "status": "decommissioned",
                        "vlan_id": 100,
                    }
                ],
            }
        ]
        device = _device_data(interfaces=interfaces)

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        rendered_kwargs = fake_template.render.call_args[1]
        assert rendered_kwargs.get("vlans") == []

    @pytest.mark.asyncio
    async def test_activation_missing_yields_no_vlans(self) -> None:
        """No segment_deployments on capabilities → vlans is empty in rendered config."""
        t = _make_transform(device_role="leaf")
        # Interfaces with no capabilities carrying segment_deployments
        device = _device_data(interfaces=[{"name": "Ethernet1", "capabilities": []}])

        fake_template = MagicMock()
        fake_template.render.return_value = "! rendered"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            await t.transform({"raw": "data"})

        rendered_kwargs = fake_template.render.call_args[1]
        assert rendered_kwargs.get("vlans") == []

    @pytest.mark.asyncio
    async def test_template_render_called_with_config_keys(self) -> None:
        t = _make_transform(device_role="")
        device = _device_data()

        fake_template = MagicMock()
        fake_template.render.return_value = "! config"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [device]}
            result = await t.transform({"raw": "data"})

        assert result == "! config"
        call_kwargs = fake_template.render.call_args[1]
        assert "name" in call_kwargs
        assert "bgp" in call_kwargs
        assert "ospf" in call_kwargs

    @pytest.mark.asyncio
    async def test_extra_roots_passed_through(self) -> None:
        """Extra GQL root keys are forwarded to _extra_config via extra_roots."""
        t = _make_transform(device_role="leaf")
        device = _device_data()

        fake_template = MagicMock()
        fake_template.render.return_value = "! config"

        extra_key_value = [{"id": "fw-1"}]
        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
            patch.object(t, "_extra_config", return_value={}) as mock_extra,
        ):
            mock_clean.return_value = {
                "DcimPhysicalDevice": [device],
                "DcimFirewallInterface": extra_key_value,
            }
            await t.transform({"raw": "data"})

        _, extra_kwargs = mock_extra.call_args
        assert "extra_roots" in extra_kwargs
        assert extra_kwargs["extra_roots"].get("DcimFirewallInterface") == extra_key_value


# ---------------------------------------------------------------------------
# _resolve_own_vlan_domain_id / _collect_activations_from_interfaces
# ---------------------------------------------------------------------------


class TestResolveOwnVlanDomainId:
    """A local VLAN ID is allocated per VLAN DOMAIN, not DC-wide. An MLAG pair is
    one domain — both members must resolve to the ManagedMLAG id, or each half
    picks a different local VLAN for the same stretched segment and the
    port-channel between them carries mismatched tags.
    """

    def test_mlag_capability_wins_over_the_device_id(self) -> None:
        resolved = BaseDeviceTransform._resolve_own_vlan_domain_id(
            "dev-leaf-01", [{"typename": "ManagedMLAG", "id": "mlag-domain-1"}]
        )
        assert resolved == "mlag-domain-1"

    def test_both_members_of_a_pair_resolve_to_the_same_domain(self) -> None:
        caps = [{"typename": "ManagedMLAG", "id": "mlag-domain-1"}]
        assert BaseDeviceTransform._resolve_own_vlan_domain_id(
            "dev-leaf-01", caps
        ) == BaseDeviceTransform._resolve_own_vlan_domain_id("dev-leaf-02", caps)

    def test_standalone_device_is_its_own_domain(self) -> None:
        resolved = BaseDeviceTransform._resolve_own_vlan_domain_id(
            "dev-leaf-01", [{"typename": "ManagedBGP", "id": "bgp-1"}]
        )
        assert resolved == "dev-leaf-01"

    def test_standalone_vlan_domain_capability_is_the_domain(self) -> None:
        """A non-MLAG switch's activations point at its ManagedStandaloneVlanDomain,
        not at the device, so the domain id must win over the device id."""
        resolved = BaseDeviceTransform._resolve_own_vlan_domain_id(
            "dev-leaf-01",
            [{"typename": "ManagedBGP", "id": "bgp-1"}, {"typename": "ManagedStandaloneVlanDomain", "id": "svd-1"}],
        )
        assert resolved == "svd-1"

    def test_mlag_capability_wins_over_a_standalone_domain(self) -> None:
        """A device left with a stale standalone domain after pairing still uses its MLAG."""
        resolved = BaseDeviceTransform._resolve_own_vlan_domain_id(
            "dev-leaf-01",
            [{"typename": "ManagedStandaloneVlanDomain", "id": "svd-1"}, {"typename": "ManagedMLAG", "id": "mlag-1"}],
        )
        assert resolved == "mlag-1"

    def test_no_capabilities_falls_back_to_the_device_id(self) -> None:
        assert BaseDeviceTransform._resolve_own_vlan_domain_id("dev-leaf-01", []) == "dev-leaf-01"

    def test_mlag_capability_without_an_id_is_ignored(self) -> None:
        """An MLAG capability the query did not select an id for would otherwise
        resolve the domain to None and silently drop every VXLAN segment."""
        resolved = BaseDeviceTransform._resolve_own_vlan_domain_id("dev-leaf-01", [{"typename": "ManagedMLAG"}])
        assert resolved == "dev-leaf-01"


class TestCollectActivationsVlanDomainMatching:
    """A VXLAN segment's local vlan_id comes from the vlan_domain_segments entry
    matching THIS device's own domain."""

    @staticmethod
    def _vxlan_iface(vlan_domain_id: str, vlan_id: int = 100) -> dict:
        return {
            "capabilities": [
                {
                    "typename": "ManagedVxlanSegment",
                    "id": "seg-1",
                    "name": "tenant-a-web",
                    "segment_deployments": [{"vni": 10100}],
                    "vlan_domain_segments": [{"vlan_domain": {"id": vlan_domain_id}, "vlan_id": vlan_id}],
                }
            ]
        }

    def test_mlag_pair_reads_the_domain_entry_not_a_device_entry(self) -> None:
        t = _make_transform("leaf")
        activations = t._collect_activations_from_interfaces(
            [self._vxlan_iface("mlag-domain-1", vlan_id=250)],
            device_id="dev-leaf-01",
            device_capabilities=[{"typename": "ManagedMLAG", "id": "mlag-domain-1"}],
        )
        assert [a["vlan_id"] for a in activations] == [250]

    def test_standalone_device_reads_its_own_entry(self) -> None:
        t = _make_transform("leaf")
        activations = t._collect_activations_from_interfaces(
            [self._vxlan_iface("dev-leaf-01", vlan_id=110)],
            device_id="dev-leaf-01",
            device_capabilities=[],
        )
        assert [a["vlan_id"] for a in activations] == [110]

    def test_segment_with_no_entry_for_this_domain_is_skipped(self) -> None:
        """Allocation has not converged for this domain yet. Skipping beats
        rendering an SVI on a VLAN this device never allocated."""
        t = _make_transform("leaf")
        activations = t._collect_activations_from_interfaces(
            [self._vxlan_iface("some-other-domain")],
            device_id="dev-leaf-01",
            device_capabilities=[],
        )
        assert activations == []


# ---------------------------------------------------------------------------
# ToR class attributes
# ---------------------------------------------------------------------------


class TestToRClassAttributes:
    def test_device_role_is_tor(self) -> None:
        assert ToR.device_role == "tor"

    def test_template_subdir_is_leafs(self) -> None:
        assert ToR.template_subdir == "leafs"

    def test_query_is_leaf_config(self) -> None:
        assert ToR.query == "leaf_config"

    def test_inherits_base_device_transform(self) -> None:
        assert issubclass(ToR, BaseDeviceTransform)

    def test_extra_config_includes_vxlan_keys(self) -> None:
        """ToR uses device_role='tor' so _extra_config returns vxlan/vlans/acls."""
        t = _make_tor()
        result = t._extra_config(_device_data(), "arista_eos")
        assert "vlans" in result
        assert "vxlan" in result
        assert "acls" in result

    def test_extra_config_empty_activations_no_vlans(self) -> None:
        t = _make_tor()
        data = _device_data()  # no segment_deployments
        result = t._extra_config(data, "arista_eos")
        assert result["vlans"] == []

    @pytest.mark.asyncio
    async def test_transform_no_platform_returns_comment(self) -> None:
        t = _make_tor()
        with patch("transforms.common.clean_data") as mock_clean:
            mock_clean.return_value = {"DcimPhysicalDevice": [_device_data(platform=None, name="tor-01")]}
            result = await t.transform({"raw": "data"})
        assert "tor-01" in result
        assert "No configuration generated" in result

    @pytest.mark.asyncio
    async def test_transform_with_platform_renders_template(self) -> None:
        t = _make_tor()
        fake_template = MagicMock()
        fake_template.render.return_value = "! tor config"

        with (
            patch("transforms.common.clean_data") as mock_clean,
            patch.object(t, "_load_template", return_value=fake_template),
        ):
            mock_clean.return_value = {"DcimPhysicalDevice": [_device_data(role="tor")]}
            result = await t.transform({"raw": "data"})

        assert result == "! tor config"
        kwargs = fake_template.render.call_args[1]
        assert kwargs["device_role"] == "tor"
        # ToR has device_role set → extra config keys present
        assert "vlans" in kwargs
        assert "vxlan" in kwargs


# ---------------------------------------------------------------------------
# SR OS: segment ACL binding (SAP ingress filter per VLAN of every port)
# ---------------------------------------------------------------------------


def _sros_vxlan(vlan_ids: list[int]) -> dict:
    """A minimal VXLAN context with one L2 VNI mapping (vni = 10000 + vlan) per VLAN."""
    return {
        "enabled": True,
        "vtep": {"ipv4": "10.0.0.1"},
        "evpn": {"enabled": True, "rd_format": None},
        "anycast_gateway": None,
        "l3_vni_mappings": [],
        "l2_vni_mappings": [{"vlan_id": v, "vni": 10000 + v, "route_target": None} for v in vlan_ids],
    }


class TestSrosAclBinding:
    def test_trunk_port_gets_a_sap_and_filter_for_every_vlan(self) -> None:
        """A trunk carrying two VLANs binds each VLAN's ip-filter on its own SAP."""
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100, 200]}],
            acls=[{"vlan_id": 100, "name": "ACL-100", "rules": []}, {"vlan_id": 200, "name": "ACL-200", "rules": []}],
            vxlan=_sros_vxlan([100, 200]),
        )

        assert 'vpls "vpls-10100" sap 1/1/10:100 ingress ip-filter "100"' in rendered
        assert 'vpls "vpls-10200" sap 1/1/10:200 ingress ip-filter "200"' in rendered

    def test_vlan_without_acl_still_gets_a_sap_without_filter(self) -> None:
        """A non-first VLAN of a trunk is bridged even when no ACL protects it."""
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100, 200]}],
            vxlan=_sros_vxlan([100, 200]),
        )

        assert 'vpls "vpls-10200" sap 1/1/10:200 create' in rendered
        assert "ingress ip-filter" not in rendered

    def test_port_only_joins_the_vpls_of_its_own_vlans(self) -> None:
        """A port that does not carry the VLAN gets no SAP in that VLAN's service."""
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100]}],
            vxlan=_sros_vxlan([100, 200]),
        )

        assert "sap 1/1/10:100 create" in rendered
        assert "sap 1/1/10:200" not in rendered

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf"])
    def test_leaf_refuses_acl_for_vlan_without_l2_vni_service(self, role: str) -> None:
        """A VLAN-only segment ACL has no vpls to attach to: refuse instead of leaving it unbound."""
        transform = _make_transform(role)

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 200, "name": "ACL-200"}]):
            with pytest.raises(ValueError, match=r"cannot bind the segment ACL for VLANs \[200\]"):
                transform._extra_config(_device_data(), "nokia_sros")

    def test_leaf_accepts_acl_for_vlan_with_l2_vni_service(self) -> None:
        """An ACL whose VLAN has an L2 VNI mapping is bound by the template, so no refusal."""
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]),
            patch("transforms.common.get_vxlan_config", return_value=_sros_vxlan([100])),
        ):
            config = transform._extra_config(_device_data(), "nokia_sros")

        assert config["acls"] == [{"vlan_id": 100, "name": "ACL-100"}]

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos"])
    def test_other_platforms_accept_vlan_only_acl(self, platform: str) -> None:
        """Only the SR OS template binds ACLs through VNI services; other leaf templates are unaffected."""
        transform = _make_transform("leaf")

        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 200, "name": "ACL-200"}]):
            config = transform._extra_config(_device_data(), platform)

        assert config["acls"] == [{"vlan_id": 200, "name": "ACL-200"}]


# ---------------------------------------------------------------------------
# SR OS: IRB list (anycast gateway) computed once in Python
# ---------------------------------------------------------------------------


def _gateway_vlan(vlan_id: int, **extra: object) -> dict:
    return {"vlan_id": vlan_id, "name": f"VLAN{vlan_id}", "gateway_ip": f"10.0.{vlan_id}.1/24", **extra}


def _sros_vxlan_with_l3(vlan_ids: list[int], l3_vni: int | None = 50001, extra_l3: tuple[int, ...] = ()) -> dict:
    """The SR OS VXLAN context whose VLANs sit in the VRF of ``l3_vni`` (a vprn service) plus ``extra_l3`` vprns."""
    vxlan = _sros_vxlan(vlan_ids)
    for mapping in vxlan["l2_vni_mappings"]:
        mapping["l3_vni"] = l3_vni
    vxlan["l3_vni_mappings"] = [
        {"vrf_name": f"VRF{vni}", "l3_vni": vni} for vni in ([l3_vni] if l3_vni else []) + list(extra_l3)
    ]
    return vxlan


class TestSrosIrbs:
    def test_vni_and_vprn_come_from_the_vxlan_mapping_of_the_vlan(self) -> None:
        """vpls = the L2 VNI, vprn = the VRF's L3 VNI; the gateway addresses ride along."""
        vlan = _gateway_vlan(100, gateway_ipv6="2001:db8::1/64")

        irbs = BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [vlan], _sros_vxlan_with_l3([100]))

        assert irbs == [
            {
                "vlan_id": 100,
                "vni": 10100,
                "vprn": 50001,
                "gateway_ip": "10.0.100.1/24",
                "gateway_ipv6": "2001:db8::1/64",
            }
        ]

    def test_transit_vlan_explicit_vnis_win_over_the_mapping(self) -> None:
        """A transit VLAN carries its own vni and l3_vni (transforms/common.py sets them)."""
        vlan = _gateway_vlan(3001, vni=64001, l3_vni=50003)

        irbs = BaseDeviceTransform._sros_irbs(
            "border-leaf", "nokia_sros", [vlan], _sros_vxlan_with_l3([3001], extra_l3=(50003,))
        )

        assert [(irb["vni"], irb["vprn"]) for irb in irbs] == [(64001, 50003)]

    def test_namespace_without_l3_vni_is_refused_not_given_a_vprn_numbered_like_the_vpls(self) -> None:
        """No L3 VNI mapping means no vprn: the IRB would reference a service nothing creates."""
        vlan = {"vlan_id": 100, "gateway_ip": "10.0.0.1/24", "gateway_ipv6": None, "vrf": None}
        vxlan = {"l2_vni_mappings": [{"vlan_id": 100, "vni": 10100, "l3_vni": None}], "l3_vni_mappings": []}

        with pytest.raises(
            ValueError, match=r"cannot render the gateway of VLAN 100 \(no vpls/vprn to attach the IRB to\)"
        ):
            BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [vlan], vxlan)

    def test_a_vprn_that_no_l3_vni_mapping_creates_is_refused(self) -> None:
        """A VLAN naming an L3 VNI of another VRF than the device's mappings creates is refused too."""
        with pytest.raises(ValueError, match=r"cannot render the gateway of VLAN 100"):
            BaseDeviceTransform._sros_irbs(
                "leaf", "nokia_sros", [_gateway_vlan(100, l3_vni=50009)], _sros_vxlan_with_l3([100])
            )

    def test_vlan_without_gateway_gets_no_irb(self) -> None:
        irbs = BaseDeviceTransform._sros_irbs(
            "leaf", "nokia_sros", [{"vlan_id": 100, "name": "L2ONLY"}], _sros_vxlan_with_l3([100])
        )

        assert irbs == []

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf", "border-leaf", "border-spine"])
    def test_gateway_vlan_without_vni_is_refused(self, role: str) -> None:
        """A VLAN-only gateway segment has no vpls: the IRB would reference a service nothing creates."""
        with pytest.raises(
            ValueError,
            match=rf"nokia_sros {role} cannot render the gateway of VLAN 200 "
            r"\(no vpls/vprn to attach the IRB to\); refusing incomplete config",
        ):
            BaseDeviceTransform._sros_irbs(
                role, "nokia_sros", [_gateway_vlan(100), _gateway_vlan(200)], _sros_vxlan_with_l3([100])
            )

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "sonic", "dell_sonic"])
    def test_other_platforms_compute_no_irbs_and_never_refuse(self, platform: str) -> None:
        assert BaseDeviceTransform._sros_irbs("leaf", platform, [_gateway_vlan(200)], _sros_vxlan([100])) == []

    def test_without_vxlan_nothing_is_rendered_so_nothing_is_refused(self) -> None:
        assert BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [_gateway_vlan(200)], None) == []

    def test_extra_config_refuses_a_vlan_only_gateway_segment(self) -> None:
        """The refusal runs in the real context build, for the vlans the template would render."""
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_vlans", return_value=[_gateway_vlan(200)]),
            patch("transforms.common.get_vxlan_config", return_value=_sros_vxlan_with_l3([100])),
        ):
            with pytest.raises(ValueError, match=r"cannot render the gateway of VLAN 200"):
                transform._extra_config(_device_data(), "nokia_sros")

    def test_extra_config_passes_the_irb_list_to_the_template(self) -> None:
        transform = _make_transform("leaf")

        with (
            patch("transforms.common.get_vlans", return_value=[_gateway_vlan(100)]),
            patch("transforms.common.get_vxlan_config", return_value=_sros_vxlan_with_l3([100])),
        ):
            config = transform._extra_config(_device_data(), "nokia_sros")

        assert [(irb["vlan_id"], irb["vni"], irb["vprn"]) for irb in config["sros_irbs"]] == [(100, 10100, 50001)]

    def test_template_iterates_the_irb_list(self) -> None:
        """The template renders exactly the IRBs it is given (no VNI fallback of its own)."""
        vlan = _gateway_vlan(100)
        vxlan = {**_sros_vxlan_with_l3([100]), "anycast_gateway": {"enabled": True, "mac": "00:00:5e:00:01:01"}}

        rendered = _render_leaf(
            "nokia_sros",
            vlans=[vlan, _gateway_vlan(200)],
            vxlan=vxlan,
            sros_irbs=BaseDeviceTransform._sros_irbs("leaf", "nokia_sros", [vlan], vxlan),
        )

        assert 'configure service vprn 50001 interface "int-vpls-10100" vpls "vpls-10100"' in rendered
        assert 'configure service vprn 50001 interface "int-vpls-10100" mac "00:00:5e:00:01:01"' in rendered
        assert 'ipv4 primary-address "10.0.100.1/24"' in rendered
        assert "int-vpls-200" not in rendered


# ---------------------------------------------------------------------------
# Border guards: the refusal in _refuse_unprotected_tenant_svis makes them redundant
# ---------------------------------------------------------------------------


class TestPolicyBlocksFollowTheContext:
    """The leaf templates render ACLs/PBR from the context alone; borders get none because they are refused first."""

    _ACL = {"vlan_id": 100, "name": "ACL-100", "rules": []}
    _PBR = {"vlan_id": 100, "fw_nexthop": "10.0.0.2", "bypass_prefixes": []}

    def test_nxos_leaf_renders_acls_and_pbr_from_the_context(self) -> None:
        rendered = _render_leaf("cisco_nxos", name="test-leaf", acls=[self._ACL], leaf_pbr_rules=[self._PBR])

        assert "! Zero-Trust ACLs" in rendered
        assert "feature pbr" in rendered
        assert "! Leaf PBR" in rendered

    def test_nxos_border_template_renders_only_from_the_context(self) -> None:
        """The template renders policy only from the context; the refusal tests prove the removed guards redundant."""
        rendered = _render_leaf("cisco_nxos", name="test-bl", nxos_border=True, acls=[], leaf_pbr_rules=[])

        assert "Zero-Trust" not in rendered
        assert "feature pbr" not in rendered
        assert "Leaf PBR" not in rendered

    def test_sros_leaf_renders_ip_filters_and_binds_them(self) -> None:
        rendered = _render_leaf(
            "nokia_sros",
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100]}],
            acls=[self._ACL],
            vxlan=_sros_vxlan([100]),
        )

        assert "configure filter ip-filter 100 admin-state enable" in rendered
        assert 'sap 1/1/10:100 ingress ip-filter "100"' in rendered

    def test_sros_border_template_renders_only_from_the_context(self) -> None:
        """The template renders filters only from the context; the refusal tests prove the removed guards redundant."""
        rendered = _render_leaf(
            "nokia_sros",
            sros_border=True,
            interfaces=[{"name": "1/1/10", "role": "server", "vlans": [100]}],
            acls=[],
            vxlan=_sros_vxlan([100]),
        )

        assert "ip-filter" not in rendered
        assert "Zero-Trust" not in rendered
