"""Unit tests for the render side of an office's WAN exit (access-switch and distribution-switch on cisco_xe).

An office whose deployment has an SD-WAN edge (``lisp_wan_edges``, the edge add_sdwan_edge creates) gets the way
out of its LISP site: the distribution-switch border is the proxy-ETR/ITR, exports the site registrations into each
VRF's RIB and runs an eBGP session to the edge in the tenant VRF over a dot1q sub-interface (redistributing what the
site registered); the access-switch edges ``use-petr`` the borders. The generator that builds the session is
generators/topology/office_fabric.py; the VCO describes the edge's side from the same peering
(tests/unit/test_controller_transform.py).

Covers:
  - the signal         — only an office role with an SD-WAN edge on its deployment has a WAN exit
  - the lisp context   — the border's exit VRFs come from the site's EID records, the edge's PETRs are the map-servers
  - the rendered CLI   — golden fixtures plus the lines that must (and must not) be there
  - refusals           — a session in a VRF no hosted segment defines; a border with no locator address
  - the data center    — untouched whatever its deployment lists
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.smoke.generate_config_fixtures import (
    OFFICE_EDGE_NAME,
    OFFICE_EXIT_EDGE_ASN,
    OFFICE_EXIT_LOOPBACK,
    OFFICE_EXIT_PORT,
    OFFICE_EXIT_SWITCH_ADDRESS,
    OFFICE_EXIT_SWITCH_ASN,
    OFFICE_EXIT_VLAN,
    OFFICE_EXIT_VRF,
    build_office_wan_exit_data,
)
from tests.smoke.test_configs_smoke import _run_transform
from transforms.config.access_switch import AccessSwitch
from transforms.config.border_leaf import BorderLeaf
from transforms.config.distribution_switch import DistributionSwitch
from transforms.helpers.lisp import EidRecord, LispInputs, build_lisp_inputs, get_lisp_config
from utils.exchange_transit import wan_exit_vlan

CONFIGS_DIR = Path(__file__).resolve().parents[2] / "tests" / "smoke" / "configs"
MARKER = "unverified-no-lab:"
ACCESS_EXIT = "access_switch_cisco_xe_lisp_wan_exit"
DISTRIBUTION_EXIT = "distribution_switch_cisco_xe_lisp_wan_exit"
ACCESS_PLAIN = "access_switch_cisco_xe_lisp_ospf"
DISTRIBUTION_PLAIN = "distribution_switch_cisco_xe_lisp_ospf"
DC_BORDER = "border_leaf_cisco_xe_ospf_ibgp"

_MAP_SERVERS: list[dict[str, Any]] = [
    {"name": "ds-c005-office02", "interfaces": [{"ip_address": {"address": "10.250.5.2/32"}}]},
    {"name": "ds-c005-office01", "interfaces": [{"ip_address": {"address": "10.250.5.1/32"}}]},
]
_EDGES = [{"name": OFFICE_EDGE_NAME}]


def _golden(directory: str) -> tuple[str, str]:
    return (CONFIGS_DIR / directory / "input.json").read_text(), (CONFIGS_DIR / directory / "output.txt").read_text()


def _raw(directory: str) -> dict[str, Any]:
    return json.loads(_golden(directory)[0])


def _deployment(data: dict[str, Any]) -> dict[str, Any]:
    return data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]


def _inputs(**overrides: Any) -> LispInputs:
    values: dict[str, Any] = {
        "map_servers": ("10.250.5.1", "10.250.5.2"),
        "site_name": "C005-OFFICE",
        "site_key": "0ff1c3-k3y",
        "l3_records": (EidRecord(instance_id=50001, vrf="VRF_A", prefix="10.100.0.0/24"),),
        "l2_instance_ids": (10100,),
        "wan_exit": True,
    }
    values.update(overrides)
    return LispInputs(**values)


def _device(*, loopback: str | None = "10.250.5.1/32") -> dict[str, Any]:
    address = {"ip_address": {"address": loopback}} if loopback else {}
    return {"name": "ds-c005-office01", "interfaces": [{"name": "Loopback0", "role": "loopback", **address}]}


def _block(text: str, header: str) -> list[str]:
    """The lines of one stanza: from ``header`` to the next line indented no deeper than it."""
    lines = text.splitlines()
    start = lines.index(header)
    depth = len(header) - len(header.lstrip())
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) <= depth
        ),
        len(lines),
    )
    return lines[start + 1 : end]


class TestTheSignal:
    @pytest.mark.parametrize("role", ["access-switch", "distribution-switch"])
    def test_an_office_role_with_an_sdwan_edge_has_the_exit(self, role: str) -> None:
        deployment = {"lisp_map_servers": _MAP_SERVERS, "lisp_site_key": {"password": "k"}, "lisp_wan_edges": _EDGES}

        assert build_lisp_inputs(deployment, role).wan_exit is True

    @pytest.mark.parametrize("edges", [None, []])
    def test_an_office_without_an_sdwan_edge_has_none(self, edges: Any) -> None:
        deployment = {"lisp_map_servers": _MAP_SERVERS, "lisp_site_key": {"password": "k"}, "lisp_wan_edges": edges}

        assert build_lisp_inputs(deployment, "distribution-switch").wan_exit is False

    def test_the_default_is_no_exit(self) -> None:
        assert LispInputs(("1.1.1.1",), None, "k", (), ()).wan_exit is False

    def test_the_fragment_is_spread_by_the_office_queries_only(self) -> None:
        queries = Path(__file__).resolve().parents[2] / "queries" / "config"
        spreading = sorted(path.stem for path in queries.glob("*.gql") if "LispSiteOnOfficeFields" in path.read_text())

        assert spreading == ["access_switch", "distribution_switch"]


class TestLispContext:
    def test_the_border_is_the_exit_of_every_vrf_the_site_registers(self) -> None:
        """A distribution-switch hosts no segment port, yet it exits from the site's VRFs."""
        config = get_lisp_config(_device(), "distribution-switch", [], _inputs())

        assert config["wan_exit"] is True
        assert config["rloc_address"] == "10.250.5.1"
        assert config["l3_instances"] == [{"instance_id": 50001, "vrf": "VRF_A", "prefixes": []}]

    def test_the_exit_vrfs_are_sorted_by_instance_id_and_not_repeated(self) -> None:
        records = (
            EidRecord(60000, "PROD", "10.1.0.0/24"),
            EidRecord(50001, "VRF_A", "10.100.0.0/24"),
            EidRecord(60000, "PROD", "10.2.0.0/24"),
        )

        config = get_lisp_config(_device(), "distribution-switch", [], _inputs(l3_records=records))

        assert [(i["instance_id"], i["vrf"]) for i in config["l3_instances"]] == [(50001, "VRF_A"), (60000, "PROD")]

    def test_a_vrf_the_border_already_hosts_keeps_its_prefixes(self) -> None:
        activation = {
            "vlan_id": 100,
            "vni": 10100,
            "status": "active",
            "segment": {
                "name": "seg-100",
                "customer_name": "seg-100",
                "gateway": {
                    "address": "10.100.0.1/24",
                    "ip_prefix": {"ip_namespace": {"name": "VRF_A", "l3_vni": 50001}},
                },
            },
        }

        config = get_lisp_config(_device(), "distribution-switch", [activation], _inputs())

        assert config["l3_instances"] == [{"instance_id": 50001, "vrf": "VRF_A", "prefixes": ["10.100.0.0/24"]}]

    def test_without_the_exit_the_border_has_no_instance_of_its_own(self) -> None:
        config = get_lisp_config(_device(), "distribution-switch", [], _inputs(wan_exit=False))

        assert config["wan_exit"] is False
        assert config["l3_instances"] == []

    def test_an_edge_gets_the_flag_and_no_extra_instance(self) -> None:
        config = get_lisp_config(_device(), "access-switch", [], _inputs())

        assert config["wan_exit"] is True
        assert config["role"] == "edge"
        assert config["l3_instances"] == []

    def test_a_border_without_a_locator_address_is_refused(self) -> None:
        with pytest.raises(ValueError, match="the WAN exit needs an address on the locator loopback"):
            get_lisp_config(_device(loopback=None), "distribution-switch", [], _inputs())

    def test_an_edge_needs_no_locator_address_for_the_exit(self) -> None:
        get_lisp_config(_device(loopback=None), "access-switch", [], _inputs())


