"""Unit tests for the colocation edge's VRF handoff to the SD-WAN gateway.

Covers:
  - _build_session_from_peering()  — VRF session detection from the local address namespace
  - _build_peer_groups()           — VRF sessions stay out of every peer group
  - _multisite_vrf_site_asns()     — site ASNs per VRF over stretched segments
  - get_vxlan_config()             — import_route_targets on L3 VNI mappings
  - get_interfaces()               — dot1q tag from a plain sub-interface's name
  - cisco_nxos_bgp.j2              — VRF neighbours under `vrf X`, not globally
  - cisco_nxos_vxlan_vrf.j2 / arista_eos.j2 — remote-site RT imports
  - edges/cisco_nxos.j2            — anycast SVIs and sub-interface encapsulation
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jinja2
import pytest

from transforms.common import _build_peer_groups
from transforms.helpers.bgp import _build_session_from_peering
from transforms.helpers.vxlan import _multisite_vrf_site_asns, get_interfaces, get_vxlan_config

_TEMPLATES_CONFIGS_DIR = Path(__file__).parents[2] / "templates" / "configs"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ip(address: str, namespace: str = "default") -> dict[str, Any]:
    return {"address": address, "ip_namespace": {"name": namespace}}


def _handoff_peering(*, remote_ip: dict[str, Any] | None = None, namespace: str = "PROD") -> dict[str, Any]:
    """The PROD handoff peering eg-fr01 <-> EQX-FR2-SDWAN-GW1, as the transform sees it."""
    remote: dict[str, Any] = {"name": "eth0.1900", "device": {"name": "EQX-FR2-SDWAN-GW1"}}
    if remote_ip is not None:
        remote["ip_address"] = remote_ip
    return {
        "name": "VRF-PROD-EQXFR2-SDWAN",
        "session_type": "EBGP",
        "peering_role": "regular",
        "ttl": 1,
        "send_community": True,
        "interface_capabilities": [
            {
                "name": "Ethernet1/13.1900",
                "device": {"name": "eg-fr01"},
                "ip_address": _ip("10.255.5.0/31", namespace),
            },
            remote,
        ],
        "bgp_processes": [
            {"local_as": {"asn": 4200000100}, "capabilities": [{"name": "eg-fr01"}]},
            {"local_as": {"asn": 65028}, "capabilities": [{"name": "EQX-FR2-SDWAN-GW1"}]},
        ],
    }


def _parent_port_with_cable() -> list[dict[str, Any]]:
    """Ethernet1/13 cabled towards GW1 in the default namespace — must not win."""
    return [
        {
            "name": "Ethernet1/13",
            "ip_address": _ip("192.0.2.0/31"),
            "cable": {"endpoints": [{"device": {"name": "EQX-FR2-SDWAN-GW1"}, "ip_address": _ip("192.0.2.1/31")}]},
        }
    ]


def _stretched_segment(namespace: str | None = "PROD", scope: str = "global") -> dict[str, Any]:
    segment: dict[str, Any] = {
        "id": "seg-stretch",
        "name": "colo-services-stretch",
        "stretch_scope": scope,
        "segment_deployments": [
            {"vni": 10100, "deployment": {"id": "dc-10", "evpn_rt_as": {"asn": 65010}}},
            {"vni": 10100, "deployment": {"id": "colo-fr", "evpn_rt_as": {"asn": 65100}}},
        ],
    }
    if namespace:
        segment["gateway"] = {
            "address": "10.5.50.1/24",
            "ip_prefix": {"prefix": "10.5.50.0/24", "ip_namespace": {"name": namespace, "l3_vni": 60000}},
        }
    return segment


# ---------------------------------------------------------------------------
# VRF session detection
# ---------------------------------------------------------------------------


class TestVrfSession:
    def test_tenant_namespace_makes_a_vrf_session(self) -> None:
        """A PROD local address yields a VRF session with the inline handoff addresses."""
        session = _build_session_from_peering(
            _handoff_peering(remote_ip=_ip("10.255.5.1/31", "PROD")), "eg-fr01", {"asn": 4200000100}, []
        )
        assert session is not None
        assert session["vrf"] == "PROD"
        assert session["local_ip"]["address"] == "10.255.5.0/31"
        assert session["remote_ip"]["address"] == "10.255.5.1/31"
        assert session["remote_as"] == {"asn": 65028}
        assert session["address_families"] == ["ipv4"]

    def test_vrf_session_ignores_cable_on_parent_port(self) -> None:
        """ttl 1 normally prefers cable IPs; a VRF session must keep its own sub-interface's."""
        session = _build_session_from_peering(
            _handoff_peering(remote_ip=_ip("10.255.5.1/31", "PROD")),
            "eg-fr01",
            {"asn": 4200000100},
            _parent_port_with_cable(),
        )
        assert session is not None
        assert session["remote_ip"]["address"] == "10.255.5.1/31"

    def test_missing_remote_address_skips_with_warning(self) -> None:
        """Without the far side's address there's no neighbour to configure."""
        warnings: list[str] = []
        session = _build_session_from_peering(_handoff_peering(), "eg-fr01", {"asn": 4200000100}, [], warnings=warnings)
        assert session is None
        assert len(warnings) == 1
        assert "VRF-PROD-EQXFR2-SDWAN" in warnings[0]

    def test_default_namespace_is_not_a_vrf_session(self) -> None:
        """A default-namespace address keeps the existing underlay resolution."""
        session = _build_session_from_peering(
            _handoff_peering(namespace="default"), "eg-fr01", {"asn": 4200000100}, _parent_port_with_cable()
        )
        assert session is not None
        assert "vrf" not in session
        assert session["remote_ip"]["address"] == "192.0.2.1/31"

    def test_vrf_sessions_join_no_peer_group(self) -> None:
        """Peer groups are global; a VRF neighbour can't be a member of one."""
        underlay = {"name": "u", "session_type": "EBGP", "ttl": 1, "address_families": ["ipv4"]}
        vrf_session = {"name": "v", "session_type": "EBGP", "ttl": 1, "vrf": "PROD", "address_families": ["ipv4"]}
        groups = _build_peer_groups([underlay, vrf_session])
        assert [g["name"] for g in groups] == ["UNDERLAY-PEERS"]
        assert "peer_group" not in vrf_session


