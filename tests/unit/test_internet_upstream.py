"""Unit tests for the INTERNET upstream handoff (ISP -> BGW edge -> EVPN type-5).

Covers:
  - _build_session_from_peering()  — ``transit`` flag on a namespace_type internet VRF session
  - get_vxlan_config()             — INTERNET L3 VNI on a BGW edge, one global route-target
  - get_exchange_transits()        — border-leaf INTERNET mapping carries namespace_type
  - edges/cisco_nxos.j2            — transit filters and the shared VXLAN VRF stanza
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest

from transforms.helpers.bgp import _build_session_from_peering, add_template_fields
from transforms.helpers.vxlan import get_vxlan_config

_TEMPLATES_CONFIGS_DIR = Path(__file__).parents[2] / "templates" / "configs"
_INTERNET_NS = {"name": "INTERNET", "l3_vni": 60002, "namespace_type": "internet"}


def _transit_peering(namespace: dict[str, Any]) -> dict[str, Any]:
    """The eg-fr201 <-> EXT-LUMEN-FRA1 peering, local address in ``namespace``."""
    return {
        "name": "TRANSIT-LUMEN-FRA1",
        "session_type": "EBGP",
        "peering_role": "regular",
        "ttl": 1,
        "maximum_routes": 100,
        "capabilities": [
            {
                "name": "Ethernet1/14",
                "device": {"name": "eg-fr201"},
                "ip_address": {"address": "203.0.113.0/31", "ip_namespace": namespace},
            },
            {
                "name": "eth1",
                "device": {"name": "EXT-LUMEN-FRA1"},
                "ip_address": {"address": "203.0.113.1/31", "ip_namespace": namespace},
            },
        ],
        "bgp_processes": [
            {"local_as": {"asn": 4200000100}, "capabilities": [{"name": "eg-fr201"}]},
            {"local_as": {"asn": 3356}, "capabilities": [{"name": "EXT-LUMEN-FRA1"}]},
        ],
    }


def _edge(*, bgw: bool = True, namespace: dict[str, Any] | None = _INTERNET_NS) -> dict[str, Any]:
    """Cleaned edge data: Loopback0 VTEP, optional multisite VIP, one transit peering."""
    interfaces: list[dict[str, Any]] = [
        {"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}}
    ]
    if bgw:
        interfaces.append({"name": "Loopback1", "role": "multisite-vip", "ip_address": {"address": "10.0.0.50/32"}})
    capabilities = []
    vrfs = []
    if namespace:
        capabilities.append({"typename": "ManagedBGP", "peerings": [_transit_peering(namespace)]})
        # the VRF generator's membership of the session's namespace: the device's only VRF set
        vrfs.append({"namespace": namespace})
    return {"name": "eg-fr201", "interfaces": interfaces, "capabilities": capabilities, "vrfs": vrfs}


class TestTransitFlag:
    def test_internet_namespace_flags_the_vrf_session_transit(self) -> None:
        session = _build_session_from_peering(_transit_peering(_INTERNET_NS), "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == "INTERNET"
        assert session["transit"] is True

    @pytest.mark.parametrize("namespace_type", ["prod", None])
    def test_other_namespaces_are_not_transit(self, namespace_type: str | None) -> None:
        namespace = {"name": "PROD", "namespace_type": namespace_type}
        session = _build_session_from_peering(_transit_peering(namespace), "eg-fr201", {"asn": 4200000100}, [])
        assert session is not None
        assert session["vrf"] == "PROD"
        assert "transit" not in session


class TestInternetVniMapping:
    def test_bgw_with_internet_session_gets_the_l3_vni_and_global_rt(self) -> None:
        config = get_vxlan_config(_edge(), "cisco_nxos", device_role="edge", activations=[], fabric_rt_asn=65000)
        assert config is not None
        (mapping,) = config["l3_vni_mappings"]
        assert mapping["vrf_name"] == "INTERNET"
        assert mapping["l3_vni"] == 60002
        # {l3_vni}:{l3_vni}, not the fabric's {evpn_rt_as}:{l3_vni}
        assert mapping["route_target"] == "60002:60002"
        assert mapping["import_route_targets"] == []
        assert config["multisite"]["site_id"] == 65000

    def test_global_rt_needs_no_fabric_rt_asn(self) -> None:
        config = get_vxlan_config(_edge(), "cisco_nxos", device_role="edge", activations=[], fabric_rt_asn=65000)
        assert config is not None
        assert config["l3_vni_mappings"][0]["route_target"] == "60002:60002"

    def test_edge_that_is_not_a_bgw_has_no_vxlan(self) -> None:
        assert get_vxlan_config(_edge(bgw=False), "cisco_nxos", device_role="edge", fabric_rt_asn=65000) is None

    def test_bgw_without_internet_session_has_no_vxlan(self) -> None:
        assert get_vxlan_config(_edge(namespace=None), "cisco_nxos", device_role="edge", fabric_rt_asn=65000) is None

    def test_non_internet_session_namespace_is_ignored(self) -> None:
        namespace = {"name": "PROD", "l3_vni": 50001, "namespace_type": "prod"}
        assert (
            get_vxlan_config(_edge(namespace=namespace), "cisco_nxos", device_role="edge", fabric_rt_asn=65000) is None
        )

    def test_bgw_without_evpn_rt_as_is_no_bgw_so_no_internet_vni(self) -> None:
        assert get_vxlan_config(_edge(), "cisco_nxos", device_role="edge", fabric_rt_asn=None) is None

    def test_tenant_vrf_keeps_the_fabric_rt_and_internet_the_global_one(self) -> None:
        """A border leaf's INTERNET transit leg and a PROD segment side by side."""
        activations = [
            {
                "vlan_id": vlan,
                "vni": vni,
                "segment": {
                    "id": name,
                    "name": name,
                    "gateway": {"address": "10.0.0.1/24", "ip_prefix": {"prefix": "10.0.0.0/24", "ip_namespace": ns}},
                },
            }
            for vlan, vni, name, ns in (
                (100, 10100, "seg", {"name": "PROD", "l3_vni": 50001, "namespace_type": "prod"}),
                (3000, 13000, "XCHG-INTERNET", _INTERNET_NS),
            )
        ]
        device = {
            "name": "dc1-border-01",
            "interfaces": [{"name": "Loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}}],
            "vrfs": [
                {"namespace": {"name": "PROD", "l3_vni": 50001, "namespace_type": "prod"}},
                {"namespace": _INTERNET_NS},
            ],
        }
        config = get_vxlan_config(
            device, "cisco_nxos", device_role="border-leaf", activations=activations, fabric_rt_asn=65000
        )
        assert config is not None
        rts = {m["vrf_name"]: m["route_target"] for m in config["l3_vni_mappings"]}
        assert rts == {"INTERNET": "60002:60002", "PROD": "65000:50001"}


@pytest.fixture
def env() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)))