class TestRenderedAccessSwitch:
    def test_it_uses_every_border_as_petr_in_service_ipv4_only(self) -> None:
        text = _golden(ACCESS_EXIT)[1]

        assert _block(text, " service ipv4").count("  use-petr 10.250.5.1") == 1
        assert "  use-petr 10.250.5.2" in _block(text, " service ipv4")
        assert not any("use-petr" in line for line in _block(text, " service ethernet"))

    def test_the_petrs_are_the_map_servers_so_there_is_no_second_list(self) -> None:
        text = _golden(ACCESS_EXIT)[1]

        petrs = re.findall(r"use-petr (\S+)", text)
        map_servers = re.findall(
            r"itr map-resolver (\S+)", _block(text, " service ipv4").__str__().replace("', '", "\n")
        )

        assert petrs == ["10.250.5.1", "10.250.5.2"]
        assert petrs == sorted(set(map_servers))

    def test_the_default_map_request_stays_it_is_what_sends_a_packet_to_the_petr(self) -> None:
        text = _golden(ACCESS_EXIT)[1]

        assert text.count("map-cache 0.0.0.0/0 map-request") == text.count("eid-table vrf")

    def test_an_office_without_the_exit_renders_no_petr(self) -> None:
        assert "use-petr" not in _golden(ACCESS_PLAIN)[1]

    def test_it_differs_from_the_plain_render_by_the_petr_lines_only(self) -> None:
        plain = _golden(ACCESS_PLAIN)[1].splitlines()
        exit_ = _golden(ACCESS_EXIT)[1].splitlines()

        added = [line for line in exit_ if line not in plain]

        assert [line.strip().split(" ")[0] for line in added] == ["!", "use-petr", "use-petr"]
        assert [line for line in plain if line not in exit_] == []


