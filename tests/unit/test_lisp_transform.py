"""Unit tests for the LISP overlay renderer (transforms/helpers/lisp.py and templates/configs/common/cisco_xe_lisp.j2).

Covers:
  - the ``lisp`` template context  — leaf and border content, map-server set, EID prefixes, site records
  - refusals                       — non-cisco_xe platform, IPv6 gateway, missing key / map-server / site name
  - control-plane gate             — ``bgp_evpn`` keeps ``vxlan`` and gets no ``lisp`` key, lisp has no VXLAN
  - purity                         — inputs are not mutated, collections are lists
  - rendered CLI                   — every LISP stanza sits under an ``unverified-no-lab`` marker, no EVPN leftovers

The rendered text goes through the real transforms on the cisco_xe smoke inputs (tests/smoke/configs), switched
to a LISP fabric the way queries/fragments/lisp_fabric.gql returns it. All IOS-XE LISP syntax is unlabbed.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.smoke.test_configs_smoke import _run_transform
from transforms.common import BaseDeviceTransform, _fabric_overlay_control_plane
from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf
from transforms.helpers.lisp import EidRecord, LispInputs, build_lisp_inputs, get_lisp_config

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "smoke" / "configs"
MARKER = "unverified-no-lab:"
LEAF_INPUT = "leaf_cisco_xe_ospf_ibgp"
BORDER_INPUT = "border_leaf_cisco_xe_ospf_ibgp"

_MAP_SERVERS: list[dict[str, Any]] = [
    {"name": "dc1-border-02", "interfaces": [{"ip_address": {"address": "10.255.0.2/32"}}]},
    {"name": "dc1-border-01", "interfaces": [{"ip_address": {"address": "10.255.0.1/32"}}]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lisp_raw_input(directory: str, *, key: bool = True, platform: str | None = None) -> dict[str, Any]:
    """The raw GraphQL input of a smoke golden, turned into a LISP fabric (no BGP process, flag, map-servers, key)."""
    data = json.loads((CONFIGS_DIR / directory / "input.json").read_text())
    node = data["DcimDevice"]["edges"][0]["node"]
    node["capabilities"]["edges"] = [
        edge for edge in node["capabilities"]["edges"] if edge["node"].get("__typename") != "ManagedBGP"
    ]
    deployment = node["deployment"]["node"]
    deployment["overlay_control_plane"] = {"value": "lisp"}
    deployment["lisp_map_servers"] = {
        "edges": [
            {
                "node": {
                    "name": {"value": server["name"]},
                    "interfaces": {
                        "edges": [
                            {"node": {"ip_address": {"node": {"address": {"value": iface["ip_address"]["address"]}}}}}
                            for iface in server["interfaces"]
                        ]
                    },
                }
            }
            for server in _MAP_SERVERS
        ]
    }
    if key:
        deployment["lisp_site_key"] = {"node": {"password": {"value": "s3cr3t"}}}
    if platform:
        node["platform"]["node"]["netmiko_device_type"]["value"] = platform
    return data


def _render(transform_cls: type, directory: str, **kwargs: Any) -> str:
    return _run_transform(transform_cls, _lisp_raw_input(directory, **kwargs))


def _inputs(**overrides: Any) -> LispInputs:
    values: dict[str, Any] = {
        "map_servers": ("10.255.0.1", "10.255.0.2"),
        "site_name": "DC-1",
        "site_key": "s3cr3t",
        "l3_records": (EidRecord(instance_id=50001, vrf="PROD", prefix="10.1.0.0/24"),),
        "l2_instance_ids": (10100,),
    }
    values.update(overrides)
    return LispInputs(**values)


def _activation(
    vlan_id: int = 100,
    vni: int | None = 10100,
    gateway: str | None = "10.1.0.1/24",
    namespace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    segment: dict[str, Any] = {"name": f"seg-{vlan_id}", "customer_name": f"seg-{vlan_id}", "arp_suppression": True}
    if gateway:
        segment["gateway"] = {
            "address": gateway,
            "ip_prefix": {"ip_namespace": namespace or {"name": "PROD", "l3_vni": 50001}},
        }
    return {"vlan_id": vlan_id, "vni": vni, "status": "active", "segment": segment}


def _device() -> dict[str, Any]:
    return {
        "name": "dc1-leaf-01",
        "interfaces": [
            {"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}},
            {"name": "Ethernet1", "role": "uplink"},
        ],
    }


def _make_transform(device_role: str) -> BaseDeviceTransform:
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    transform.device_role = device_role
    transform.template_subdir = "leafs"
    transform.root_directory = "/fake/root"
    return transform


def _fabric_data(role: str, platform: str, control_plane: str | None) -> dict[str, Any]:
    deployment: dict[str, Any] = {"name": "DC-1", "lisp_map_servers": _MAP_SERVERS, "lisp_site_key": {"password": "k"}}
    if control_plane:
        deployment["overlay_control_plane"] = control_plane
    return {
        "name": "dev-1",
        "role": role,
        "platform": {"netmiko_device_type": platform},
        "interfaces": [],
        "capabilities": [],
        "deployment": deployment,
    }


def _lisp_lines(text: str) -> list[str]:
    """The lines of the ``router lisp`` block of a rendered config (up to the next column-0 ``!``)."""
    lines = text.splitlines()
    start = lines.index("router lisp")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "!")
    return lines[start:end]


# ---------------------------------------------------------------------------
# Rendered leaf and border
# ---------------------------------------------------------------------------


class TestRenderedLeaf:
    def test_registers_with_every_map_server_sorted_by_device_name(self) -> None:
        text = _render(Leaf, LEAF_INPUT)

        assert "  itr map-resolver 10.255.0.1\n  itr map-resolver 10.255.0.2\n" in text
        assert text.count("  etr map-server 10.255.0.1 key s3cr3t") == 2  # service ipv4 and service ethernet
        assert text.count("  etr map-server 10.255.0.2 proxy-reply") == 2

    def test_leaf_is_not_a_map_server_and_has_no_site(self) -> None:
        lisp = _lisp_lines(_render(Leaf, LEAF_INPUT))

        assert "  map-server" not in lisp
        assert "  map-resolver" not in lisp
        assert not any(line.startswith(" site ") for line in lisp)

    def test_instances_and_dynamic_eid_follow_the_segments(self) -> None:
        text = _render(Leaf, LEAF_INPUT)

        assert (
            " instance-id 50001\n  dynamic-eid VRF_A-IPV4\n   database-mapping 10.100.0.0/24 locator-set RLOC\n" in text
        )
        assert "   eid-table vrf VRF_A\n   map-cache 0.0.0.0/0 map-request\n" in text
        assert (
            " instance-id 10100\n  service ethernet\n   eid-table vlan 100\n   database-mapping mac locator-set RLOC\n"
            in text
        )

    def test_gateway_svi_carries_the_mobility_and_the_anycast_mac(self) -> None:
        text = _render(Leaf, LEAF_INPUT)

        assert (
            "interface Vlan100\n description Customer-A\n vrf forwarding VRF_A\n ip address 10.100.0.1 255.255.255.0\n"
            " lisp mobility VRF_A-IPV4\n mac-address 001c.7300.dc01\n"
        ) in text

    def test_no_evpn_vxlan_or_bgp_leftovers(self) -> None:
        text = _render(Leaf, LEAF_INPUT)

        for leftover in ("l2vpn evpn", "nve1", "router bgp", "RM-CONN-2-BGP", "EVPN-VXLAN"):
            assert leftover not in text
        assert "router ospf 1" in text  # the OSPFv2 underlay is the existing template's

    def test_site_key_missing_is_refused(self) -> None:
        with pytest.raises(
            ValueError, match=r"cisco_xe leaf cannot render the LISP overlay: the fabric has no LISP site key"
        ):
            _render(Leaf, LEAF_INPUT, key=False)


class TestRenderedBorder:
    def test_border_is_a_co_located_map_server_and_resolver_listing_every_border(self) -> None:
        lisp = _lisp_lines(_render(BorderLeaf, BORDER_INPUT))

        assert lisp.count("  map-server") == 2  # service ipv4 and service ethernet
        assert lisp.count("  map-resolver") == 2
        assert "  itr map-resolver 10.255.0.1" in lisp and "  itr map-resolver 10.255.0.2" in lisp

    def test_site_registers_the_eid_records_of_the_dc_segments(self) -> None:
        text = _render(BorderLeaf, BORDER_INPUT)

        assert (
            " site DC-1\n  authentication-key s3cr3t\n"
            "  eid-record instance-id 50001 10.100.0.0/24 accept-more-specifics\n"
            "  eid-record instance-id 10100 any-mac\n  exit-site\n"
        ) in text

    def test_border_keeps_the_leaf_overlay_wiring(self) -> None:
        text = _render(BorderLeaf, BORDER_INPUT)

        assert "! Cisco IOS-XE (Catalyst 9000) LISP border-leaf" in text
        assert "l2vpn evpn" not in text and "router bgp" not in text


class TestMarkers:
    @pytest.mark.parametrize(("cls", "directory"), [(Leaf, LEAF_INPUT), (BorderLeaf, BORDER_INPUT)])
    def test_every_lisp_stanza_sits_under_an_unverified_marker(self, cls: type, directory: str) -> None:
        """The line above each locator-set / service / instance-id / site / `lisp mobility` block is a marker."""
        text = _render(cls, directory)
        lines = text.splitlines()
        stanza = re.compile(r"^\s*(locator-table|service (ipv4|ethernet)|instance-id \d+|site \S+)")
        checked = 0
        for index, line in enumerate(lines):
            if line == "router lisp" or stanza.match(line) and not line.startswith("  "):
                previous = lines[index - 1]
                assert MARKER in previous, line
                checked += 1
        assert checked >= 5

    @pytest.mark.parametrize(("cls", "directory"), [(Leaf, LEAF_INPUT), (BorderLeaf, BORDER_INPUT)])
    def test_lisp_mobility_follows_a_marker_in_its_section(self, cls: type, directory: str) -> None:
        lines = _render(cls, directory).splitlines()
        first = next(i for i, line in enumerate(lines) if line.strip().startswith("lisp mobility"))
        section_start = max(i for i in range(first) if lines[i] == "! Gateway SVIs")
        assert any(MARKER in line and "lisp mobility" in line for line in lines[section_start:first])

    def test_every_unverified_vrf_and_router_lisp_line_has_a_marker_above_the_block(self) -> None:
        lines = _render(Leaf, LEAF_INPUT).splitlines()
        vrf = lines.index("vrf definition VRF_A")
        assert MARKER in lines[vrf - 1]
        router = lines.index("router lisp")
        assert MARKER in lines[router + 1]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestPlatformRefusal:
    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "nokia_sros"])
    @pytest.mark.parametrize(
        ("cls", "directory", "role"), [(Leaf, LEAF_INPUT, "leaf"), (BorderLeaf, BORDER_INPUT, "border-leaf")]
    )
    def test_a_vtep_on_another_platform_is_refused_with_the_exact_message(
        self, platform: str, cls: type, directory: str, role: str
    ) -> None:
        with pytest.raises(ValueError) as error:
            _run_transform(cls, _lisp_raw_input(directory, platform=platform))

        assert str(error.value) == f"{platform} {role} cannot render the LISP overlay; refusing incomplete config"

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos"])
    def test_spines_of_a_lisp_fabric_are_underlay_only_and_never_refused(self, platform: str) -> None:
        config = _make_transform("spine")._extra_config(_fabric_data("spine", platform, "lisp"), platform)

        assert config["vxlan"] is None
        assert "lisp" not in config

    def test_cisco_xe_existing_refusals_still_apply_on_lisp(self) -> None:
        data = _lisp_raw_input(LEAF_INPUT)
        with patch("transforms.common.get_acls", return_value=[{"vlan_id": 100, "name": "ACL-100"}]):
            with pytest.raises(ValueError, match=r"cisco_xe leaf cannot render: acls; refusing incomplete config"):
                _run_transform(Leaf, data)


class TestContentRefusals:
    def test_an_ipv6_gateway_is_refused(self) -> None:
        activation = _activation()
        activation["segment"]["gateway"]["address"] = "2001:db8::1/64"

        with pytest.raises(ValueError, match=r"VLAN 100 has an IPv6 gateway \(service ipv6 is not rendered\)"):
            get_lisp_config(_device(), "leaf", [activation], _inputs())

    def test_a_gateway_outside_a_vrf_with_an_l3_instance_is_refused(self) -> None:
        activation = _activation(namespace={"name": "default"})

        with pytest.raises(ValueError, match=r"the gateway of VLAN 100 is in no VRF with an L3 instance-id"):
            get_lisp_config(_device(), "leaf", [activation], _inputs())

    def test_no_map_server_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the fabric has no border leaf map-server"):
            get_lisp_config(_device(), "leaf", [_activation()], _inputs(map_servers=()))

    def test_no_site_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the fabric has no LISP site key"):
            get_lisp_config(_device(), "leaf", [_activation()], _inputs(site_key=None))

    def test_a_border_without_a_site_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the data center has no name for the LISP site"):
            get_lisp_config(_device(), "border-leaf", [], _inputs(site_name=None))

    def test_a_map_server_without_a_loopback_address_is_refused(self) -> None:
        deployment = {"lisp_map_servers": [{"name": "dc1-border-01", "interfaces": []}]}

        with pytest.raises(ValueError, match="LISP map-server dc1-border-01 has no loopback address"):
            build_lisp_inputs(deployment, "leaf")

    def test_an_ipv6_segment_prefix_on_a_border_is_refused(self) -> None:
        deployment = {
            "segment_deployments": [{"vni": 10100, "segment": {"gateway": {"ip_prefix": {"prefix": "2001:db8::/64"}}}}]
        }

        with pytest.raises(ValueError, match=r"has an IPv6 prefix \(service ipv6 is not rendered\)"):
            build_lisp_inputs(deployment, "border-leaf")


# ---------------------------------------------------------------------------
# Context content
# ---------------------------------------------------------------------------


class TestLispContext:
    def test_edge_context(self) -> None:
        config = get_lisp_config(_device(), "leaf", [_activation(), _activation(200, 10200, gateway=None)], _inputs())

        assert config is not None
        assert config["role"] == "edge"
        assert config["rloc_interface"] == "Loopback0"
        assert config["map_servers"] == ["10.255.0.1", "10.255.0.2"]
        assert config["l2_instances"] == [
            {"instance_id": 10100, "vlan_id": 100, "name": "seg-100"},
            {"instance_id": 10200, "vlan_id": 200, "name": "seg-200"},
        ]
        assert config["l3_instances"] == [{"instance_id": 50001, "vrf": "PROD", "prefixes": ["10.1.0.0/24"]}]
        assert config["anycast_gateway"]["enabled"] is True
        assert config["site"] is None

    def test_prefixes_are_grouped_per_vrf_deduplicated_and_sorted(self) -> None:
        other = {"name": "DEV", "l3_vni": 50002}
        activations = [
            _activation(100, 10100, "10.1.1.1/24"),
            _activation(101, 10101, "10.1.0.1/24"),
            _activation(102, 10102, "10.1.0.5/24"),
            _activation(103, 10103, "10.9.0.1/24", namespace=other),
        ]

        config = get_lisp_config(_device(), "leaf", activations, _inputs())

        assert config is not None
        assert {l3["vrf"]: l3["prefixes"] for l3 in config["l3_instances"]} == {
            "DEV": ["10.9.0.0/24"],
            "PROD": ["10.1.0.0/24", "10.1.1.0/24"],
        }

    def test_an_edge_without_instances_has_no_lisp_context(self) -> None:
        assert get_lisp_config(_device(), "leaf", [], _inputs()) is None
        assert get_lisp_config(_device(), "leaf", [_activation(250, None, gateway=None)], _inputs()) is None

    def test_a_border_always_has_a_context_with_its_site(self) -> None:
        config = get_lisp_config(_device(), "border-leaf", [], _inputs())

        assert config is not None
        assert config["role"] == "border"
        assert config["l2_instances"] == [] and config["l3_instances"] == []
        assert config["anycast_gateway"] == {"enabled": False, "mac": "00:1c:73:00:dc:01"}
        assert config["site"] == {
            "name": "DC-1",
            "l3_records": [{"instance_id": 50001, "vrf": "PROD", "prefix": "10.1.0.0/24"}],
            "l2_instance_ids": [10100],
        }

    def test_the_fabric_anycast_mac_is_used(self) -> None:
        config = get_lisp_config(_device(), "leaf", [_activation()], _inputs(), fabric_anycast_mac="00:00:22:22:33:33")

        assert config is not None
        assert config["anycast_gateway"] == {"enabled": True, "mac": "00:00:22:22:33:33"}

    def test_the_rloc_interface_prefers_the_dedicated_vtep_loopback(self) -> None:
        device = _device()
        device["interfaces"].append(
            {"name": "Loopback1", "role": "loopback-vtep", "ip_address": {"address": "10.9.0.1/32"}}
        )

        config = get_lisp_config(device, "leaf", [_activation()], _inputs())

        assert config is not None
        assert config["rloc_interface"] == "Loopback1"

    def test_stretched_and_arp_suppression_have_no_effect(self) -> None:
        plain = get_lisp_config(_device(), "leaf", [_activation()], _inputs())
        activation = _activation()
        activation["segment"].update(arp_suppression=False, id="s", stretch_scope="metro")
        activation["segment"]["segment_deployments"] = [
            {"vni": 1, "deployment": {"id": "a"}},
            {"vni": 1, "deployment": {"id": "b"}},
        ]

        assert get_lisp_config(_device(), "leaf", [activation], _inputs()) == plain


class TestBuildLispInputs:
    def test_dc_level_device_reads_its_own_deployment(self) -> None:
        deployment = {
            "name": "DC-1",
            "lisp_map_servers": _MAP_SERVERS,
            "lisp_site_key": {"password": "k"},
            "segment_deployments": [
                {
                    "vni": 10100,
                    "segment": {
                        "gateway": {
                            "ip_prefix": {"prefix": "10.1.0.0/24", "ip_namespace": {"name": "PROD", "l3_vni": 50001}}
                        }
                    },
                },
                {
                    "vni": 10200,
                    "segment": {
                        "terminate_inline": True,
                        "gateway": {
                            "ip_prefix": {"prefix": "10.2.0.0/24", "ip_namespace": {"name": "PROD", "l3_vni": 50001}}
                        },
                    },
                },
                {
                    "vni": 10300,
                    "segment": {
                        "gateway": {"ip_prefix": {"prefix": "10.3.0.0/24", "ip_namespace": {"name": "default"}}}
                    },
                },
                {
                    "vni": None,
                    "segment": {
                        "gateway": {
                            "ip_prefix": {"prefix": "10.4.0.0/24", "ip_namespace": {"name": "PROD", "l3_vni": 50001}}
                        }
                    },
                },
            ],
        }

        inputs = build_lisp_inputs(deployment, "border-leaf")

        assert inputs == LispInputs(
            map_servers=("10.255.0.1", "10.255.0.2"),
            site_name="DC-1",
            site_key="k",
            l3_records=(EidRecord(50001, "PROD", "10.1.0.0/24"),),
            l2_instance_ids=(10100, 10200, 10300),
        )

    def test_pod_level_device_reads_the_parent_dc(self) -> None:
        deployment = {
            "name": "POD-1",
            "parent": {"name": "DC-1", "lisp_map_servers": _MAP_SERVERS, "lisp_site_key": {"password": "k"}},
        }

        inputs = build_lisp_inputs(deployment, "leaf")

        assert inputs.map_servers == ("10.255.0.1", "10.255.0.2")
        assert inputs.site_key == "k"
        assert inputs.site_name is None  # only a border renders the site
        assert inputs.l3_records == () and inputs.l2_instance_ids == ()

    @pytest.mark.parametrize("deployment", [None, {}, {"lisp_site_key": None, "lisp_map_servers": None}])
    def test_missing_data_gives_empty_inputs_not_errors(self, deployment: Any) -> None:
        inputs = build_lisp_inputs(deployment, "leaf")

        assert inputs.map_servers == () and inputs.site_key is None


# ---------------------------------------------------------------------------
# Control-plane gate in BaseDeviceTransform
# ---------------------------------------------------------------------------


class TestControlPlaneGate:
    @pytest.mark.parametrize(
        ("deployment", "expected"),
        [
            (None, "bgp_evpn"),
            ({}, "bgp_evpn"),
            ({"overlay_control_plane": "bgp_evpn"}, "bgp_evpn"),
            ({"overlay_control_plane": "lisp"}, "lisp"),
            ({"parent": {"overlay_control_plane": "lisp"}}, "lisp"),
            ({"overlay_control_plane": "bgp_evpn", "parent": {"overlay_control_plane": "lisp"}}, "bgp_evpn"),
        ],
    )
    def test_fabric_overlay_control_plane_resolves_dc_then_pod_parent(self, deployment: Any, expected: str) -> None:
        assert _fabric_overlay_control_plane(deployment) == expected

    @pytest.mark.parametrize("flag", [None, "bgp_evpn"])
    def test_bgp_evpn_keeps_vxlan_and_adds_no_lisp_key(self, flag: str | None) -> None:
        with patch("transforms.common.get_vxlan_config", return_value={"enabled": True}) as vxlan:
            config = _make_transform("leaf")._extra_config(_fabric_data("leaf", "arista_eos", flag), "arista_eos")

        vxlan.assert_called_once()
        assert config["vxlan"] == {"enabled": True}
        assert "lisp" not in config

    def test_lisp_replaces_vxlan_with_none_and_never_builds_evpn(self) -> None:
        data = _fabric_data("leaf", "cisco_xe", "lisp")
        data["segment_deployments"] = [_activation()]

        with patch("transforms.common.get_vxlan_config") as vxlan:
            config = _make_transform("leaf")._extra_config(data, "cisco_xe")

        vxlan.assert_not_called()
        assert config["vxlan"] is None
        assert config["lisp"]["l3_instances"][0]["vrf"] == "PROD"

    def test_lisp_vrfs_reach_the_group_policy_view(self) -> None:
        data = _fabric_data("leaf", "cisco_xe", "lisp")
        data["segment_deployments"] = [_activation()]

        with patch("transforms.common.group_policy_device_view", return_value=None) as view:
            _make_transform("leaf")._extra_config(data, "cisco_xe")

        assert view.call_args.kwargs["vrfs"] == ["PROD"]

    def test_an_edge_without_instances_gets_no_lisp_key(self) -> None:
        config = _make_transform("leaf")._extra_config(_fabric_data("leaf", "cisco_xe", "lisp"), "cisco_xe")

        assert config["vxlan"] is None
        assert "lisp" not in config

    def test_evpn_render_is_unchanged_by_the_explicit_default_flag(self) -> None:
        """An explicit bgp_evpn flag and none at all render the same EVPN config."""
        implicit = json.loads((CONFIGS_DIR / LEAF_INPUT / "input.json").read_text())
        explicit = copy.deepcopy(implicit)
        explicit["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]["overlay_control_plane"] = {
            "value": "bgp_evpn"
        }

        rendered = _run_transform(Leaf, implicit)

        assert _run_transform(Leaf, explicit) == rendered
        assert "l2vpn evpn" in rendered and "router lisp" not in rendered


# ---------------------------------------------------------------------------
# Purity and shape
# ---------------------------------------------------------------------------


class TestPurityAndShape:
    def test_inputs_are_not_mutated(self) -> None:
        device, activations, inputs = _device(), [_activation(), _activation(200, 10200, "10.2.0.1/24")], _inputs()
        before = copy.deepcopy((device, activations, inputs))

        get_lisp_config(device, "border-leaf", activations, inputs)

        assert (device, activations, inputs) == before

    def test_build_lisp_inputs_does_not_mutate_the_deployment(self) -> None:
        deployment = {
            "name": "DC-1",
            "lisp_map_servers": copy.deepcopy(_MAP_SERVERS),
            "lisp_site_key": {"password": "k"},
            "segment_deployments": [
                {
                    "vni": 1,
                    "segment": {
                        "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24", "ip_namespace": {"name": "P", "l3_vni": 5}}}
                    },
                }
            ],
        }
        before = copy.deepcopy(deployment)

        build_lisp_inputs(deployment, "border-leaf")

        assert deployment == before

    def test_the_result_is_repeatable_and_owned_by_the_caller(self) -> None:
        args = (_device(), "border-leaf", [_activation()], _inputs())
        first = get_lisp_config(*args)
        assert first is not None
        first["map_servers"].append("junk")
        first["site"]["l3_records"].clear()

        assert get_lisp_config(*args) == get_lisp_config(*copy.deepcopy(args))
        second = get_lisp_config(*args)
        assert second is not None and second["map_servers"] == ["10.255.0.1", "10.255.0.2"]

    def test_collections_are_lists_never_none(self) -> None:
        config = get_lisp_config(_device(), "border-leaf", [_activation()], _inputs())

        assert config is not None
        for key in ("map_servers", "l2_instances", "l3_instances"):
            assert type(config[key]) is list
        assert type(config["l3_instances"][0]["prefixes"]) is list
        assert type(config["site"]["l3_records"]) is list and type(config["site"]["l2_instance_ids"]) is list

    def test_lisp_inputs_are_frozen(self) -> None:
        with pytest.raises(AttributeError):
            setattr(_inputs(), "site_key", "other")  # noqa: B010

    def test_the_context_has_no_vxlan_state(self) -> None:
        config = get_lisp_config(_device(), "leaf", [_activation()], _inputs())

        assert config is not None
        assert not {"vxlan", "evpn", "l2_vni_mappings", "l3_vni_mappings", "vtep"} & set(config)
