"""Unit tests for the exchange legs and ASA leg security levels of transforms/firewall.py."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.firewall_transform_helpers import _make_context_smoke_data, _make_fw, _make_smoke_data, _render_template

# ===========================================================================
# Exchange transit legs (one VLAN sub-interface per VRF of a context)
# ===========================================================================

_LEG_NAMESPACES = {
    "prod": ("PROD", "100.66.0.5/29", 3000),
    "non_prod": ("NON-PROD", "100.66.16.5/29", 3200),
    "internet": ("INTERNET", "100.66.32.5/29", 3400),
}


def _leg_interface(ns_type: str, *, context_id: str = "ctx-shared") -> dict[str, Any]:
    ns_name, address, vlan = _LEG_NAMESPACES[ns_type]
    return {
        "__typename": {"value": "DcimVirtualInterface"},
        "name": {"value": f"eth1.{vlan}"},
        "description": {"value": None},
        "status": {"value": "active"},
        "role": {"value": None},
        "parent_interface": {"node": {"name": {"value": "eth1"}}},
        "ip_address": {
            "node": {
                "address": {"value": address},
                "ip_namespace": {"node": {"name": {"value": ns_name}, "namespace_type": {"value": ns_type}}},
            }
        },
        "ha_domain": {"node": None},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedFirewallContext",
                        "id": context_id,
                        "name": {"value": "dc11-fw-shared"},
                        "vlan_id": {"value": 3000},
                        "context_id": {"value": None},
                        "tenant": {"node": None},
                        "served_deployments": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "dep-1",
                                        "network_segments": {
                                            "edges": [
                                                {"node": {"id": "seg-prod"}},
                                                {"node": {"id": "seg-nonprod"}},
                                            ]
                                        },
                                    }
                                }
                            ]
                        },
                    }
                }
            ]
        },
    }


def _make_exchange_data(platform: str, *, ns_types: tuple[str, ...], ha: bool = True) -> dict[str, Any]:
    """A shared context with a leg per namespace type, the firewall one member
    (``dc11-fw-01``) of an HA pair; served segments seg-prod 10.1.0.0/24 and
    seg-nonprod 10.2.0.0/24 sit in PROD-ZONE / NONPROD-ZONE."""
    data = _make_smoke_data(platform)
    device = data["DcimPhysicalDevice"]["edges"][0]["node"]
    device["name"] = {"value": "dc11-fw-01"}
    device["interfaces"]["edges"] = [{"node": _leg_interface(ns)} for ns in ns_types]
    if ha:
        device["capabilities"] = {
            "edges": [
                {
                    "node": {
                        "__typename": "ManagedFirewallHA",
                        "name": {"value": "dc11-fw-ha"},
                        "group_id": {"value": 1},
                        "mode": {"value": "active-passive"},
                        "priority": {"value": 100},
                        "preempt": {"value": False},
                        "ha_timer": {"value": "standard"},
                        "capabilities": {
                            "edges": [
                                {"node": {"name": {"value": "dc11-fw-01"}}},
                                {"node": {"name": {"value": "dc11-fw-02"}}},
                            ]
                        },
                    }
                }
            ]
        }

    def zone(name: str, segment_id: str, prefix: str) -> dict[str, Any]:
        segment = {
            "id": segment_id,
            "gateway": {"node": {"ip_prefix": {"node": {"prefix": {"value": prefix}}}}},
        }
        return {
            "node": {
                "name": {"value": name},
                "trust_level": {"value": 50},
                "description": {"value": ""},
                "network_segments": {"edges": [{"node": segment}]},
            }
        }

    data["SecurityZone"] = {
        "edges": [zone("PROD-ZONE", "seg-prod", "10.1.0.0/24"), zone("NONPROD-ZONE", "seg-nonprod", "10.2.0.0/24")]
    }
    return data


# vendor -> markers of a PROD + INTERNET context: leg (transit VLAN, VIP), the
# tenant prefix via the PROD anycast .1, 0/0 via the INTERNET anycast .1, zones.
_LEG_MARKERS = {
    "paloalto_panos": [
        "set network interface ethernet eth1 layer3 units eth1.3000 tag 3000",
        "set network interface ethernet eth1 layer3 units eth1.3000 ip 100.66.0.4/29",
        "set network interface ethernet eth1 layer3 units eth1.3400 ip 100.66.32.4/29",
        "set vsys dc11-fw-shared zone PROD-ZONE network layer3 eth1.3000",
        "set vsys dc11-fw-shared zone INTERNET-ZONE network layer3 eth1.3400",
        "set vsys dc11-fw-shared import network virtual-router vr-dc11-fw-shared",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-1 destination 10.1.0.0/24",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-1 nexthop ip-address 100.66.0.1",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-2 destination 0.0.0.0/0",
        "set network virtual-router vr-dc11-fw-shared routing-table ip static-route rt-2 nexthop ip-address 100.66.32.1",
    ],
    "checkpoint_gaia": [
        "set interface eth1.3000 vlan 3000",
        "set interface eth1.3000 ipv4-address 100.66.0.5 mask-length 29",
        "set interface eth1.3000 security-zone PROD-ZONE",
        "set interface eth1.3400 security-zone INTERNET-ZONE",
        "set virtual-system dc11-fw-shared interface eth1.3000",
        "set static-route 10.1.0.0/24 nexthop gateway address 100.66.0.1 on",
        "set static-route default nexthop gateway address 100.66.32.1 on",
        'interfaces.add.1.name "eth1.3000" interfaces.add.1.interface-type "cluster" interfaces.add.1.ip-address "100.66.0.4"',
    ],
    "fortinet_fortios": [
        'edit "eth1.3000"',
        "set vlanid 3000",
        'set vdom "dc11-fw-shared"',
        "set ip 100.66.0.4/29",
        'edit "PROD-ZONE"',
        'set interface "eth1.3000"',
        "set dst 10.1.0.0/24",
        "set gateway 100.66.0.1",
        "set dst 0.0.0.0/0",
        "set gateway 100.66.32.1",
        'set device "eth1.3400"',
    ],
    "cisco_asa": [
        "allocate-interface eth1.3000",
        "interface eth1.3000",
        " nameif prod",
        " security-level 100",
        " ip address 100.66.0.4 255.255.255.248 standby 100.66.0.6",
        " nameif internet",
        " security-level 0",
        "route prod 10.1.0.0 255.255.255.0 100.66.0.1 1",
        "route internet 0.0.0.0 0.0.0.0 100.66.32.1 1",
        "access-group prod-in in interface prod",
    ],
    "juniper_junos": [
        "set tenants dc11-fw-shared interfaces eth1 unit 3000 vlan-id 3000",
        "set tenants dc11-fw-shared interfaces eth1 unit 3000 family inet address 100.66.0.4/29",
        "set tenants dc11-fw-shared security zones security-zone PROD-ZONE interfaces eth1.3000",
        "set tenants dc11-fw-shared routing-instances ri-dc11-fw-shared interface eth1.3400",
        "set tenants dc11-fw-shared routing-instances ri-dc11-fw-shared routing-options static route 10.1.0.0/24 next-hop 100.66.0.1",
        "set tenants dc11-fw-shared routing-instances ri-dc11-fw-shared routing-options static route 0.0.0.0/0 next-hop 100.66.32.1",
    ],
}


_ISOLATION_MARKERS = {
    "paloalto_panos": [
        "set vsys dc11-fw-shared rulebase security rules deny-PROD-ZONE-to-NONPROD-ZONE from PROD-ZONE",
        "set vsys dc11-fw-shared rulebase security rules deny-NONPROD-ZONE-to-PROD-ZONE action deny",
        "set vsys dc11-fw-shared rulebase security rules deny-PROD-ZONE-to-NONPROD-ZONE log-end yes",
    ],
    "checkpoint_gaia": [
        'add access-rule layer "Network" position top name "deny-PROD-ZONE-to-NONPROD-ZONE" action drop source PROD-ZONE destination NONPROD-ZONE service Any track Log',
        'name "deny-NONPROD-ZONE-to-PROD-ZONE" action drop source NONPROD-ZONE destination PROD-ZONE',
    ],
    "fortinet_fortios": [
        'set name "deny-PROD-ZONE-to-NONPROD-ZONE"',
        'set srcintf "NONPROD-ZONE"',
        "set action deny",
        "set logtraffic all",
    ],
    "cisco_asa": [
        "access-list prod-in extended deny ip any 10.2.0.0 255.255.255.0 log",
        "access-list nonprod-in extended deny ip any 10.1.0.0 255.255.255.0 log",
    ],
    "juniper_junos": [
        "policy deny-PROD-ZONE-to-NONPROD-ZONE then deny",
        "policy deny-NONPROD-ZONE-to-PROD-ZONE then log session-init",
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
        assert "deny-PROD-ZONE-to-NONPROD-ZONE" not in out
        assert "deny ip any" not in out.replace("deny ip any any log", "")

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
    async def test_without_ha_the_leg_keeps_the_members_own_address(self) -> None:
        out = await _make_fw().transform(
            _make_exchange_data("fortinet_fortios", ns_types=("prod", "internet"), ha=False)
        )
        assert "set ip 100.66.0.5/29" in out
        assert "100.66.0.4" not in out

    @pytest.mark.asyncio
    async def test_asa_without_ha_has_no_standby_address(self) -> None:
        out = await _make_fw().transform(_make_exchange_data("cisco_asa", ns_types=("prod", "internet"), ha=False))
        assert " ip address 100.66.0.5 255.255.255.248\n" in out
        assert "standby 100.66" not in out

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


class TestAsaLegSecurityLevels:
    @pytest.mark.parametrize("ns_type,level", [("prod", 100), ("non_prod", 50), ("internet", 0), ("management", 60)])
    def test_every_exchange_namespace_type_has_a_security_level(self, ns_type: str, level: int) -> None:
        leg = {
            "ns_type": ns_type,
            "nameif": "x",
            "vlan": 3000,
            "parent_interface": {"name": "Port-channel1"},
            "own_ip": "100.66.0.5/29",
            "virtual_ip": None,
            "standby_ip": None,
        }
        context = {"name": "c1", "legs": [leg], "routes": [], "isolation": [], "policies": []}
        out = _render_template("cisco_asa", name="fw1", contexts=[context])
        assert f"security-level {level}" in out
        # the internet leg takes no ACL (stateful return); management takes only denies
        assert "access-list x-in" not in out or ns_type in ("prod", "non_prod", "management")

    def test_management_leg_acl_is_isolation_denies_then_implicit_deny_without_the_context_rules(self) -> None:
        legs = [
            {
                "ns_type": ns_type,
                "zone": zone,
                "nameif": nameif,
                "vlan": 3000 + i,
                "parent_interface": {"name": "Port-channel1"},
                "own_ip": f"100.66.{i}.5/29",
                "virtual_ip": None,
                "standby_ip": None,
                "interface": f"Port-channel1.{3000 + i}",
                "prefixes": prefixes,
            }
            for i, (ns_type, zone, nameif, prefixes) in enumerate(
                [
                    ("prod", "PROD-ZONE", "prod", ["10.1.0.0/24"]),
                    ("management", "MANAGEMENT-ZONE", "management", ["10.250.0.0/24"]),
                ]
            )
        ]
        rule = {"src_zone": "PROD-ZONE", "action": "permit", "protocol": "udp", "src": None, "dst": "10.250.0.0/24"}
        context = {
            "name": "c1",
            "legs": legs,
            "routes": [],
            "isolation": [{"src_zone": "MANAGEMENT-ZONE", "dst_zone": "PROD-ZONE"}],
            "policies": [{"rules": [{**rule, "dst_port": "eq 53", "log": False}]}],
        }
        out = _render_template("cisco_asa", name="fw1", contexts=[context])
        mgmt_acl = [line for line in out.splitlines() if line.startswith("access-list management-in")]
        assert mgmt_acl == [
            "access-list management-in extended deny ip any 10.1.0.0 255.255.255.0 log",
            "access-list management-in extended deny ip any any log",
        ]
        assert "access-group management-in in interface management" in out
        assert "access-list prod-in extended permit udp any 10.250.0.0 255.255.255.0 eq 53" in out
