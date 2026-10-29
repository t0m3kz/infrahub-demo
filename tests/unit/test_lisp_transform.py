"""Unit tests for the LISP renderer (transforms/helpers/lisp.py and templates/configs/common/cisco_xe_lisp.j2).

The renderer serves an office's LISP site: its access-switches (edges) and distribution-switches (borders).
tests/unit/test_office_lisp_transform.py covers the office end to end (registration, queries, goldens); this module
covers the renderer's own behaviour.

Covers:
  - the ``lisp`` template context  — edge and border content, map-server set, EID prefixes, site records
  - refusals                       — IPv6 gateway, gateway outside a VRF, missing key / map-server / site name
  - role gate                      — an office role replaces ``vxlan`` with ``lisp``; a DC VTEP never takes the LISP path
  - purity                         — inputs are not mutated, collections are lists
  - rendered CLI                   — a hostless edge, the SVIs' mobility binding, every LISP stanza under an
                                     ``unverified-no-lab`` marker, no EVPN leftovers

The rendered text goes through the real transforms on the office smoke inputs (tests/smoke/configs). All IOS-XE LISP
syntax is unlabbed.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.smoke.fixture_builders.common import run_transform
from transforms.common import BaseDeviceTransform
from transforms.config.access_switch import AccessSwitch
from transforms.helpers.lisp import EidRecord, LispInputs, build_lisp_inputs, get_lisp_config
from utils.fabric_roles import DC_VTEP_ROLES, OFFICE_LISP_ROLES

CONFIGS_DIR = Path(__file__).resolve().parents[1] / "smoke" / "configs"
MARKER = "unverified-no-lab:"
ACCESS_INPUT = "access_switch_cisco_xe_lisp_ospf"
HOSTLESS_INPUT = "access_switch_cisco_xe_lisp_hostless"

_MAP_SERVERS: list[dict[str, Any]] = [
    {"name": "ds-c005-office02", "interfaces": [{"ip_address": {"address": "10.250.5.2/32"}}]},
    {"name": "ds-c005-office01", "interfaces": [{"ip_address": {"address": "10.250.5.1/32"}}]},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw(directory: str) -> dict[str, Any]:
    """The raw GraphQL input of a smoke golden."""
    return json.loads((CONFIGS_DIR / directory / "input.json").read_text())


def _inputs(**overrides: Any) -> LispInputs:
    values: dict[str, Any] = {
        "map_servers": ("10.250.5.1", "10.250.5.2"),
        "site_name": "C005-OFFICE",
        "site_key": "s3cr3t",
        "l3_records": (EidRecord(instance_id=50001, vrf="PROD", prefix="10.1.0.0/24"),),
        "l2_instance_ids": (10100,),
    }
    values.update(overrides)
    return LispInputs(**values)


_PROD = {"name": "PROD", "l3_vni": 50001, "namespace_type": "prod"}


def _vrf_cap(namespace: dict[str, Any]) -> dict[str, Any]:
    """A ManagedVRF instance of the device, as the cleaned config query returns it."""
    return {"typename": "ManagedVRF", "name": f"dev-vrf-{namespace['name'].lower()}", "namespace": namespace}


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
        "name": "as-c005-office01",
        "interfaces": [
            {"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}},
            {"name": "Ethernet1", "role": "uplink"},
        ],
        "capabilities": [_vrf_cap(_PROD)],
    }


def _make_transform(device_role: str) -> BaseDeviceTransform:
    transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
    transform.device_role = device_role
    transform.template_subdir = "leafs"
    transform.root_directory = "/fake/root"
    return transform


def _office_data(role: str, platform: str = "cisco_xe") -> dict[str, Any]:
    """A cleaned device with the office deployment the LISP queries return (queries/fragments/lisp_fabric.gql)."""
    deployment: dict[str, Any] = {
        "name": "C005-OFFICE",
        "lisp_map_servers": _MAP_SERVERS,
        "lisp_site_key": {"password": "k"},
    }
    return {
        "name": "dev-1",
        "role": role,
        "platform": {"name": platform},
        "interfaces": [],
        "capabilities": [_vrf_cap(_PROD)],
        "deployment": deployment,
    }


def _lisp_lines(text: str) -> list[str]:
    """The lines of the ``router lisp`` block of a rendered config (up to the next column-0 ``!``)."""
    lines = text.splitlines()
    start = lines.index("router lisp")
    end = next(i for i in range(start + 1, len(lines)) if lines[i] == "!")
    return lines[start:end]


# ---------------------------------------------------------------------------
# Rendered edge
# ---------------------------------------------------------------------------


class TestRenderedEdge:
    def test_a_hostless_edge_renders_the_edge_config_with_no_instance(self) -> None:
        """No segment on the device: the edge LISP config is complete, with no instance-id, VRF or dangling `!`."""
        text = run_transform(AccessSwitch, _raw(HOSTLESS_INPUT))
        lisp = _lisp_lines(text)

        assert "LISP" in text.splitlines()[2] and "EVPN-VXLAN" not in text
        assert lisp.count("  itr map-resolver 10.250.5.1") == 2 and lisp.count("  itr map-resolver 10.250.5.2") == 2
        assert text.count("  etr map-server 10.250.5.1 key 0ff1c3-k3y") == 2
        assert text.count("  etr map-server 10.250.5.2 key 0ff1c3-k3y") == 2
        assert lisp.count("  itr") == 2 and lisp.count("  etr") == 2
        assert not any("instance-id" in line or "dynamic-eid" in line or line.startswith(" site ") for line in lisp)
        assert "vrf definition" not in text and "Gateway SVIs" not in text
        assert "  map-server" not in lisp and "  map-resolver" not in lisp
        assert "\n!\n!\n" not in text.split("router lisp")[1]
        assert not any(re.search(pattern, text, re.MULTILINE) for pattern in ("l2vpn evpn", r"\bnve1\b", "^router bgp"))

    def test_instances_and_dynamic_eid_follow_the_segments(self) -> None:
        text = run_transform(AccessSwitch, _raw(ACCESS_INPUT))

        assert (
            " instance-id 50001\n  dynamic-eid VRF_A-IPV4\n   database-mapping 10.100.0.0/24 locator-set RLOC\n" in text
        )
        assert "   eid-table vrf VRF_A\n   map-cache 0.0.0.0/0 map-request\n" in text
        assert (
            " instance-id 10100\n  service ethernet\n   eid-table vlan 100\n   database-mapping mac locator-set RLOC\n"
            in text
        )

    def test_gateway_svi_carries_the_mobility_and_the_anycast_mac(self) -> None:
        text = run_transform(AccessSwitch, _raw(ACCESS_INPUT))

        assert (
            "interface Vlan100\n description Customer-A\n vrf forwarding VRF_A\n ip address 10.100.0.1 255.255.255.0\n"
            " lisp mobility VRF_A-IPV4\n mac-address 001c.7300.dc01\n"
        ) in text

    def test_no_evpn_vxlan_or_bgp_leftovers(self) -> None:
        text = run_transform(AccessSwitch, _raw(ACCESS_INPUT))

        for leftover in ("l2vpn evpn", "nve1", "router bgp", "RM-CONN-2-BGP", "EVPN-VXLAN"):
            assert leftover not in text
        assert "router ospf 1" in text  # the OSPFv2 underlay is the existing template's

    def test_every_unverified_vrf_and_router_lisp_line_has_a_marker_above_the_block(self) -> None:
        lines = run_transform(AccessSwitch, _raw(ACCESS_INPUT)).splitlines()
        vrf = lines.index("vrf definition VRF_A")
        assert MARKER in lines[vrf - 1]
        router = lines.index("router lisp")
        assert MARKER in lines[router + 1]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestContentRefusals:
    def test_an_ipv6_gateway_is_refused(self) -> None:
        activation = _activation()
        activation["segment"]["gateway"]["address"] = "2001:db8::1/64"

        with pytest.raises(ValueError, match=r"VLAN 100 has an IPv6 gateway \(service ipv6 is not rendered\)"):
            get_lisp_config(_device(), "access-switch", [activation], _inputs())

    def test_a_gateway_outside_a_vrf_with_an_l3_instance_is_refused(self) -> None:
        activation = _activation(namespace={"name": "default"})

        with pytest.raises(ValueError, match=r"the gateway of VLAN 100 is in no VRF with an L3 instance-id"):
            get_lisp_config(_device(), "access-switch", [activation], _inputs())

    def test_no_map_server_is_refused(self) -> None:
        with pytest.raises(ValueError, match=r"the fabric has no map-server \(distribution-switch\)"):
            get_lisp_config(_device(), "access-switch", [_activation()], _inputs(map_servers=()))

    def test_no_site_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the fabric has no LISP site key"):
            get_lisp_config(_device(), "access-switch", [_activation()], _inputs(site_key=None))

    def test_a_border_without_a_site_name_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the fabric has no name for the LISP site"):
            get_lisp_config(_device(), "distribution-switch", [], _inputs(site_name=None))

    def test_a_map_server_without_a_loopback_address_is_refused(self) -> None:
        deployment = {"lisp_map_servers": [{"name": "ds-c005-office01", "interfaces": []}]}

        with pytest.raises(ValueError, match="LISP map-server ds-c005-office01 has no loopback address"):
            build_lisp_inputs(deployment, "access-switch")

    def test_an_ipv6_segment_prefix_on_a_border_is_refused(self) -> None:
        deployment = {
            "segment_deployments": [{"vni": 10100, "segment": {"gateway": {"ip_prefix": {"prefix": "2001:db8::/64"}}}}]
        }

        with pytest.raises(ValueError, match=r"has an IPv6 prefix \(service ipv6 is not rendered\)"):
            build_lisp_inputs(deployment, "distribution-switch")


# ---------------------------------------------------------------------------
# Context content
# ---------------------------------------------------------------------------


class TestLispContext:
    def test_edge_context(self) -> None:
        config = get_lisp_config(
            _device(), "access-switch", [_activation(), _activation(200, 10200, gateway=None)], _inputs()
        )

        assert config is not None
        assert config["role"] == "edge"
        assert config["rloc_interface"] == "Loopback0"
        assert config["map_servers"] == ["10.250.5.1", "10.250.5.2"]
        assert config["l2_instances"] == [
            {"instance_id": 10100, "vlan_id": 100, "name": "seg-100"},
            {"instance_id": 10200, "vlan_id": 200, "name": "seg-200"},
        ]
        assert config["l3_instances"] == [{"instance_id": 50001, "vrf": "PROD", "prefixes": ["10.1.0.0/24"]}]
        assert config["anycast_gateway"]["enabled"] is True
        assert config["site"] is None

    def test_prefixes_are_grouped_per_vrf_deduplicated_and_sorted(self) -> None:
        other = {"name": "DEV", "l3_vni": 50002, "namespace_type": "non_prod"}
        activations = [
            _activation(100, 10100, "10.1.1.1/24"),
            _activation(101, 10101, "10.1.0.1/24"),
            _activation(102, 10102, "10.1.0.5/24"),
            _activation(103, 10103, "10.9.0.1/24", namespace=other),
        ]

        device = _device()
        device["capabilities"].append(_vrf_cap(other))
        config = get_lisp_config(device, "access-switch", activations, _inputs())

        assert config is not None
        assert {l3["vrf"]: l3["prefixes"] for l3 in config["l3_instances"]} == {
            "DEV": ["10.9.0.0/24"],
            "PROD": ["10.1.0.0/24", "10.1.1.0/24"],
        }

    @pytest.mark.parametrize("activations", [[], [_activation(250, None, gateway=None)]])
    def test_an_edge_without_instances_still_has_the_edge_context(self, activations: list[dict[str, Any]]) -> None:
        """A fabric edge that hosts nothing yet keeps the RLOC, map-servers and key; only the instances are empty."""
        config = get_lisp_config(_device(), "access-switch", activations, _inputs())

        assert config["enabled"] is True
        assert config["role"] == "edge"
        assert config["rloc_interface"] == "Loopback0"
        assert config["map_servers"] == ["10.250.5.1", "10.250.5.2"]
        assert config["site_key"] == "s3cr3t"
        assert config["l2_instances"] == [] and config["l3_instances"] == []
        assert config["anycast_gateway"] == {"enabled": False, "mac": "00:1c:73:00:dc:01"}
        assert config["site"] is None

    @pytest.mark.parametrize(
        ("inputs", "match"),
        [
            (_inputs(map_servers=()), "no map-server"),
            (_inputs(site_key=None), "no LISP site key"),
        ],
    )
    def test_a_hostless_edge_is_still_refused_without_map_server_or_key(self, inputs: LispInputs, match: str) -> None:
        with pytest.raises(ValueError, match=match):
            get_lisp_config(_device(), "access-switch", [], inputs)

    def test_a_border_always_has_a_context_with_its_site(self) -> None:
        config = get_lisp_config(_device(), "distribution-switch", [], _inputs())

        assert config is not None
        assert config["role"] == "border"
        assert config["l2_instances"] == [] and config["l3_instances"] == []
        assert config["anycast_gateway"] == {"enabled": False, "mac": "00:1c:73:00:dc:01"}
        assert config["site"] == {
            "name": "C005-OFFICE",
            "l3_records": [{"instance_id": 50001, "vrf": "PROD", "prefix": "10.1.0.0/24"}],
            "l2_instance_ids": [10100],
        }

    def test_the_fabric_anycast_mac_is_used(self) -> None:
        config = get_lisp_config(
            _device(), "access-switch", [_activation()], _inputs(), fabric_anycast_mac="00:00:22:22:33:33"
        )

        assert config is not None
        assert config["anycast_gateway"] == {"enabled": True, "mac": "00:00:22:22:33:33"}

    def test_the_rloc_interface_prefers_the_dedicated_vtep_loopback(self) -> None:
        device = _device()
        device["interfaces"].append(
            {"name": "Loopback1", "role": "loopback-vtep", "ip_address": {"address": "10.9.0.1/32"}}
        )

        config = get_lisp_config(device, "access-switch", [_activation()], _inputs())

        assert config is not None
        assert config["rloc_interface"] == "Loopback1"

    def test_stretched_and_arp_suppression_have_no_effect(self) -> None:
        plain = get_lisp_config(_device(), "access-switch", [_activation()], _inputs())
        activation = _activation()
        activation["segment"].update(arp_suppression=False, id="s", stretch_scope="metro")
        activation["segment"]["segment_deployments"] = [
            {"vni": 1, "deployment": {"id": "a"}},
            {"vni": 1, "deployment": {"id": "b"}},
        ]

        assert get_lisp_config(_device(), "access-switch", [activation], _inputs()) == plain


class TestBuildLispInputs:
    def test_the_border_reads_the_eid_records_of_the_active_vni_segments(self) -> None:
        """A segment without a VNI has no instance, an inline-terminated one is L2 only, one outside a VRF has no record."""
        deployment = {
            "name": "C005-OFFICE",
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

        inputs = build_lisp_inputs(deployment, "distribution-switch")

        assert inputs == LispInputs(
            map_servers=("10.250.5.1", "10.250.5.2"),
            site_name="C005-OFFICE",
            site_key="k",
            l3_records=(EidRecord(50001, "PROD", "10.1.0.0/24"),),
            l2_instance_ids=(10100, 10200, 10300),
        )

    @pytest.mark.parametrize("deployment", [None, {}, {"lisp_site_key": None, "lisp_map_servers": None}])
    def test_missing_data_gives_empty_inputs_not_errors(self, deployment: Any) -> None:
        inputs = build_lisp_inputs(deployment, "access-switch")

        assert inputs.map_servers == () and inputs.site_key is None
        assert inputs.wan_exit is False

    def test_the_wan_exit_is_the_presence_of_an_sdwan_edge_on_the_office(self) -> None:
        with_edge = {"lisp_wan_edges": [{"name": "C005-OFFICE-EDGE1"}]}

        assert build_lisp_inputs(with_edge, "access-switch").wan_exit is True
        assert build_lisp_inputs({"lisp_wan_edges": []}, "access-switch").wan_exit is False


# ---------------------------------------------------------------------------
# Role gate in BaseDeviceTransform
# ---------------------------------------------------------------------------


class TestRoleGate:
    @pytest.mark.parametrize("role", sorted(OFFICE_LISP_ROLES))
    def test_an_office_role_replaces_vxlan_with_lisp_and_never_builds_evpn(self, role: str) -> None:
        data = _office_data(role)
        data["segment_deployments"] = [_activation()]

        with patch("transforms.common.get_vxlan_config") as vxlan:
            config = _make_transform(role)._extra_config(data, "cisco_xe")

        vxlan.assert_not_called()
        assert config["vxlan"] is None
        assert config["lisp"]["l3_instances"][0]["vrf"] == "PROD"

    @pytest.mark.parametrize("role", ["leaf", "tor", "access-leaf"])
    def test_a_dc_vtep_never_runs_lisp_whatever_its_deployment_carries(self, role: str) -> None:
        """The LISP keys on a DC deployment (a stale query, a hand-edited fixture) do not select the LISP path."""
        with patch("transforms.common.get_vxlan_config", return_value={"enabled": True}) as vxlan:
            config = _make_transform(role)._extra_config(_office_data(role, "arista_eos"), "arista_eos")

        vxlan.assert_called_once()
        assert config["vxlan"] == {"enabled": True}
        assert "lisp" not in config

    def test_the_dc_vtep_roles_and_the_office_roles_are_disjoint(self) -> None:
        assert not DC_VTEP_ROLES & OFFICE_LISP_ROLES

    def test_lisp_vrfs_reach_the_group_policy_view(self) -> None:
        data = _office_data("access-switch")
        data["segment_deployments"] = [_activation()]

        with patch("transforms.common.group_policy_device_view", return_value=None) as view:
            _make_transform("access-switch")._extra_config(data, "cisco_xe")

        assert view.call_args.kwargs["vrfs"] == ["PROD"]

    def test_an_edge_without_instances_still_gets_the_lisp_key(self) -> None:
        config = _make_transform("access-switch")._extra_config(_office_data("access-switch"), "cisco_xe")

        assert config["vxlan"] is None
        assert config["lisp"]["role"] == "edge"
        assert config["lisp"]["l2_instances"] == [] and config["lisp"]["l3_instances"] == []


# ---------------------------------------------------------------------------
# Purity and shape
# ---------------------------------------------------------------------------


class TestPurityAndShape:
    def test_inputs_are_not_mutated(self) -> None:
        device, activations, inputs = _device(), [_activation(), _activation(200, 10200, "10.2.0.1/24")], _inputs()
        before = copy.deepcopy((device, activations, inputs))

        get_lisp_config(device, "distribution-switch", activations, inputs)

        assert (device, activations, inputs) == before

    def test_build_lisp_inputs_does_not_mutate_the_deployment(self) -> None:
        deployment = {
            "name": "C005-OFFICE",
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

        build_lisp_inputs(deployment, "distribution-switch")

        assert deployment == before

    def test_the_result_is_repeatable_and_owned_by_the_caller(self) -> None:
        args = (_device(), "distribution-switch", [_activation()], _inputs())
        first = get_lisp_config(*args)
        assert first is not None
        first["map_servers"].append("junk")
        first["site"]["l3_records"].clear()

        assert get_lisp_config(*args) == get_lisp_config(*copy.deepcopy(args))
        second = get_lisp_config(*args)
        assert second is not None and second["map_servers"] == ["10.250.5.1", "10.250.5.2"]

    def test_collections_are_lists_never_none(self) -> None:
        config = get_lisp_config(_device(), "distribution-switch", [_activation()], _inputs())

        assert config is not None
        for key in ("map_servers", "l2_instances", "l3_instances"):
            assert type(config[key]) is list
        assert type(config["l3_instances"][0]["prefixes"]) is list
        assert type(config["site"]["l3_records"]) is list and type(config["site"]["l2_instance_ids"]) is list

    def test_lisp_inputs_are_frozen(self) -> None:
        with pytest.raises(AttributeError):
            setattr(_inputs(), "site_key", "other")  # noqa: B010

    def test_the_context_has_no_vxlan_state(self) -> None:
        config = get_lisp_config(_device(), "access-switch", [_activation()], _inputs())

        assert config is not None
        assert not {"vxlan", "evpn", "l2_vni_mappings", "l3_vni_mappings", "vtep"} & set(config)
