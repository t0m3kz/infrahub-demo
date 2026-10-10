"""Unit tests for BaseDeviceTransform and ToR transform.

Covers:
- transform()  – data routing, platform detection, activation injection
- _build_config() – base context keys (interfaces, bgp, ospf, capabilities)
- _extra_config() – vlans/vxlan/acls/vrf_routes when device_role is set
- _filter_segment_deployments() – default pass-through; override semantics
- ToR class attributes (device_role="tor", template_subdir="leafs")
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import jinja2
import pytest
import yaml

from transforms.common import BaseDeviceTransform, _combine_leaf_pbr_rules, _loopback_name, get_capabilities
from transforms.config.tor import ToR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestCombineLeafPbrRules:
    def test_overlapping_vlan_keeps_lb_and_firewall_actions(self) -> None:
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}
        backend = {"vlan_id": 100, "backend_ips": ["10.0.1.10"], "lb_nexthop": "10.0.0.3"}

        assert _combine_leaf_pbr_rules([customer], [backend]) == [{**customer, **backend}]

    def test_firewall_only_vlan_needs_no_load_balancer(self) -> None:
        customer = {"vlan_id": 200, "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"}

        assert _combine_leaf_pbr_rules([customer], []) == [{**customer, "backend_ips": [], "lb_nexthop": None}]

    def test_nonoverlapping_vlans_preserve_only_their_actions(self) -> None:
        customer = {"vlan_id": 200, "bypass_prefixes": [], "fw_nexthop": "10.0.0.2"}
        backend = {"vlan_id": 100, "backend_ips": ["10.0.1.10"], "lb_nexthop": "10.0.0.3"}

        assert _combine_leaf_pbr_rules([customer], [backend]) == [
            {**backend, "bypass_prefixes": [], "fw_nexthop": None},
            {**customer, "backend_ips": [], "lb_nexthop": None},
        ]

    @pytest.mark.parametrize("platform", ["arista_eos", "cisco_nxos"])
    def test_template_attaches_one_ordered_policy_per_vlan(self, platform: str) -> None:
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}
        backend = {"vlan_id": 100, "backend_ips": ["10.0.1.10"], "lb_nexthop": "10.0.0.3"}
        template_dir = Path(__file__).parents[2] / "templates" / "configs"
        template = jinja2.Environment(loader=jinja2.FileSystemLoader(str(template_dir))).get_template(
            f"leafs/{platform}.j2"
        )

        rendered = template.render(
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
            sgt_rules=[],
            leaf_pbr_rules=_combine_leaf_pbr_rules([customer], [backend]),
        )

        assert rendered.count("ip policy route-map RM-LEAF-PBR-VLAN100") == 1
        assert rendered.index("RM-LEAF-PBR-VLAN100 permit 10") < rendered.index("RM-LEAF-PBR-VLAN100 permit 20")
        assert rendered.index("RM-LEAF-PBR-VLAN100 permit 20") < rendered.index("RM-LEAF-PBR-VLAN100 permit 30")
        assert "set ip next-hop 10.0.0.3" in rendered
        assert "set ip next-hop 10.0.0.2" in rendered

    def test_unsupported_leaf_refuses_firewall_only_pbr(self) -> None:
        """An SR OS leaf must not silently omit a firewall redirect when no LB exists."""
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}
        transform = _make_transform("leaf")

        with patch("transforms.common.get_customer_pbr_rules", return_value=[customer]):
            with pytest.raises(ValueError, match="cannot render GPO or PBR policy"):
                transform._extra_config(_device_data(), "nokia_sros")

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf"])
    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_sonic_leaf_accepts_pbr(self, platform: str, role: str) -> None:
        """SONiC renders leaf PBR, so the rules reach the template instead of raising."""
        customer = {"vlan_id": 100, "bypass_prefixes": ["10.0.2.0/24"], "fw_nexthop": "10.0.0.2"}
        transform = _make_transform(role)

        with patch("transforms.common.get_customer_pbr_rules", return_value=[customer]):
            config = transform._extra_config(_device_data(), platform)

        assert config["leaf_pbr_rules"] == [{**customer, "backend_ips": [], "lb_nexthop": None}]

    @pytest.mark.parametrize(
        ("platform", "message"),
        [
            ("sonic", "cannot render GPO policy"),
            ("dell_sonic", "cannot render GPO policy"),
            ("nokia_sros", "cannot render GPO or PBR policy"),
        ],
    )
    def test_unsupported_leaf_refuses_tag_without_contracts(self, platform: str, message: str) -> None:
        """A tag alone still requests GPO classification on the segment."""
        transform = _make_transform("leaf")

        with patch("transforms.common.get_vlans", return_value=[{"sgt": 20}]):
            with pytest.raises(ValueError, match=message):
                transform._extra_config(_device_data(), platform)

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_sonic_template_renders_ordered_pbr_table_per_vlan(self, platform: str) -> None:
        """ConfigDB PBR mirrors the route-map order: LB return > bypass > firewall redirect."""
        customer = {
            "vlan_id": 100,
            "customer_name": "c001",
            "environment": "prod",
            "bypass_prefixes": ["10.0.2.0/24", "10.0.3.0/24"],
            "fw_nexthop": "10.0.0.2",
        }
        backend = {"vlan_id": 100, "backend_ips": ["10.0.1.10", "10.0.1.11/32"], "lb_nexthop": "10.0.0.3"}

        config = json.loads(_render_leaf(platform, leaf_pbr_rules=_combine_leaf_pbr_rules([customer], [backend])))

        assert config["ACL_TABLE"]["PBR-VLAN100"] == {
            "type": "PBR",
            "policy_desc": "Leaf PBR for c001/prod",
            "ports": ["Vlan100"],
            "stage": "ingress",
        }
        rules = {k: v for k, v in config["ACL_RULE"].items() if k.startswith("PBR-VLAN100|")}
        assert rules == {
            "PBR-VLAN100|LB_1": {
                "PRIORITY": "8999",
                "IP_TYPE": "IPV4ANY",
                "SRC_IP": "10.0.1.10/32",
                "PACKET_ACTION": "REDIRECT:10.0.0.3",
            },
            "PBR-VLAN100|LB_2": {
                "PRIORITY": "8998",
                "IP_TYPE": "IPV4ANY",
                "SRC_IP": "10.0.1.11/32",
                "PACKET_ACTION": "REDIRECT:10.0.0.3",
            },
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

    def test_sonic_template_lb_only_vlan_has_no_firewall_redirect(self) -> None:
        """Without a firewall next-hop there is nothing to bypass and no catch-all redirect."""
        backend = {"vlan_id": 200, "backend_ips": ["10.0.1.10"], "lb_nexthop": "10.0.0.3"}

        config = json.loads(_render_leaf("sonic", leaf_pbr_rules=_combine_leaf_pbr_rules([], [backend])))

        assert config["ACL_TABLE"]["PBR-VLAN200"]["policy_desc"] == "Leaf PBR for any/any"
        assert sorted(k for k in config["ACL_RULE"] if k.startswith("PBR-VLAN200|")) == ["PBR-VLAN200|LB_1"]

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
        "ntp": {"servers": []},
        "syslog": {"servers": []},
        **overrides,
    }
    return template.render(**context)


@pytest.mark.parametrize("role", ["leafs", "border_leafs", "l2_leafs", "spines", "super_spines"])
@pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
def test_sonic_templates_render_pure_configdb(role: str, platform: str) -> None:
    """Management services stay in a single JSON artifact for every switch role."""
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
        ntp={"servers": [{"address": "192.0.2.1"}]},
        syslog={"servers": [{"address": "192.0.2.2"}]},
    )
    config = json.loads(rendered)
    assert config["NTP_SERVER"] == {"192.0.2.1": {}}
    assert config["SYSLOG_SERVER"] == {"192.0.2.2": {}}


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
        assert result == {
            "bgp_enabled": False,
            "ospf_enabled": False,
            "mlag_enabled": False,
            "ha_enabled": False,
            "ntp_enabled": False,
            "syslog_enabled": False,
            "snmp_enabled": False,
            "aaa_enabled": False,
        }

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
# _filter_segment_deployments
# ---------------------------------------------------------------------------


class TestFilterSegmentDeployments:
    def test_default_returns_all_activations(self) -> None:
        t = _make_transform()
        activations = [{"id": "a1"}, {"id": "a2"}]
        assert t._filter_segment_deployments(activations) == activations

    def test_empty_list_returned_unchanged(self) -> None:
        t = _make_transform()
        assert t._filter_segment_deployments([]) == []

    def test_subclass_can_filter(self) -> None:
        class FilteredTransform(BaseDeviceTransform):
            def _filter_segment_deployments(self, activations):
                return [a for a in activations if a.get("active")]

        ft = FilteredTransform.__new__(FilteredTransform)
        ft.device_role = ""
        activations = [{"id": "a1", "active": True}, {"id": "a2", "active": False}]
        assert ft._filter_segment_deployments(activations) == [{"id": "a1", "active": True}]


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
        assert cfg["capabilities"] == {
            "bgp_enabled": False,
            "ospf_enabled": False,
            "mlag_enabled": False,
            "ha_enabled": False,
            "ntp_enabled": False,
            "syslog_enabled": False,
            "snmp_enabled": False,
            "aaa_enabled": False,
        }


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
    async def test_activation_from_interface_capabilities(self) -> None:
        """Activations on segment.segment_deployments within interface_capabilities are collected."""
        t = _make_transform(device_role="leaf")
        interfaces = [
            {
                "name": "Ethernet10",
                "interface_capabilities": [
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
                "interface_capabilities": [
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
                "interface_capabilities": [
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
        """No segment_deployments on interface_capabilities → vlans is empty in rendered config."""
        t = _make_transform(device_role="leaf")
        # Interfaces with no interface_capabilities carrying segment_deployments
        device = _device_data(interfaces=[{"name": "Ethernet1", "interface_capabilities": []}])

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
    @pytest.mark.parametrize("setting", ["snmp", "aaa"])
    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    async def test_sonic_unsupported_settings_fail_before_render(self, platform: str, setting: str) -> None:
        """Unsupported settings cannot disappear while converting CLI artifacts to JSON."""
        transform = _make_transform(device_role="leaf")
        device = _device_data(platform=platform)
        with (
            patch("transforms.common.clean_data", return_value={"DcimPhysicalDevice": [device]}),
            patch.object(transform, "_build_config", return_value={setting: {"enabled": True}}),
            patch.object(transform, "_extra_config", return_value={}),
            patch.object(transform, "_load_template") as load_template,
            pytest.raises(ValueError, match=setting),
        ):
            await transform.transform({"raw": "data"})
        load_template.assert_not_called()

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
            "interface_capabilities": [
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

    @pytest.mark.asyncio
    async def test_tor_filter_segment_deployments_passthrough(self) -> None:
        """ToR does not override _filter_segment_deployments — all activations pass."""
        t = _make_tor()
        activations = [{"id": "seg-1"}, {"id": "seg-2"}]
        result = t._filter_segment_deployments(activations)
        assert result == activations
