"""Unit tests for the border-leaf transform and _flatten_deployment_segment_activations()
(transforms/helpers/segments.py).

Covers:
  - _flatten_deployment_segment_activations() — deployment.segment_deployments
    -> activations shape (keyed on `vni`, not `vlan_id`: local VLAN ID is per
    VLAN domain, not DC-wide — see ManagedVlanDomainSegment).
  - Exchange transits on a border leaf: a firewall context tagged on a service
    port brings its legs' VLAN/VNI/VRF/anycast gateway, the trunk allow-list,
    the VRF statics and their EVPN redistribution; SONiC refuses; no PBR.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from transforms.config.border_leaf import BorderLeaf
from transforms.helpers.segments import _flatten_deployment_segment_activations

_ROOT = Path(__file__).parent.parent.parent
_CONFIGS = _ROOT / "tests" / "smoke" / "configs"


# ===========================================================================
# _flatten_deployment_segment_activations()
# ===========================================================================


class TestFlattenDeploymentSegmentActivations:
    def test_none_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_segment_activations(None) == []

    def test_empty_deployment_returns_empty(self) -> None:
        assert _flatten_deployment_segment_activations({}) == []

    def test_no_segment_deployments_key_returns_empty(self) -> None:
        assert _flatten_deployment_segment_activations({"name": "DC10"}) == []

    def test_single_activation_extracted(self) -> None:
        deployment = {
            "segment_deployments": [
                {"vni": 10100, "segment": {"id": "seg-1", "customer_name": "web"}},
            ]
        }
        result = _flatten_deployment_segment_activations(deployment)
        assert result == [{"vni": 10100, "segment": {"id": "seg-1", "customer_name": "web"}}]

    def test_multiple_activations_all_extracted(self) -> None:
        deployment = {
            "segment_deployments": [
                {"vni": 10100, "segment": {"id": "seg-1"}},
                {"vni": 10200, "segment": {"id": "seg-2"}},
            ]
        }
        result = _flatten_deployment_segment_activations(deployment)
        assert [a["vni"] for a in result] == [10100, 10200]

    def test_entry_missing_vni_is_skipped(self) -> None:
        deployment = {"segment_deployments": [{"vni": None, "segment": {"id": "seg-1"}}]}
        assert _flatten_deployment_segment_activations(deployment) == []

    def test_entry_missing_segment_is_skipped(self) -> None:
        deployment = {"segment_deployments": [{"vni": 10100, "segment": None}]}
        assert _flatten_deployment_segment_activations(deployment) == []


# ===========================================================================
# Exchange transits on the border leaf
# ===========================================================================


def _v(value: Any) -> dict:
    return {"value": value}


def _node(inner: dict | None) -> dict:
    return {"node": inner}


def _edges(nodes: list[dict]) -> dict:
    return {"edges": [{"node": n} for n in nodes]}


def _ns(name: str, ns_type: str, l3_vni: int) -> dict:
    return _node({"name": _v(name), "namespace_type": _v(ns_type), "l3_vni": _v(l3_vni)})


def _leg(address: str, ns: dict, exchange: dict) -> dict:
    return {
        "name": _v("eth1.3001"),
        "device": _node({"name": _v("dc-fw-1"), "role": _v("firewall")}),
        "ip_address": _node({"address": _v(address), "ip_namespace": ns}),
        "interface_capabilities": _edges([exchange]),
    }


def _transit_device_data(platform: str) -> dict:
    """The golden border leaf of `platform` plus a firewall service port tagged with a
    shared context (PROD <-> INTERNET exchange) and one served segment in the DC."""
    data = json.loads((_CONFIGS / f"border_leaf_{platform}_ebgp_ibgp" / "input.json").read_text())
    node = data["DcimDevice"]["edges"][0]["node"]
    exchange = {
        "id": "x1",
        "namespace_a": _node({"name": _v("PROD"), "namespace_type": _v("prod")}),
        "namespace_z": _node({"name": _v("INTERNET"), "namespace_type": _v("internet")}),
        "gateway": _node({"id": "ctx-1", "tenant": _node(None)}),
    }
    legs = [
        _leg("100.66.0.5/29", _ns("PROD", "prod", 50001), exchange),
        _leg("100.66.32.5/29", _ns("INTERNET", "internet", 50003), exchange),
    ]
    context = {
        "__typename": "ManagedFirewallContext",
        "id": "ctx-1",
        "name": _v("dc11-fw-shared"),
        "vlan_id": _v(3001),
        "tenant": _node(None),
        "served_deployments": _edges([{"id": "dep-a"}]),
        "interface_capabilities": _edges(legs),
    }
    port = {
        "__typename": "DcimPhysicalInterface",
        "name": _v("Ethernet20"),
        "description": _v("to dc-fw-1"),
        "status": _v("active"),
        "role": _v("firewall"),
        "interface_type": _v("10gbase-x-sfpp"),
        "mtu": _v(9000),
        "ip_address": _node(None),
        "interface_capabilities": _edges([context]),
    }
    node["interfaces"]["edges"].append({"node": port})
    segment = {
        "id": "seg-a",
        "__typename": "ManagedVxlanSegment",
        "name": _v("seg-a"),
        "customer_name": _v("Customer-A"),
        "customer_deployments": _edges([{"id": "dep-a"}]),
        "gateway": _node(
            {
                "address": _v("10.1.0.1/24"),
                "ip_prefix": _node({"prefix": _v("10.1.0.0/24"), "ip_namespace": _ns("PROD", "prod", 50001)}),
            }
        ),
    }
    node["deployment"]["node"]["segment_deployments"] = _edges([{"vni": _v(10100), "segment": _node(segment)}])
    return data


def _render(platform: str, data: dict) -> str:
    client = MagicMock()
    client.clone.return_value = client
    client.schema.get = AsyncMock(return_value=MagicMock())
    instance = BorderLeaf(client=client, infrahub_node=MagicMock(), root_directory=str(_ROOT))
    return asyncio.run(instance.transform(copy.deepcopy(data)))


class TestExchangeTransitRendering:
    def test_nxos_statics_sit_in_the_vrf_and_redistribute_with_exact_match(self) -> None:
        out = _render("cisco_nxos", _transit_device_data("cisco_nxos"))
        prod = out.split("vrf context PROD")[1].split("vrf context")[0]
        assert "ip route 0.0.0.0/0 100.66.0.4" in prod
        assert "ip route 0.0.0.0/0 100.66.0.4 vrf" not in out
        internet = out.split("vrf context INTERNET")[1]
        assert "ip route 10.1.0.0/24 100.66.32.4" in internet
        assert "ip route 0.0.0.0/0 100.66.32" not in out
        assert out.count("redistribute static route-map RM-VRF-STATIC-2-EVPN") == 2
        assert "ip prefix-list PL-VRF-STATIC seq 20 permit 0.0.0.0/0" in out
        assert "match ip address prefix-list PL-VRF-STATIC" in out

    def test_nxos_transit_vlans_vnis_and_anycast_gateways(self) -> None:
        out = _render("cisco_nxos", _transit_device_data("cisco_nxos"))
        assert "vlan 3001" in out and "vlan 3401" in out
        assert "vn-segment 64001" in out and "vn-segment 64401" in out
        assert "ip address 100.66.0.1/29" in out
        assert "ip address 100.66.32.1/29" in out
        assert "fabric forwarding mode anycast-gateway" in out

    def test_trunk_to_the_firewall_allows_the_transit_vlans_without_a_dot1q_tag(self) -> None:
        out = _render("cisco_nxos", _transit_device_data("cisco_nxos"))
        port = out.split("interface Ethernet20")[1].split("!")[0]
        assert "switchport trunk allowed vlan 3001,3401" in port
        assert "encapsulation dot1q" not in port

    def test_eos_statics_and_redistribution(self) -> None:
        out = _render("arista_eos", _transit_device_data("arista_eos"))
        assert "ip route vrf PROD 0.0.0.0/0 100.66.0.4" in out
        assert "ip route vrf INTERNET 10.1.0.0/24 100.66.32.4" in out
        assert out.count("redistribute static route-map RM-VRF-STATIC-2-EVPN") == 2
        assert "ip prefix-list PL-VRF-STATIC seq 20 permit 0.0.0.0/0" in out
        assert "vxlan vlan 3001 vni 64001" in out

    def test_sros_statics_are_keyed_by_l3_vni_and_transit_irb_uses_explicit_vnis(self) -> None:
        out = _render("nokia_sros", _transit_device_data("nokia_sros"))
        assert (
            'configure service vprn 50001 static-routes route 0.0.0.0/0 route-type unicast next-hop "100.66.0.4"' in out
        )
        assert "configure service vprn 50003 static-routes route 10.1.0.0/24" in out
        assert 'configure service vprn 50001 interface "int-vpls-64001"' in out
        assert 'configure service vprn 50003 interface "int-vpls-64401"' in out
        assert 'export-policy ["RM-VRF-STATIC-2-EVPN"]' in out

    @pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
    def test_sonic_refuses_transit_legs(self, platform: str) -> None:
        data = _transit_device_data("cisco_nxos")
        data["DcimDevice"]["edges"][0]["node"]["platform"]["node"]["netmiko_device_type"] = _v(platform)
        with pytest.raises(ValueError, match="cannot render the inter-VRF exchange transit legs"):
            _render(platform, data)

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "nokia_sros"])
    def test_no_pbr_is_rendered_on_the_border_leaf(self, platform: str) -> None:
        out = _render(platform, _transit_device_data(platform))
        assert "BORDER-LEAF-PBR" not in out
        assert "feature pbr" not in out

    @pytest.mark.parametrize("platform", ["cisco_nxos", "arista_eos", "nokia_sros"])
    def test_without_a_context_leg_nothing_is_added(self, platform: str) -> None:
        data = json.loads((_CONFIGS / f"border_leaf_{platform}_ebgp_ibgp" / "input.json").read_text())
        out = _render(platform, data)
        assert "RM-VRF-STATIC-2-EVPN" not in out and "XCHG" not in out