class TestRenderedDistributionSwitch:
    def test_the_border_is_the_proxy_etr_and_itr_in_service_ipv4_only(self) -> None:
        text = _golden(DISTRIBUTION_EXIT)[1]

        assert "  proxy-etr" in _block(text, " service ipv4")
        assert f"  proxy-itr {OFFICE_EXIT_LOOPBACK.split('/')[0]}" in _block(text, " service ipv4")
        assert not any(re.search(r"proxy-(etr|itr)", line) for line in _block(text, " service ethernet"))

    def test_the_site_registrations_are_exported_into_the_vrf_rib(self) -> None:
        instance = _block(_golden(DISTRIBUTION_EXIT)[1], " instance-id 50001")

        assert "   route-export site-registrations" in instance
        assert "   distance site-registrations 250" in instance
        assert "   map-cache site-registration" in instance

    def test_the_vrf_has_the_route_distinguisher_the_bgp_address_family_needs(self) -> None:
        text = _golden(DISTRIBUTION_EXIT)[1]

        assert f"vrf definition {OFFICE_EXIT_VRF}\n" in text
        assert f" rd {OFFICE_EXIT_LOOPBACK.split('/')[0]}:50001\n" in text

    def test_the_session_is_ebgp_in_the_vrf_redistributing_lisp_toward_the_edge(self) -> None:
        text = _golden(DISTRIBUTION_EXIT)[1]
        family = _block(text, f"  address-family ipv4 vrf {OFFICE_EXIT_VRF}")

        assert f"router bgp {OFFICE_EXIT_SWITCH_ASN}\n" in text
        assert "    redistribute lisp metric 10" in family
        assert f"    neighbor 100.66.0.1 remote-as {OFFICE_EXIT_EDGE_ASN}" in family
        assert "    neighbor 100.66.0.1 activate" in family
        assert "    neighbor 100.66.0.1 password 0ff1c3-w4n-k3y" in family

    def test_nothing_else_is_redistributed_the_connected_route_map_is_not_even_defined(self) -> None:
        text = _golden(DISTRIBUTION_EXIT)[1]

        assert "RM-CONN-2-BGP" not in text
        assert "redistribute connected" not in text
        assert _block(text, "  address-family ipv4 unicast") == ["  exit-address-family"] or not _block(
            text, "  address-family ipv4 unicast"
        )

    def test_the_handoff_is_a_dot1q_sub_interface_in_the_vrf_on_a_routed_port(self) -> None:
        text = _golden(DISTRIBUTION_EXIT)[1]
        sub = f"{OFFICE_EXIT_PORT}.{OFFICE_EXIT_VLAN}"
        address, length = OFFICE_EXIT_SWITCH_ADDRESS.split("/")

        sub_lines = [line.strip() for line in _block(text, f"interface {sub}")]
        assert f"encapsulation dot1Q {OFFICE_EXIT_VLAN}" in sub_lines
        assert f"vrf forwarding {OFFICE_EXIT_VRF}" in sub_lines
        assert length == "31" and f"ip address {address} 255.255.255.254" in sub_lines
        assert "no switchport" in [line.strip() for line in _block(text, f"interface {OFFICE_EXIT_PORT}")]

    def test_a_port_with_no_sub_interface_is_not_made_routed(self) -> None:
        text = _golden(DISTRIBUTION_PLAIN)[1]

        assert "dot1Q" not in text and "routed port" not in text

    def test_the_fixture_follows_the_generators_naming_and_tag(self) -> None:
        """The tag is the VRF's (3800 for a prod VRF); the sub-interface is <port>.<tag>."""
        assert OFFICE_EXIT_VLAN == wan_exit_vlan("prod")
        assert f"interface {OFFICE_EXIT_PORT}.{OFFICE_EXIT_VLAN}\n" in _golden(DISTRIBUTION_EXIT)[1]
        assert OFFICE_EXIT_SWITCH_ASN != OFFICE_EXIT_EDGE_ASN
        assert min(OFFICE_EXIT_SWITCH_ASN, OFFICE_EXIT_EDGE_ASN) >= 4200000000  # PRIVATE-ASN32

    def test_the_goldens_are_what_the_writer_and_the_transform_produce(self) -> None:
        for cls, role, directory, name in (
            (AccessSwitch, "access-switch", ACCESS_EXIT, "as-c005-office01"),
            (DistributionSwitch, "distribution-switch", DISTRIBUTION_EXIT, "ds-c005-office01"),
        ):
            data = build_office_wan_exit_data(device_name=name, role=role)

            assert data == _raw(directory)
            assert _run_transform(cls, data).rstrip("\n") + "\n" == _golden(directory)[1]

    @pytest.mark.parametrize(
        ("directory", "keyword", "within"),
        [
            (ACCESS_EXIT, "use-petr", 3),
            (DISTRIBUTION_EXIT, "proxy-etr", 2),
            (DISTRIBUTION_EXIT, "proxy-itr", 2),
            (DISTRIBUTION_EXIT, "route-export site-registrations", 2),
            (DISTRIBUTION_EXIT, "distance site-registrations", 3),
            (DISTRIBUTION_EXIT, "map-cache site-registration", 4),
            (DISTRIBUTION_EXIT, "redistribute lisp", 2),
            (DISTRIBUTION_EXIT, " rd ", 2),
            (DISTRIBUTION_EXIT, "encapsulation dot1Q", 2),
            (DISTRIBUTION_EXIT, "no switchport", 3),
        ],
    )
    def test_every_exit_line_sits_under_an_unverified_marker(self, directory: str, keyword: str, within: int) -> None:
        """No lab has run any of it: each construct has a ``! unverified-no-lab:`` comment just above it."""
        lines = _golden(directory)[1].splitlines()
        checked = 0
        for index, line in enumerate(lines):
            if keyword not in line or not line.startswith(" ") or line.strip().startswith("!"):
                continue
            if keyword == "no switchport" and not any(OFFICE_EXIT_PORT in lines[j] for j in range(index - 3, index)):
                continue  # the P2P ports of the underlay are routed already, not part of the exit
            comments = [lines[j] for j in range(index - 1, index - 1 - within, -1) if lines[j].strip().startswith("!")]
            assert comments and MARKER in comments[0], f"{keyword!r} on line {index + 1} has no marker within {within}"
            checked += 1
        assert checked, f"{keyword!r} is not rendered in {directory}"


