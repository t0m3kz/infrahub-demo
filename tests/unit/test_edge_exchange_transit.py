"""Unit tests for the exchange transit legs rendered on a colocation edge.

The edge renders the transit legs of the firewall contexts tagged on its
firewall ports the way the border leaf does (trunk port, transit VLAN + L2VNI +
anycast SVI, statics inside the VRF, route-maps defined), except that the
tenant-VRF 0/0 -> VIP static is never exported into EVPN.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge
from transforms.helpers.firewall import get_exchange_routes

_ROOT = Path(__file__).parent.parent.parent
_CONFIGS = _ROOT / "tests" / "smoke" / "configs"
_EDGE_FIXTURE = "edge_cisco_nxos_exchange_transit"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_CONFIGS / name / "input.json").read_text())


def _render(transform_cls: type, data: dict[str, Any]) -> str:
    client = MagicMock()
    client.clone.return_value = client
    client.schema.get = AsyncMock(return_value=MagicMock())
    instance = transform_cls(client=client, infrahub_node=MagicMock(), root_directory=str(_ROOT))
    return asyncio.run(instance.transform(copy.deepcopy(data)))


def _section(config: str, header: str) -> str:
    """The text of one `<header>` stanza (up to the next unindented line)."""
    match = re.search(rf"^{re.escape(header)}\n((?:(?:[ ].*)?\n)*)", config, re.MULTILINE)
    assert match, f"{header!r} not rendered"
    return match.group(1)


def _undefined_route_maps(config: str) -> set[str]:
    referenced = set(re.findall(r"\broute-map (\S+)", config))
    defined = set(re.findall(r"^route-map (\S+) (?:permit|deny)", config, re.MULTILINE))
    return referenced - defined


class TestEdgeExchangeTransit:
    def test_firewall_port_is_a_trunk_of_the_transit_vlans(self) -> None:
        port = _section(_render(Edge, _load(_EDGE_FIXTURE)), "interface Ethernet20")
        assert "switchport mode trunk" in port
        assert "switchport trunk allowed vlan 3000,3001,3400,3401,3600,3601" in port
        assert "switchport mode access" not in port

    def test_transit_vlan_l2vni_and_anycast_svi_per_leg(self) -> None:
        out = _render(Edge, _load(_EDGE_FIXTURE))
        assert "vlan 3000\n  vn-segment 64000" in out
        assert "member vni 64000\n" in out
        svi = _section(out, "interface Vlan3000")
        assert "vrf member PROD" in svi
        assert "ip address 100.66.0.1/29" in svi
        assert "fabric forwarding mode anycast-gateway" in svi

    def test_statics_sit_inside_their_vrf_context(self) -> None:
        out = _render(Edge, _load(_EDGE_FIXTURE))
        assert "ip route 0.0.0.0/0 100.66.0.4" in _section(out, "vrf context PROD")
        assert "ip route 192.0.2.5/32 100.66.32.4" in _section(out, "vrf context INTERNET")

    def test_tenant_default_is_not_redistributed_on_an_edge(self) -> None:
        out = _render(Edge, _load(_EDGE_FIXTURE))
        assert "RM-VRF-STATIC-2-EVPN-PROD" not in out
        assert "0.0.0.0/0" not in "".join(line for line in out.splitlines() if "prefix-list" in line)

    def test_snat_host_route_is_still_redistributed_in_internet(self) -> None:
        out = _render(Edge, _load(_EDGE_FIXTURE))
        assert "redistribute static route-map RM-VRF-STATIC-2-EVPN-INTERNET" in out
        assert "ip prefix-list PL-VRF-STATIC-INTERNET seq 10 permit 192.0.2.5/32" in out
        assert "route-map RM-VRF-STATIC-2-EVPN-INTERNET permit 10" in out

    def test_tenant_default_is_redistributed_on_a_border_leaf(self) -> None:
        data = _load(_EDGE_FIXTURE)
        # same exchange wiring on the border leaf transform: the default is exported
        out = _render(BorderLeaf, data)
        assert "redistribute static route-map RM-VRF-STATIC-2-EVPN-PROD" in out
        assert "ip prefix-list PL-VRF-STATIC-PROD seq 10 permit 0.0.0.0/0" in out

    def test_edge_routes_keep_the_default_static_but_mark_it_local(self) -> None:
        interfaces = [e["node"] for e in _load(_EDGE_FIXTURE)["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]]
        cleaned = [_flatten(i) for i in interfaces]
        exported = get_exchange_routes(cleaned)
        local = get_exchange_routes(cleaned, export_tenant_default=False)
        assert {(r["vrf"], r["prefix"]) for r in exported} == {(r["vrf"], r["prefix"]) for r in local}
        assert [r["prefix"] for r in local if r.get("local_only")] == ["0.0.0.0/0"]
        assert not any(r.get("local_only") for r in exported)

    @pytest.mark.parametrize("name", [_EDGE_FIXTURE, "edge_cisco_nxos_bgw_pair", "edge_cisco_nxos_internet_bgw"])
    def test_every_referenced_route_map_is_defined(self, name: str) -> None:
        assert _undefined_route_maps(_render(Edge, _load(name))) == set()


def _flatten(interface: dict[str, Any]) -> dict[str, Any]:
    """The cleaned interface shape get_exchange_routes consumes (what clean_data produces)."""
    from utils.data_cleaning import clean_data

    return clean_data(interface)


class TestTransitRouteTargetSeparation:
    @staticmethod
    def _imports(site_asn: int) -> set[str]:
        data = _load(_EDGE_FIXTURE)
        deployment = data["DcimDevice"]["edges"][0]["node"]["deployment"]["node"]
        deployment["evpn_rt_as"]["node"]["asn"]["value"] = site_asn
        evpn = _render(Edge, data).split("\nevpn\n")[1]
        return set(re.findall(r"route-target import (\S+)", evpn))

    def test_transit_l2vni_rt_is_the_site_asn_and_vni(self) -> None:
        assert "65000:64000" in self._imports(65000)
        assert "65100:64000" in self._imports(65100)

    def test_two_sites_never_import_each_others_transit_l2vni(self) -> None:
        dc, fr = self._imports(65000), self._imports(65100)
        assert dc and fr
        assert dc.isdisjoint(fr)