# ---------------------------------------------------------------------------
# L3 VNI remote-site RT imports
# ---------------------------------------------------------------------------


class TestMultisiteVrfImports:
    def test_site_asns_grouped_by_vrf(self) -> None:
        acts = [{"vlan_id": 100, "vni": 10100, "segment": _stretched_segment()}]
        assert _multisite_vrf_site_asns(acts) == {"PROD": {65010, 65100}}

    @pytest.mark.parametrize(
        "segment",
        [
            _stretched_segment(namespace=None),
            _stretched_segment(namespace="default"),
            _stretched_segment(scope="local"),
        ],
        ids=["no-gateway", "default-namespace", "local-scope"],
    )
    def test_segments_without_a_routed_stretch_are_ignored(self, segment: dict[str, Any]) -> None:
        assert _multisite_vrf_site_asns([{"vlan_id": 100, "vni": 10100, "segment": segment}]) == {}

    def test_l3_mapping_imports_the_other_sites_rt(self) -> None:
        """Each site imports every other site's {asn}:{l3_vni}, never its own."""
        device = {
            "name": "eg-fr01",
            "interfaces": [{"name": "loopback1", "role": "loopback-vtep", "ip_address": {"address": "fd00::1/128"}}],
        }
        acts = [{"vlan_id": 100, "vni": 10100, "segment": _stretched_segment()}]
        config = get_vxlan_config(device, "cisco_nxos", device_role="edge", activations=acts, fabric_rt_asn=65100)
        assert config is not None
        (mapping,) = config["l3_vni_mappings"]
        assert mapping["vrf_name"] == "PROD"
        assert mapping["import_route_targets"] == ["65010:60000"]


# ---------------------------------------------------------------------------
# Sub-interface dot1q
# ---------------------------------------------------------------------------


class TestSubinterfaceDot1q:
    def test_tag_comes_from_the_name_suffix(self) -> None:
        (iface,) = get_interfaces(
            [
                {
                    "name": "Ethernet1/13.1900",
                    "typename": "DcimVirtualInterface",
                    "ip_address": _ip("10.255.5.0/31", "PROD"),
                }
            ]
        )
        assert iface["dot1q_vlan"] == 1900

    @pytest.mark.parametrize(
        ("name", "typename"),
        [
            ("loopback1", "DcimVirtualInterface"),
            ("Ethernet1/13", "DcimPhysicalInterface"),
            ("Vlan.abc", "DcimVirtualInterface"),
        ],
    )
    def test_no_tag_without_numeric_suffix(self, name: str, typename: str) -> None:
        (iface,) = get_interfaces([{"name": name, "typename": typename}])
        assert iface["dot1q_vlan"] is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.fixture
def env() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)))


def _bgp_with_vrf_session() -> list[dict[str, Any]]:
    return [
        {
            "local_as": {"asn": 4200000100},
            "router_id": {"address": "10.0.0.1/32"},
            "peer_groups": [],
            "sessions": [
                {
                    "remote_ip": {"address": "fd00:2500::1/127"},
                    "remote_as": {"asn": 65010},
                    "remote_device": "bl-dc101101",
                    "ttl": 1,
                    "address_families": ["ipv6", "evpn"],
                },
                {
                    "vrf": "PROD",
                    "remote_ip": {"address": "10.255.5.1/31"},
                    "remote_as": {"asn": 65028},
                    "remote_device": "EQX-FR2-SDWAN-GW1",
                    "ttl": 1,
                    "send_community": True,
                    "address_families": ["ipv4"],
                },
            ],
        }
    ]