def _bgp(namespace: dict[str, Any]) -> list[dict[str, Any]]:
    session = _build_session_from_peering(_transit_peering(namespace), "eg-fr201", {"asn": 4200000100}, [])
    assert session is not None
    session["address_families"] = ["ipv4"]
    # `advertise l2vpn evpn` is only rendered by the process that carries the EVPN address-family.
    evpn = {
        "name": "overlay",
        "session_type": "EBGP",
        "ttl": 2,
        "neighbor": "10.0.0.100",
        "remote_as": {"asn": 65100},
        "address_families": ["evpn"],
    }
    bgp = [{"local_as": {"asn": 4200000100}, "router_id": {"address": "10.0.0.1/32"}, "sessions": [evpn, session]}]
    add_template_fields(bgp[0])
    return bgp


class TestEdgeTemplate:
    def _render(self, env: jinja2.Environment, namespace: dict[str, Any], vxlan: dict[str, Any] | None) -> str:
        return env.get_template("edges/cisco_nxos.j2").render(
            name="eg-fr201",
            interfaces=[],
            loopback_name="loopback0",
            vlans=[],
            ospf=[],
            bgp=_bgp(namespace),
            vxlan=vxlan,
            # The transform's plain_vrf_names: the instances without an L3 VNI (a border gateway has INTERNET's).
            plain_vrfs=[] if vxlan else [namespace["name"]],
        )

    def _vxlan(self) -> dict[str, Any]:
        config = get_vxlan_config(_edge(), "cisco_nxos", device_role="edge", activations=[], fabric_rt_asn=65000)
        assert config is not None
        return config

    def test_bgw_renders_internet_vni_rt_and_evpn_advertisement(self, env: jinja2.Environment) -> None:
        rendered = self._render(env, _INTERNET_NS, self._vxlan())
        assert "vrf context INTERNET\n  vni 60002\n" in rendered
        assert rendered.count("vrf context INTERNET") == 1  # the shared VXLAN stanza replaces the plain one
        assert "route-target both 60002:60002 evpn" in rendered
        assert "advertise l2vpn evpn" in rendered
        assert "redistribute direct route-map RM-VRF-DIRECT-2-EVPN" not in rendered.split("vrf INTERNET")[1]
        assert "member vni 60002 associate-vrf" in rendered

    def test_transit_session_is_filtered(self, env: jinja2.Environment) -> None:
        rendered = self._render(env, _INTERNET_NS, self._vxlan())
        assert "route-map RM-TRANSIT-IN in" in rendered
        assert "route-map RM-TRANSIT-OUT out" in rendered
        assert "ip prefix-list PL-TRANSIT-IN-DEFAULT seq 10 permit 0.0.0.0/0\n" in rendered
        assert "route-map RM-TRANSIT-IN permit 10\n  match ip address prefix-list PL-TRANSIT-IN-DEFAULT" in rendered
        assert "route-map RM-TRANSIT-OUT deny 10\n" in rendered

    def test_non_bgw_edge_keeps_a_plain_vrf_context(self, env: jinja2.Environment) -> None:
        rendered = self._render(env, _INTERNET_NS, None)
        assert "vrf context INTERNET\n  address-family ipv4 unicast\n" in rendered
        assert "vni 60002" not in rendered
        assert "route-map RM-TRANSIT-IN in" in rendered

    def test_vrf_session_without_transit_renders_no_transit_filters(self, env: jinja2.Environment) -> None:
        """A regular peer in PROD is an external tenant-VRF peer (tests/unit/test_external_vrf_peers.py), not transit."""
        rendered = self._render(env, {"name": "PROD", "namespace_type": "prod"}, None)
        assert "RM-TRANSIT-IN" not in rendered
        assert "RM-TRANSIT-OUT" not in rendered
        assert "PL-TRANSIT" not in rendered
        assert "route-map RM-TRANSIT-LUMEN-FRA1-OUT out" in rendered
        assert "vrf context PROD" in rendered