class TestRefusals:
    def test_a_session_in_a_vrf_no_hosted_segment_defines_is_refused(self) -> None:
        """The LISP template defines the VRFs of its L3 instances; a BGP session elsewhere would render under none."""
        data = _raw(DISTRIBUTION_EXIT)
        _deployment(data)["segment_deployments"] = {"edges": []}

        with pytest.raises(
            ValueError, match=rf"a BGP session in VRF {OFFICE_EXIT_VRF}, which no hosted segment defines"
        ):
            _run_transform(DistributionSwitch, data)

    def test_the_refusal_names_the_role_and_asks_for_no_partial_config(self) -> None:
        data = _raw(DISTRIBUTION_EXIT)
        _deployment(data)["segment_deployments"] = {"edges": []}

        with pytest.raises(
            ValueError, match=r"^cisco_xe distribution-switch cannot render: .*refusing incomplete config$"
        ):
            _run_transform(DistributionSwitch, data)

    def test_a_border_with_no_address_on_its_locator_is_refused(self) -> None:
        data = _raw(DISTRIBUTION_EXIT)
        for edge in data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]:
            if edge["node"]["name"]["value"] == "Loopback0":
                edge["node"]["ip_address"] = {"node": None}

        with pytest.raises(ValueError, match="the WAN exit needs an address on the locator loopback"):
            _run_transform(DistributionSwitch, data)


class TestDataCenterIsUnchanged:
    def test_a_dc_border_leaf_ignores_an_edge_on_its_deployment(self) -> None:
        """A DC VTEP never takes the LISP path: an edge (or LISP keys) on its deployment changes nothing."""
        plain = _run_transform(BorderLeaf, _raw(DC_BORDER))
        data = _raw(DC_BORDER)
        _deployment(data)["lisp_wan_edges"] = {"edges": [{"node": {"name": {"value": OFFICE_EDGE_NAME}}}]}

        assert _run_transform(BorderLeaf, data) == plain
        assert "proxy-etr" not in plain and "use-petr" not in plain

    def test_a_dc_golden_has_no_exit_line(self) -> None:
        checked = 0
        for path in CONFIGS_DIR.glob("*_cisco_xe_*"):
            if path.name.startswith(("access_switch", "distribution_switch")):
                continue
            text = (path / "output.txt").read_text()
            assert not re.search(r"proxy-etr|use-petr|redistribute lisp|route-export|router lisp", text), path.name
            checked += 1
        assert checked > 10
