"""Unit tests for EVPN Multi-Site (colocation edge as border gateway).

Covers:
  - _build_peer_groups()                    — DCI-PEERS group, peer-type fabric-external
  - _stretch_rt_anchor()                    — shared L2 RT admin ASN across sites
  - _is_multisite_segment()                 — stretched across >1 deployment
  - _l2_from_activations()                  — multisite / rt_anchor_asn on mappings
  - _select_vtep_interface()                — loopback-vtep preference, lowercase loopback0
  - _get_multisite_config()                 — BGW detection, dci/fabric tracking interfaces
  - get_vxlan_config()                      — route_target and multisite keys
  - _collect_border_gateway_activations()   — stretched segments from the site's deployment
  - cisco_nxos_vxlan.j2 / cisco_nxos_bgp.j2 — NX-OS BGW rendering
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import jinja2
import pytest

from transforms.common import BaseDeviceTransform
from transforms.helpers.bgp import _build_peer_groups, add_template_fields
from transforms.helpers.vxlan import (
    _get_multisite_config,
    _is_multisite_segment,
    _l2_from_activations,
    _select_vtep_interface,
    _stretch_rt_anchor,
    get_vxlan_config,
)

_TEMPLATES_CONFIGS_DIR = Path(__file__).parents[2] / "templates" / "configs"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _segment(
    *,
    stretch_scope: str = "metro",
    deployments: list[tuple[str, int | None]] | None = None,
    seg_id: str = "seg-1",
) -> dict[str, Any]:
    """VXLAN segment active in the given (deployment id, evpn_rt_as) pairs."""
    pairs = [("dc-10", 65010), ("colo-fr", 65100)] if deployments is None else deployments
    return {
        "id": seg_id,
        "name": "colo-services-stretch",
        "stretch_scope": stretch_scope,
        "segment_deployments": [
            {"vni": 10100, "deployment": {"id": dep_id, "evpn_rt_as": {"asn": asn} if asn else None}}
            for dep_id, asn in pairs
        ],
    }


def _dci_session(name: str = "dci-1", address_families: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "session_type": "EBGP",
        "ttl": 1,
        "peering_role": "dci",
        "bfd_enabled": True,
        "send_community": True,
        "send_extended_community": True,
        "address_families": ["ipv6", "evpn"] if address_families is None else address_families,
    }


def _bgw_device(role: str = "edge", *, with_vip: bool = True) -> dict[str, Any]:
    """Device data for a BGW with a VTEP loopback, a DCI link and a fabric uplink."""
    interfaces: list[dict[str, Any]] = [
        {"name": "loopback0", "role": "loopback", "ip_address": {"address": "10.0.0.1/32"}},
        {"name": "loopback1", "role": "loopback-vtep", "ip_address": {"address": "fd00:100::1/128"}},
        {"name": "Ethernet1/1", "role": "uplink"},
        {"name": "Ethernet1/49", "role": "uplink"},
    ]
    if with_vip:
        interfaces.append(
            {"name": "loopback100", "role": "multisite-vip", "ip_address": {"address": "fd00:1ff::1/128"}}
        )
    return {
        "name": "eg-fr01",
        "interfaces": interfaces,
        "capabilities": [
            {
                "typename": "ManagedBGP",
                "peerings": [
                    {
                        "peering_role": "dci",
                        "interface_capabilities": [
                            {"name": "Ethernet1/49", "device": {"name": "eg-fr01"}},
                            {"name": "Ethernet1/49", "device": {"name": "dc10-bl01"}},
                        ],
                    },
                    {
                        "peering_role": "underlay",
                        "interface_capabilities": [{"name": "Ethernet1/1", "device": {"name": "eg-fr01"}}],
                    },
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# BGP DCI peer group
# ---------------------------------------------------------------------------


class TestDciPeerGroup:
    def test_dci_sessions_get_own_group_not_underlay(self) -> None:
        """DCI sessions are ttl 1 but must land in DCI-PEERS, not UNDERLAY-PEERS."""
        underlay = {"name": "u", "session_type": "EBGP", "ttl": 1, "address_families": ["ipv4"]}
        dci = _dci_session()
        groups = {pg["name"]: pg for pg in _build_peer_groups([underlay, dci])}
        assert set(groups) == {"UNDERLAY-PEERS", "DCI-PEERS"}
        assert underlay["peer_group"] == "UNDERLAY-PEERS"
        assert dci["peer_group"] == "DCI-PEERS"
        assert groups["UNDERLAY-PEERS"]["address_families"] == ["ipv4"]

    def test_dci_group_carries_evpn_and_session_is_fabric_external(self) -> None:
        dci = _dci_session()
        (group,) = _build_peer_groups([dci])
        assert group["type"] == "dci"
        assert group["session_type"] == "EBGP"
        assert group["address_families"] == ["ipv6", "evpn"]
        assert group["bfd_enabled"] is True
        assert dci["peer_type"] == "fabric-external"

    def test_dci_group_defaults_to_evpn_without_families(self) -> None:
        (group,) = _build_peer_groups([_dci_session(address_families=[])])
        assert group["address_families"] == ["evpn"]

    def test_non_dci_sessions_have_no_peer_type(self) -> None:
        underlay = {"name": "u", "session_type": "EBGP", "ttl": 1}
        _build_peer_groups([underlay])
        assert "peer_type" not in underlay


# ---------------------------------------------------------------------------
# Stretched segment RT anchor / multisite flag
# ---------------------------------------------------------------------------


class TestStretchedSegment:
    def test_rt_anchor_is_lowest_rt_as(self) -> None:
        assert _stretch_rt_anchor(_segment()) == 65010

    def test_local_segment_has_no_anchor(self) -> None:
        assert _stretch_rt_anchor(_segment(stretch_scope="local")) is None

    def test_missing_scope_is_local(self) -> None:
        seg = _segment()
        del seg["stretch_scope"]
        assert _stretch_rt_anchor(seg) is None
        assert _is_multisite_segment(seg) is False

    def test_no_rt_as_has_no_anchor(self) -> None:
        assert _stretch_rt_anchor(_segment(deployments=[("dc-10", None)])) is None

    def test_multisite_needs_two_deployments(self) -> None:
        assert _is_multisite_segment(_segment()) is True
        assert _is_multisite_segment(_segment(deployments=[("dc-10", 65010)])) is False

    def test_local_segment_is_not_multisite(self) -> None:
        assert _is_multisite_segment(_segment(stretch_scope="local")) is False

    def test_l2_mapping_carries_multisite_and_anchor(self) -> None:
        (mapping,) = _l2_from_activations([{"vlan_id": 100, "vni": 10100, "segment": _segment()}])
        assert mapping["multisite"] is True
        assert mapping["rt_anchor_asn"] == 65010


# ---------------------------------------------------------------------------
# VTEP interface selection
# ---------------------------------------------------------------------------


class TestSelectVtepInterface:
    def test_loopback_vtep_role_wins(self) -> None:
        interfaces = [
            {"name": "loopback0", "role": "loopback"},
            {"name": "loopback2", "role": "loopback-vtep"},
            {"name": "loopback1", "role": "loopback-vtep"},
        ]
        assert (_select_vtep_interface(interfaces) or {})["name"] == "loopback1"

    @pytest.mark.parametrize("name", ["Loopback0", "loopback0"])
    def test_falls_back_to_loopback0_any_case(self, name: str) -> None:
        interfaces = [{"name": "Ethernet1", "role": "uplink"}, {"name": name, "role": "loopback"}]
        assert (_select_vtep_interface(interfaces) or {})["name"] == name

    def test_none_without_loopback(self) -> None:
        assert _select_vtep_interface([{"name": "Ethernet1", "role": "uplink"}]) is None


# ---------------------------------------------------------------------------
# Multi-Site BGW config
# ---------------------------------------------------------------------------


class TestGetMultisiteConfig:
    def test_not_a_bgw_without_vip(self) -> None:
        assert _get_multisite_config(_bgw_device(with_vip=False), "edge", 65100) is None

    def test_missing_site_id_logs_and_skips(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.ERROR):
            assert _get_multisite_config(_bgw_device(), "edge", None) is None
        assert "no EVPN Multi-Site site-id" in caplog.text

    def test_edge_bgw_has_dci_tracking_but_no_fabric_tracking(self) -> None:
        config = _get_multisite_config(_bgw_device(), "edge", 65100, vtep_source="loopback1")
        assert config == {
            "enabled": True,
            "site_id": 65100,
            "vip_interface": "loopback100",
            "vip_address": "fd00:1ff::1",
            "dci_interfaces": ["Ethernet1/49"],
            "fabric_interfaces": [],
            "advertise_interfaces": ["loopback1", "loopback100"],
        }

    def test_border_leaf_bgw_tracks_fabric_uplinks_except_dci(self) -> None:
        config = _get_multisite_config(_bgw_device(), "border-leaf", 65010)
        assert config is not None
        assert config["fabric_interfaces"] == ["Ethernet1/1"]
        assert config["advertise_interfaces"] == ["loopback100"]


class TestGetVxlanConfigMultisite:
    def _config(self, segment: dict[str, Any]) -> dict[str, Any]:
        activations = [{"vlan_id": 100, "vni": 10100, "segment": segment}]
        config = get_vxlan_config(
            _bgw_device(), "cisco_nxos", device_role="edge", activations=activations, fabric_rt_asn=65100
        )
        assert config is not None
        return config

    def test_stretched_segment_uses_anchor_route_target(self) -> None:
        config = self._config(_segment())
        (mapping,) = config["l2_vni_mappings"]
        assert mapping["route_target"] == "65010:10100"
        assert "rt_anchor_asn" not in mapping

    def test_local_segment_uses_fabric_route_target(self) -> None:
        (mapping,) = self._config(_segment(stretch_scope="local"))["l2_vni_mappings"]
        assert mapping["route_target"] == "65100:10100"

    def test_vtep_is_dedicated_loopback_and_multisite_set(self) -> None:
        config = self._config(_segment())
        assert config["vtep"]["source_interface"] == "loopback1"
        assert config["multisite"]["site_id"] == 65100
        assert config["multisite"]["advertise_interfaces"] == ["loopback1", "loopback100"]


# ---------------------------------------------------------------------------
# BGW activations from the site's deployment
# ---------------------------------------------------------------------------


def _deployment_entry(seg_id: str, *, scope: str = "metro", vni: int | None = 10100, domain: str = "dev-1") -> dict:
    return {
        "vni": vni,
        "segment": {
            "id": seg_id,
            "stretch_scope": scope,
            "vlan_domain_segments": [
                {"vlan_id": 200, "vlan_domain": {"id": "other"}},
                {"vlan_id": 100, "vlan_domain": {"id": domain}},
            ],
        },
    }


class TestCollectBorderGatewayActivations:
    def _collect(self, entries: list[dict], seen: set[str | None] | None = None, caps: list | None = None) -> list:
        transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
        return transform._collect_border_gateway_activations(
            {"segment_deployments": entries},
            device_id="dev-1",
            device_capabilities=caps or [],
            seen=set() if seen is None else seen,
        )

    def test_stretched_segment_uses_own_vlan_domain(self) -> None:
        (act,) = self._collect([_deployment_entry("s1")])
        assert act["vlan_id"] == 100
        assert act["vni"] == 10100
        assert act["segment"]["id"] == "s1"

    def test_mlag_domain_id_is_own_domain(self) -> None:
        caps = [{"typename": "ManagedMLAG", "id": "mlag-1"}]
        (act,) = self._collect([_deployment_entry("s1", domain="mlag-1")], caps=caps)
        assert act["vlan_id"] == 100

    def test_skips_local_unconverged_vniless_and_seen(self) -> None:
        entries = [
            _deployment_entry("local", scope="local"),
            _deployment_entry("no-vni", vni=None),
            _deployment_entry("no-domain", domain="someone-else"),
            _deployment_entry("seen"),
            _deployment_entry("dup"),
            _deployment_entry("dup"),
        ]
        acts = self._collect(entries, seen={"seen"})
        assert [a["segment"]["id"] for a in acts] == ["dup"]

    def test_no_deployment_returns_empty(self) -> None:
        transform = BaseDeviceTransform.__new__(BaseDeviceTransform)
        assert (
            transform._collect_border_gateway_activations(None, device_id="d", device_capabilities=[], seen=set()) == []
        )


# ---------------------------------------------------------------------------
# NX-OS rendering
# ---------------------------------------------------------------------------


@pytest.fixture
def nxos_env() -> jinja2.Environment:
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES_CONFIGS_DIR)))


def _vxlan_ctx() -> dict[str, Any]:
    return {
        "enabled": True,
        "features": ["nv overlay", "vn-segment-vlan-based"],
        "nve_interface": "nve1",
        "vtep": {"ipv4": "fd00:100::1", "source_interface": "loopback1"},
        "evpn": {"enabled": True, "rd_format": "auto"},
        "l2_vni_mappings": [
            {"vlan_id": 100, "vni": 10100, "arp_suppression": True, "multisite": True, "route_target": "65010:10100"},
            {"vlan_id": 200, "vni": 10200, "arp_suppression": False, "multisite": False, "route_target": "65100:10200"},
        ],
        "l3_vni_mappings": [],
        "multisite": {
            "enabled": True,
            "site_id": 65100,
            "vip_interface": "loopback100",
            "dci_interfaces": ["Ethernet1/49"],
            "fabric_interfaces": ["Ethernet1/1"],
            "advertise_interfaces": ["loopback1", "loopback100"],
        },
    }


class TestNxosMultisiteRendering:
    def test_bgw_block_rendered(self, nxos_env: jinja2.Environment) -> None:
        rendered = nxos_env.get_template("common/cisco_nxos_vxlan.j2").render(vxlan=_vxlan_ctx())
        assert "nv overlay evpn" in rendered
        assert "evpn multisite border-gateway 65100\n  delay-restore time 300" in rendered
        assert "  source-interface loopback1\n" in rendered
        assert "  multisite border-gateway interface loopback100" in rendered
        assert "interface Ethernet1/49\n  evpn multisite dci-tracking" in rendered
        assert "interface Ethernet1/1\n  evpn multisite fabric-tracking" in rendered
        assert "route-target import 65010:10100" in rendered

    def test_only_stretched_vni_gets_multisite_replication(self, nxos_env: jinja2.Environment) -> None:
        rendered = nxos_env.get_template("common/cisco_nxos_vxlan.j2").render(vxlan=_vxlan_ctx())
        assert rendered.count("multisite ingress-replication") == 1
        stretched = rendered.split("member vni 10100")[1].split("member vni")[0]
        assert "multisite ingress-replication" in stretched

    def test_no_multisite_lines_without_bgw(self, nxos_env: jinja2.Environment) -> None:
        ctx = _vxlan_ctx()
        ctx["multisite"] = None
        rendered = nxos_env.get_template("common/cisco_nxos_vxlan.j2").render(vxlan=ctx)
        assert "multisite" not in rendered

    def test_dci_neighbor_is_fabric_external(self, nxos_env: jinja2.Environment) -> None:
        bgp = [
            {
                "local_as": {"asn": 65100},
                "router_id": {"address": "10.0.0.1/32"},
                "peer_groups": [],
                "sessions": [
                    {
                        "remote_ip": {"address": "fd00:200::1/127"},
                        "remote_as": {"asn": 65010},
                        "remote_device": "dc10-bl01",
                        "peer_type": "fabric-external",
                        "ttl": 1,
                        "address_families": ["ipv6", "evpn"],
                    }
                ],
            }
        ]
        add_template_fields(bgp[0])
        rendered = nxos_env.get_template("common/cisco_nxos_bgp.j2").render(bgp=bgp, loopback_name="loopback0")
        assert "description dc10-bl01\n    peer-type fabric-external" in rendered
