"""Unit tests for the exchange legs of transforms/firewall.py."""

from __future__ import annotations

import pytest

from tests.unit.firewall_transform_helpers import (
    _make_context_smoke_data,
    _make_exchange_data,
    _make_fw,
)

# ===========================================================================
# Exchange transit legs (one VLAN sub-interface per VRF of a context)
# ===========================================================================

# vendor -> markers of a PROD + INTERNET context: leg (transit VLAN, VIP), the
# tenant prefix via the PROD anycast .1, 0/0 via the INTERNET anycast .1, zones.
_LEG_MARKERS = {
    "paloalto_panos": [
        "set network interface ethernet eth1 layer3 units eth1.3000 tag 3000",
        "set network interface ethernet eth1 layer3 units eth1.3000 ip 100.66.0.4/29",
        "set network interface ethernet eth1 layer3 units eth1.3400 ip 100.66.32.4/29",
        "set vsys dc11-fw-shared zone PROD network layer3 eth1.3000",
        "set vsys dc11-fw-shared zone INTERNET network layer3 eth1.3400",
        "set vsys dc11-fw-shared import network virtual-router vr-dc11-fw-shared",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-1 destination 10.1.0.0/24",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-1 nexthop ip-address 100.66.0.1",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-2 destination 0.0.0.0/0",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-2 nexthop ip-address 100.66.32.1",
    ],
    "checkpoint_gaia": [
        "set interface eth1.3000 vlan 3000",
        "set interface eth1.3000 ipv4-address 100.66.0.5 mask-length 29",
        "set interface eth1.3000 security-zone PROD",
        "set interface eth1.3400 security-zone INTERNET",
        "set virtual-system dc11-fw-shared interface eth1.3000",
        "set static-route 10.1.0.0/24 nexthop gateway address 100.66.0.1 on",
        "set static-route default nexthop gateway address 100.66.32.1 on",
        'interfaces.add.1.name "eth1.3000" interfaces.add.1.interface-type "cluster" interfaces.add.1.ip-address "100.66.0.4"',
    ],
    "juniper_junos": [
        "set tenants dc11-fw-shared interfaces eth1 unit 3000 vlan-id 3000",
        "set tenants dc11-fw-shared interfaces eth1 unit 3000 family inet address 100.66.0.4/29",
        "set tenants dc11-fw-shared security zones security-zone PROD interfaces eth1.3000",
        "set tenants dc11-fw-shared routing-instances ri-dc11-fw-shared interface eth1.3400",
        "set tenants dc11-fw-shared routing-instances ri-dc11-fw-shared routing-options static route 10.1.0.0/24 next-hop 100.66.0.1",
        "set tenants dc11-fw-shared routing-instances ri-dc11-fw-shared routing-options static route 0.0.0.0/0 next-hop 100.66.32.1",
    ],
}


_ISOLATION_MARKERS = {
    "paloalto_panos": [
        "set vsys dc11-fw-shared rulebase security rules deny-PROD-to-NONPROD from PROD",
        "set vsys dc11-fw-shared rulebase security rules deny-NONPROD-to-PROD action deny",
        "set vsys dc11-fw-shared rulebase security rules deny-PROD-to-NONPROD log-end yes",
    ],
    "checkpoint_gaia": [
        'add access-rule layer "Network" position top name "deny-PROD-to-NONPROD" action drop source PROD destination NONPROD service Any track Log',
        'name "deny-NONPROD-to-PROD" action drop source NONPROD destination PROD',
    ],
    "juniper_junos": [
        "policy deny-PROD-to-NONPROD then deny",
        "policy deny-NONPROD-to-PROD then log session-init",
    ],
}


class TestFirewallExchangeLegs:
    @pytest.mark.parametrize("platform", sorted(_LEG_MARKERS))
    @pytest.mark.asyncio
    async def test_legs_routes_and_zones_render(self, platform: str) -> None:
        out = await _make_fw().transform(_make_exchange_data(platform, ns_types=("prod", "internet")))
        for marker in _LEG_MARKERS[platform]:
            assert marker in out, marker

    @pytest.mark.parametrize("platform", sorted(_LEG_MARKERS))
    @pytest.mark.asyncio
    async def test_single_tenant_context_has_no_isolation_deny(self, platform: str) -> None:
        out = await _make_fw().transform(_make_exchange_data(platform, ns_types=("prod", "internet")))
        assert "deny-PROD-to-NONPROD" not in out

    @pytest.mark.parametrize("platform", sorted(_ISOLATION_MARKERS))
    @pytest.mark.asyncio
    async def test_context_holding_prod_and_nonprod_denies_between_them(self, platform: str) -> None:
        out = await _make_fw().transform(_make_exchange_data(platform, ns_types=("prod", "non_prod", "internet")))
        for marker in _ISOLATION_MARKERS[platform]:
            assert marker in out, marker

    @pytest.mark.asyncio
    async def test_prefix_of_another_environment_is_not_routed_through_a_single_tenant_leg(self) -> None:
        out = await _make_fw().transform(_make_exchange_data("paloalto_panos", ns_types=("prod", "internet")))
        assert out.count("destination 10.2.0.0/24") == 0

    @pytest.mark.asyncio
    async def test_each_tenant_prefix_routes_via_its_own_leg(self) -> None:
        out = await _make_fw().transform(
            _make_exchange_data("paloalto_panos", ns_types=("prod", "non_prod", "internet"))
        )
        assert "static-route rt-1 destination 10.1.0.0/24" in out
        assert "static-route rt-1 nexthop ip-address 100.66.0.1" in out
        assert "static-route rt-2 destination 10.2.0.0/24" in out
        assert "static-route rt-2 nexthop ip-address 100.66.16.1" in out
        assert "static-route rt-3 nexthop ip-address 100.66.32.1" in out

    @pytest.mark.parametrize("platform", sorted(_LEG_MARKERS))
    @pytest.mark.asyncio
    async def test_leg_is_not_also_rendered_as_a_root_interface(self, platform: str) -> None:
        out = await _make_fw().transform(_make_exchange_data(platform, ns_types=("prod", "internet")))
        # only the context scope carries the leg's address, never an unscoped duplicate
        assert out.count("100.66.0.5") <= (1 if platform in ("checkpoint_gaia",) else 0)

    @pytest.mark.asyncio
    async def test_cluster_context_without_legs_renders_its_policies(self) -> None:
        """A colocation context sits on the cluster only (no interface on a member):
        its vsys and the rules of the segments it serves still render."""
        data = _make_context_smoke_data("paloalto_panos", serves_segment=True)
        device = data["DcimPhysicalDevice"]["edges"][0]["node"]
        context = device["interfaces"]["edges"].pop()["node"]["capabilities"]["edges"][0]["node"]
        device["capabilities"] = {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedFirewallHA",
                        "name": {"value": "fr-fw-ha"},
                        "contexts": {"edges": [{"node": context}]},
                    }
                }
            ]
        }
        out = await _make_fw().transform(data)
        assert "set vsys c005-dedicated rulebase security rules smoke-permit-rule" in out
        assert "virtual-router vr-" not in out
        assert "import network interface" not in out