class TestNxosVrfBgpRendering:
    def test_vrf_neighbor_rendered_under_vrf(self, env: jinja2.Environment) -> None:
        rendered = env.get_template("common/cisco_nxos_bgp.j2").render(bgp=_bgp_with_vrf_session(), interfaces=[])
        vrf_block = rendered.split("  vrf PROD")[1]
        assert "    neighbor 10.255.5.1\n      remote-as 65028" in vrf_block
        assert "      address-family ipv4 unicast" in vrf_block

    def test_vrf_neighbor_not_rendered_globally(self, env: jinja2.Environment) -> None:
        rendered = env.get_template("common/cisco_nxos_bgp.j2").render(bgp=_bgp_with_vrf_session(), interfaces=[])
        global_part = rendered.split("  vrf PROD")[0]
        assert "10.255.5.1" not in global_part
        assert "neighbor fd00:2500::1" in global_part

    def test_non_evpn_vrf_has_no_evpn_advertisement(self, env: jinja2.Environment) -> None:
        """Without an L3 VNI mapping the VRF only carries its neighbours."""
        rendered = env.get_template("common/cisco_nxos_bgp.j2").render(bgp=_bgp_with_vrf_session(), interfaces=[])
        assert "advertise l2vpn evpn" not in rendered.split("  vrf PROD")[1]


def _l3_mapping() -> dict[str, Any]:
    return {
        "vrf_name": "PROD",
        "l3_vni": 60000,
        "vlan_id": 3000,
        "route_target": "65100:60000",
        "import_route_targets": ["65010:60000"],
    }


class TestImportRouteTargetRendering:
    def test_nxos_imports_remote_site_rt(self, env: jinja2.Environment) -> None:
        rendered = env.get_template("common/cisco_nxos_vxlan_vrf.j2").render(
            vxlan={"enabled": True, "evpn": {"enabled": True, "rd_format": "auto"}, "l3_vni_mappings": [_l3_mapping()]}
        )
        assert "route-target import 65010:60000\n" in rendered
        assert "route-target import 65010:60000 evpn" in rendered

    def test_nxos_without_imports_renders_no_import(self, env: jinja2.Environment) -> None:
        mapping = _l3_mapping()
        del mapping["import_route_targets"]
        rendered = env.get_template("common/cisco_nxos_vxlan_vrf.j2").render(
            vxlan={"enabled": True, "evpn": {"enabled": True, "rd_format": "auto"}, "l3_vni_mappings": [mapping]}
        )
        assert "route-target import" not in rendered


class TestEdgeTemplate:
    def _render(self, env: jinja2.Environment, **ctx: Any) -> str:
        base: dict[str, Any] = {"name": "eg-fr01", "interfaces": [], "vlans": [], "bgp": [], "ospf": []}
        base.update(ctx)
        return env.get_template("edges/cisco_nxos.j2").render(**base)

    def test_subinterface_gets_encapsulation_and_vrf(self, env: jinja2.Environment) -> None:
        iface = {
            "name": "Ethernet1/13.1900",
            "status": "active",
            "dot1q_vlan": 1900,
            "ip_addresses": [_ip("10.255.5.0/31", "PROD")],
        }
        rendered = self._render(env, interfaces=[iface])
        assert "interface Ethernet1/13.1900\n  encapsulation dot1q 1900\n" in rendered
        assert "  vrf member PROD\n  ip address 10.255.5.0/31" in rendered

    def test_anycast_svi_rendered_for_gateway_vlans(self, env: jinja2.Environment) -> None:
        vlans = [
            {"vlan_id": 100, "name": "colo-services-stretch", "vrf": "PROD", "gateway_ip": "10.5.50.1/24"},
            {"vlan_id": 200, "name": "l2-only"},
        ]
        vxlan = {"enabled": False, "anycast_gateway": {"enabled": True, "mac": "00:00:22:22:33:33"}}
        rendered = self._render(env, vlans=vlans, vxlan=vxlan)
        assert "fabric forwarding anycast-gateway-mac 000022223333" in rendered
        assert (
            "interface Vlan100\n  description colo-services-stretch\n  vrf member PROD\n"
            "  ip address 10.5.50.1/24\n  fabric forwarding mode anycast-gateway"
        ) in rendered
        assert "interface Vlan200" not in rendered

    def test_no_svis_without_anycast_gateway(self, env: jinja2.Environment) -> None:
        vlans = [{"vlan_id": 100, "name": "s", "gateway_ip": "10.5.50.1/24"}]
        rendered = self._render(env, vlans=vlans)
        assert "anycast-gateway" not in rendered
        assert "interface Vlan100" not in rendered
