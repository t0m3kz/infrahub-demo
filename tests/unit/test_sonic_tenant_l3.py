"""Unit tests for the SONiC tenant L3 config-DB (templates/configs/common/sonic_tenant_l3.j2).

Renders the real leaf / border-leaf SONiC templates through the transforms'
own _load_template, fed a narrow context, so each case pins one rule:
gateway SVIs, L3 VNI through its VLAN, per-VRF BGP and EVPN type-5, the
exchange statics, and the two EVPN filters (a border's exported statics, a
leaf's own transit /29s). The full-transform paths are the smoke fixtures
border_leaf_*sonic_exchange_transit and leaf_*sonic_pbr.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from transforms.config.border_leaf import BorderLeaf
from transforms.config.leaf import Leaf

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAC = "00:1c:73:00:dc:01"


def _vlan(vlan_id: int, gateway: str | None, vrf: str | None = "PROD", **extra: Any) -> dict[str, Any]:
    return {
        "vlan_id": vlan_id,
        "gateway_ip": gateway,
        "gateway_ipv6": None,
        "vrf": vrf,
        "terminate_inline": False,
    } | extra


def _vxlan(*vrfs: tuple[str, int, int | None], anycast: bool = True) -> dict[str, Any]:
    return {
        "enabled": True,
        "interface": "vtep",
        "vtep": {"ipv4": "10.0.0.1"},
        "l2_vni_mappings": [],
        "l3_vni_mappings": [{"vrf_name": n, "l3_vni": vni, "svi_vlan_id": svi} for n, vni, svi in vrfs],
        "evpn": {"enabled": True},
        "anycast_gateway": {"enabled": anycast, "mac": _MAC},
    }


def _render(
    *,
    border: bool = False,
    vlans: list[dict[str, Any]] | None = None,
    vxlan: dict[str, Any] | None = None,
    vrf_routes: list[dict[str, Any]] | None = None,
    leaf_transit_prefixes: list[str] | None = None,
    bgp: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    transform_cls = BorderLeaf if border else Leaf
    transform = transform_cls.__new__(transform_cls)
    transform.root_directory = str(_REPO_ROOT)
    out = transform._load_template("sonic").render(
        hostname="dc1-leaf-01",
        interfaces=[],
        bgp=bgp if bgp is not None else [{"local_as": {"asn": 65000}, "router_ip": "10.0.0.1", "sessions": []}],
        vlans=vlans if vlans is not None else [_vlan(100, "10.1.0.1/24")],
        vxlan=vxlan if vxlan is not None else _vxlan(("PROD", 50001, 3900)),
        vrf_routes=vrf_routes or [],
        leaf_transit_prefixes=leaf_transit_prefixes or [],
        acls=[],
        leaf_pbr_rules=[],
    )
    return json.loads(out)


class TestGatewaySvis:
    def test_routed_segment_gets_vrf_anycast_flag_and_gateway_address(self) -> None:
        config = _render()
        assert config["VLAN_INTERFACE"]["Vlan100"] == {"vrf_name": "PROD", "static_anycast_gateway": "true"}
        assert "Vlan100|10.1.0.1/24" in config["VLAN_INTERFACE"]
        assert config["SAG"] == {"GLOBAL": {"gateway_mac": _MAC}}

    def test_ipv6_gateway_is_a_second_address_and_turns_on_the_ipv6_family(self) -> None:
        config = _render(vlans=[_vlan(100, "10.1.0.1/24", gateway_ipv6="fd00:1::1/64")])
        assert "Vlan100|fd00:1::1/64" in config["VLAN_INTERFACE"]
        assert config["BGP_GLOBALS_AF"]["PROD|ipv6_unicast"]["redistribute_connected"] == "true"
        assert config["BGP_GLOBALS_AF"]["PROD|l2vpn_evpn"]["advertise-ipv6-unicast"] == "true"

    def test_without_anycast_gateway_there_is_no_sag(self) -> None:
        config = _render(vxlan=_vxlan(("PROD", 50001, 3900), anycast=False))
        assert config["VLAN_INTERFACE"]["Vlan100"] == {"vrf_name": "PROD"}
        assert "SAG" not in config

    def test_l2_only_and_inline_terminated_vlans_get_no_svi_address(self) -> None:
        config = _render(vlans=[_vlan(100, None), _vlan(200, "10.2.0.1/24", terminate_inline=True)])
        assert config["VLAN_INTERFACE"]["Vlan100"] == {}
        assert "Vlan200" not in config["VLAN_INTERFACE"]
        assert not [key for key in config["VLAN_INTERFACE"] if "|" in key]


class TestL3Vni:
    def test_l3_vni_maps_through_its_svi_vlan_bound_to_the_vrf(self) -> None:
        config = _render()
        assert config["VXLAN_TUNNEL_MAP"]["vtep|map_50001_Vlan3900"] == {"vlan": "Vlan3900", "vni": "50001"}
        assert config["VLAN"]["Vlan3900"] == {"vlanid": "3900"}
        assert config["VLAN_INTERFACE"]["Vlan3900"] == {"vrf_name": "PROD"}
        assert config["VRF"]["PROD"] == {"vni": "50001"}

    def test_vrf_past_the_svi_band_gets_no_l3_vni_map(self) -> None:
        config = _render(vxlan=_vxlan(("PROD", 50001, None)))
        assert not [key for key in config["VXLAN_TUNNEL_MAP"] if "50001" in key]
        assert config["VRF"]["PROD"] == {"vni": "50001"}


class TestVrfBgp:
    def test_each_vrf_gets_a_bgp_instance_and_an_evpn_type5_export(self) -> None:
        config = _render(vxlan=_vxlan(("PROD", 50001, 3900), ("INTERNET", 50003, 3901)))
        for vrf in ("PROD", "INTERNET"):
            assert config["BGP_GLOBALS"][vrf] == {"local_asn": 65000, "router_id": "10.0.0.1"}
            assert config["BGP_GLOBALS_AF"][f"{vrf}|ipv4_unicast"] == {
                "admin_status": "true",
                "redistribute_connected": "true",
            }
            assert config["BGP_GLOBALS_AF"][f"{vrf}|l2vpn_evpn"] == {
                "admin_status": "true",
                "advertise-ipv4-unicast": "true",
            }

    def test_the_vrf_gets_an_explicit_evpn_route_distinguisher_and_route_target(self) -> None:
        """frrcfgd: BGP_GLOBALS_AF `<vrf>|l2vpn_evpn` field route-distinguisher, BGP_GLOBALS_EVPN_RT
        `<vrf>|l2vpn_evpn|<rt>` with route-target-type (sonic-buildimage 77eafbef, documented in sonic_vxlan.j2)."""
        vxlan = _vxlan(("PROD", 50001, 3900))
        vxlan["evpn"] = {"enabled": True, "rd_format": "10.0.0.7:{vni}"}
        vxlan["l3_vni_mappings"][0]["route_target"] = "65100:50001"
        vxlan["l3_vni_mappings"][0]["import_route_targets"] = []
        config = _render(vxlan=vxlan)
        assert config["BGP_GLOBALS_AF"]["PROD|l2vpn_evpn"]["route-distinguisher"] == "10.0.0.7:50001"
        assert config["BGP_GLOBALS_EVPN_RT"] == {"PROD|l2vpn_evpn|65100:50001": {"route-target-type": "both"}}
        assert "PROD" in config["BGP_GLOBALS"], "the handlers run `router bgp <local_asn> vrf <vrf>`"

    def test_no_route_distinguisher_without_a_router_id(self) -> None:
        config = _render(vxlan=_vxlan(("PROD", 50001, 3900)))
        assert "route-distinguisher" not in config["BGP_GLOBALS_AF"]["PROD|l2vpn_evpn"]

    def test_vrf_instance_merges_into_a_vrf_handoff_session_entry(self) -> None:
        """sonic_common.j2 already creates BGP_GLOBALS[vrf] for a VRF-scoped session; it is kept."""
        session = {"neighbor": "100.64.0.1", "remote_as": {"asn": 65100}, "vrf": "PROD"}
        bgp = [{"local_as": {"asn": 65000}, "router_ip": "10.0.0.1", "sessions": [session]}]
        config = _render(bgp=bgp)
        assert config["BGP_GLOBALS"]["PROD"]["local_asn"] == 65000
        assert "PROD|100.64.0.1" in config["BGP_NEIGHBOR"]

    def test_no_bgp_means_no_vrf_instance(self) -> None:
        config = _render(bgp=[])
        assert "PROD" not in config["BGP_GLOBALS"]
        assert "PROD|l2vpn_evpn" not in config["BGP_GLOBALS_AF"]


_ROUTES = [
    {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.4"},
    {"vrf": "PROD", "l3_vni": 50001, "prefix": "0.0.0.0/0", "nexthop": "100.66.0.12"},
    {"vrf": "PROD", "l3_vni": 50001, "prefix": "10.9.0.0/16", "nexthop": "100.66.0.4", "local_only": True},
]


class TestExchangeStatics:
    def test_statics_are_keyed_by_vrf_and_prefix_with_ecmp_next_hops(self) -> None:
        config = _render(border=True, vrf_routes=_ROUTES)
        assert config["STATIC_ROUTE"] == {
            "PROD|0.0.0.0/0": {"nexthop": "100.66.0.4,100.66.0.12"},
            "PROD|10.9.0.0/16": {"nexthop": "100.66.0.4"},
        }

    def test_border_exports_its_statics_except_local_only(self) -> None:
        config = _render(border=True, vrf_routes=_ROUTES)
        af = config["BGP_GLOBALS_AF"]["PROD|ipv4_unicast"]
        assert af["redistribute_static"] == "true"
        assert af["route_map_redistribute_static"] == "RM-VRF-STATIC-2-EVPN-PROD"
        assert config["ROUTE_MAP"]["RM-VRF-STATIC-2-EVPN-PROD|10"] == {
            "route_operation": "permit",
            "match_prefix_set": "PL-VRF-STATIC-PROD",
        }
        assert config["PREFIX"] == {"PL-VRF-STATIC-PROD|10|0.0.0.0/0|exact": {"action": "permit"}}

    def test_only_local_only_statics_are_not_exported(self) -> None:
        config = _render(border=True, vrf_routes=[_ROUTES[2]])
        assert "PROD|10.9.0.0/16" in config["STATIC_ROUTE"]
        assert "redistribute_static" not in config["BGP_GLOBALS_AF"]["PROD|ipv4_unicast"]
        assert "ROUTE_MAP" not in config

    def test_a_leaf_never_redistributes_statics(self) -> None:
        config = _render(vrf_routes=_ROUTES)
        assert "redistribute_static" not in config["BGP_GLOBALS_AF"]["PROD|ipv4_unicast"]


class TestLeafTransitFilter:
    def test_leaf_keeps_its_transit_29s_out_of_evpn(self) -> None:
        config = _render(
            vlans=[_vlan(100, "10.1.0.1/24"), _vlan(3000, "100.66.0.1/29")],
            leaf_transit_prefixes=["100.66.0.0/29"],
        )
        assert config["BGP_GLOBALS_AF"]["PROD|ipv4_unicast"]["route_map_redistribute_connected"] == (
            "RM-VRF-CONN-2-EVPN"
        )
        assert config["ROUTE_MAP"] == {
            "RM-VRF-CONN-2-EVPN|5": {"route_operation": "deny", "match_prefix_set": "PL-XCHG-TRANSIT"},
            "RM-VRF-CONN-2-EVPN|10": {"route_operation": "permit"},
        }
        assert config["PREFIX"] == {"PL-XCHG-TRANSIT|10|100.66.0.0/29|exact": {"action": "permit"}}
        assert "Vlan3000|100.66.0.1/29" in config["VLAN_INTERFACE"]

    def test_a_border_originates_its_transits(self) -> None:
        config = _render(border=True, leaf_transit_prefixes=["100.66.0.0/29"])
        assert "route_map_redistribute_connected" not in config["BGP_GLOBALS_AF"]["PROD|ipv4_unicast"]
        assert "ROUTE_MAP" not in config

    @pytest.mark.parametrize("border", [False, True])
    def test_no_route_map_tables_without_anything_to_filter(self, border: bool) -> None:
        config = _render(border=border)
        assert not {"ROUTE_MAP", "PREFIX_SET", "PREFIX"} & set(config)
