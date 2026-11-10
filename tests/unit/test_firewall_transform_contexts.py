"""Unit tests for the per-context rendering of transforms/firewall.py.

Covers address-family aware rendering, per-context policies and the placement
of contexts on the firewall.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.encapsulation_helpers import cleaned_encapsulation
from tests.unit.firewall_transform_helpers import _make_context_smoke_data, _make_fw, _render_template
from tests.unit.routing_helpers import in_vrf
from transforms.config.firewall import _build_fw_interfaces
from utils.routing_domain import RoutingDomainError


class TestAddressFamilyAwareRendering:
    _FW_IFACE_V6 = {
        "name": "eth0",
        **cleaned_encapsulation(None, None),
        "ip_address": {"address": "fd00:2300::1/127"},
        "description": None,
        "security_zone": None,
    }
    _FW_IFACE_V4 = {
        "name": "eth1",
        **cleaned_encapsulation(None, None),
        "ip_address": {"address": "10.0.0.1/30"},
        "description": None,
        "security_zone": None,
    }

    @pytest.mark.parametrize(
        "platform,v6_marker,v4_marker",
        [
            ("checkpoint_gaia", "ipv6-address", "ipv4-address"),
            ("paloalto_panos", "ipv6 addr", "ipv4 addr"),
            ("juniper_junos", "family inet6 address", "family inet address"),
        ],
    )
    def test_fw_interface_v6_uses_v6_command(self, platform: str, v6_marker: str, v4_marker: str) -> None:
        out = _render_template(platform, name="fw1", fw_interfaces=[self._FW_IFACE_V6])
        assert v6_marker in out
        assert v4_marker not in out

    @pytest.mark.parametrize(
        "platform,v6_marker,v4_marker",
        [
            ("checkpoint_gaia", "ipv6-address", "ipv4-address"),
            ("paloalto_panos", "ipv6 addr", "ipv4 addr"),
            ("juniper_junos", "family inet6 address", "family inet address"),
        ],
    )
    def test_fw_interface_v4_uses_v4_command(self, platform: str, v6_marker: str, v4_marker: str) -> None:
        out = _render_template(platform, name="fw1", fw_interfaces=[self._FW_IFACE_V4])
        assert v4_marker in out
        assert v6_marker not in out


# ===========================================================================
# Per-context security policies
# ===========================================================================

_CTX_RULE = {
    "seq": 100,
    "name": "c005-web-to-api",
    "action": "permit",
    "protocol": "tcp",
    "raw_protocol": "tcp",
    "port_start": 8443,
    "port_end": None,
    "src_zone": "PROD",
    "dst_zone": "PROD",
    "src": "10.5.1.0/24",
    "dst": "10.5.2.0/24",
    "dst_port": "eq 8443",
    "log": False,
    "description": "",
    "security_profile": None,
}


def _policy_context(**extra: Any) -> dict[str, Any]:
    return {
        "id": "ctx-c005",
        "name": "c005-dedicated",
        "tenant_name": "C005-P-DC12",
        "vlan_id": 3005,
        "context_id": None,
        "legs": [],
        "policies": [{"name": "seg-c005-web-egress", "default_action": "deny", "rules": [_CTX_RULE]}],
        **extra,
    }


class TestPerContextPolicyRendering:
    """Rules placed in a context render inside that vsys/tenant/context, not at root."""

    @pytest.mark.parametrize(
        ("platform", "interface_marker", "route_marker"),
        [
            (
                "paloalto_panos",
                "set vsys c005-dedicated import network interface eth1",
                "set network virtual-router vr-c005-dedicated routing-table ip static-route rt-1 destination 10.9.0.0/24",
            ),
            (
                "checkpoint_gaia",
                "set virtual-system c005-dedicated interface eth1",
                "set virtual-system c005-dedicated\nset static-route 10.9.0.0/24",
            ),
            (
                "juniper_junos",
                "set tenants c005-dedicated routing-instances ri-c005-dedicated interface eth1.0",
                "set tenants c005-dedicated routing-instances ri-c005-dedicated routing-options static route 10.9.0.0/24",
            ),
        ],
    )
    def test_authored_interfaces_and_routes_use_the_context_table(
        self, platform: str, interface_marker: str, route_marker: str
    ) -> None:
        """A context owns authored /30 interfaces and routes without generated /29 transit legs."""
        interface = {
            **TestAddressFamilyAwareRendering._FW_IFACE_V4,
            "context_name": "c005-dedicated",
            "security_zone": {"name": "PROD"},
        }
        context = _policy_context(
            authored_interfaces=["eth1"],
            routes=[{"destination": "10.9.0.0/24", "nexthop": "10.0.0.2", "interface": "eth1", "vrf": "PROD"}],
            nat=[],
            isolation=[],
        )

        output = _render_template(platform, name="fw1", contexts=[context], fw_interfaces=[interface])

        assert interface_marker in output
        assert route_marker in output

    @pytest.mark.parametrize(
        "platform,scope_marker,rule_marker",
        [
            (
                "paloalto_panos",
                "vsys c005-dedicated",
                "set vsys c005-dedicated rulebase security rules c005-web-to-api",
            ),
            ("juniper_junos", "tenant system c005-dedicated", "set tenants c005-dedicated security policies"),
            ("checkpoint_gaia", "set virtual-system c005-dedicated", 'name "c005-web-to-api"'),
        ],
    )
    def test_context_policies_render_in_the_context_scope(
        self, platform: str, scope_marker: str, rule_marker: str
    ) -> None:
        out = _render_template(platform, name="fw1", contexts=[_policy_context()], zone_policies=[])
        assert rule_marker in out.split(scope_marker, 1)[1]

    @pytest.mark.parametrize(
        "platform,root_marker",
        [
            ("paloalto_panos", "set rulebase security rules"),
            ("juniper_junos", "set security policies"),
            ("checkpoint_gaia", "add access-rule"),
        ],
    )
    def test_context_policies_do_not_leak_into_root(self, platform: str, root_marker: str) -> None:
        out = _render_template(platform, name="fw1", contexts=[_policy_context()], zone_policies=[])
        before_context = out.split("c005-dedicated", 1)[0]
        assert root_marker not in before_context
        if platform in ("paloalto_panos", "juniper_junos"):
            assert root_marker not in out

    @pytest.mark.parametrize(
        "platform,declaration",
        [
            ("paloalto_panos", "set vsys c005-dedicated\n"),
            ("checkpoint_gaia", "add virtual-system name c005-dedicated"),
        ],
    )
    def test_context_without_legs_renders_its_declaration_and_policies_only(
        self, platform: str, declaration: str
    ) -> None:
        """A colocation context has no data-plane legs: no interface, route or isolation, still its rules."""
        out = _render_template(platform, name="fw1", contexts=[_policy_context()], zone_policies=[])
        assert declaration in out
        assert "c005-web-to-api" in out
        assert "eth1" not in out
        assert "virtual-router" not in out

    @pytest.mark.parametrize("platform", ["paloalto_panos", "juniper_junos", "checkpoint_gaia"])
    def test_context_without_policies_renders_no_policy_section(self, platform: str) -> None:
        out = _render_template(platform, name="fw1", contexts=[_policy_context(policies=[])])
        assert "c005-web-to-api" not in out


class TestFirewallTransformContextPlacement:
    @pytest.mark.asyncio
    async def test_segment_rule_moves_into_the_context_serving_it(self) -> None:
        """The rule renders under the vsys and no longer in the root rulebase."""
        out = await _make_fw().transform(_make_context_smoke_data("paloalto_panos", serves_segment=True))
        assert "set vsys c005-dedicated rulebase security rules smoke-permit-rule" in out
        assert "set rulebase security rules smoke-permit-rule" not in out

    @pytest.mark.asyncio
    async def test_rule_of_a_carried_segment_no_context_serves_stays_in_root(self) -> None:
        """A context serving other segments does not take the rule."""
        out = await _make_fw().transform(_make_context_smoke_data("paloalto_panos", serves_segment=False))
        assert "set rulebase security rules smoke-permit-rule" in out
        assert "set vsys c005-dedicated rulebase" not in out


class TestParentInterfaceOnlyForSubInterfaces:
    """The encapsulation parent is also a VM NIC's host port: only a parent on the interface's own device is a
    sub-interface, whatever the names say."""

    @staticmethod
    def _interfaces(name: str, parent: str, parent_device: str = "dev-1") -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                **cleaned_encapsulation(
                    int(name.rsplit(".", 1)[1]) if "." in name else None,
                    {"name": parent, "device": {"id": parent_device}},
                ),
                "device": {"id": "dev-1"},
                "ip_address": {"address": "10.0.0.2/30"},
                "capabilities": [],
            }
        ]

    def test_a_nic_with_an_address_and_a_host_port_parent_has_no_parent(self) -> None:
        """A NIC riding on the host port Ethernet5 of another device keeps parent_interface None."""
        (nic,) = _build_fw_interfaces(self._interfaces("eth0", "Ethernet5", "host-1"), [], {})

        assert nic["parent_interface"] is None

    def test_a_vlan_sub_interface_keeps_its_parent(self) -> None:
        """ethernet1/1.210 on ethernet1/1 is the sub-interface the templates tag with its VLAN."""
        (sub,) = _build_fw_interfaces(self._interfaces("ethernet1/1.210", "ethernet1/1"), [], {})

        assert sub["parent_interface"] == {"name": "ethernet1/1", "device": {"id": "dev-1"}}
        assert sub["vlan_id"] == 210

    def test_the_name_plays_no_part(self) -> None:
        """A sub-interface named otherwise keeps its parent and states its own tag; a NIC named like a sub-interface
        of its host port is none."""
        interfaces = self._interfaces("unit-7", "ethernet1/1")
        interfaces[0].update(cleaned_encapsulation(210, {"name": "ethernet1/1", "device": {"id": "dev-1"}}))
        (renamed,) = _build_fw_interfaces(interfaces, [], {})
        (nic,) = _build_fw_interfaces(self._interfaces("Ethernet5.100", "Ethernet5", "host-1"), [], {})

        assert renamed["parent_interface"] == {"name": "ethernet1/1", "device": {"id": "dev-1"}}
        assert renamed["vlan_id"] == 210
        assert nic["parent_interface"] is None

    def test_direct_zone_is_rendered_without_segment_membership(self) -> None:
        """A routed firewall interface keeps its own zone in default IPAM."""
        interfaces = self._interfaces("ethernet1/1.210", "ethernet1/1")
        zone = {"id": "zone-prod", "name": "PROD", "vrf_type": "prod", "l3_vni": 60000}
        interfaces[0].update(in_vrf({"id": "vrf-dc10-prod", "security_zone": zone}))
        interfaces[0]["ip_address"]["ip_namespace"] = {"name": "default"}

        (interface,) = _build_fw_interfaces(interfaces, [], {})

        assert interface["security_zone"] == zone
        assert interface["vlan_id"] == 210

    def test_ipam_namespace_is_not_a_firewall_zone(self) -> None:
        """An underlay address does not gain a zone from an IPAM namespace label."""
        interfaces = self._interfaces("eth0", "Ethernet5")
        interfaces[0]["ip_address"]["ip_namespace"] = {"name": "PROD", "vrf_type": "prod"}

        (interface,) = _build_fw_interfaces(interfaces, [], {})

        assert interface["security_zone"] is None
        assert interface["vlan_id"] is None

    def test_conflicting_interface_and_prefix_zones_are_refused(self) -> None:
        """The interface's own VRF zone cannot hide a contradictory handoff circuit VRF."""
        interfaces = self._interfaces("eth0", "Ethernet5")
        interfaces[0].update(in_vrf({"id": "vrf-prod", "security_zone": {"id": "zone-prod", "name": "PROD"}}))
        interfaces[0]["capabilities"] = [
            {
                "typename": "TopologySessionCircuit",
                "circuit_type": "handoff",
                "vrf": {"id": "vrf-nonprod", "security_zone": {"id": "zone-nonprod", "name": "NONPROD"}},
            }
        ]

        with pytest.raises(RoutingDomainError, match="conflicting security zones"):
            _build_fw_interfaces(interfaces, [], {})

    def test_a_leg_keeps_the_context_it_is_a_leg_of(self) -> None:
        """The context of a leg sub-interface (utils/service_contexts.py ``leg_contexts``) names it in the config."""
        interfaces = self._interfaces("ethernet1/1.3000", "ethernet1/1")
        context = {"id": "ctx-prod", "name": "prod-policy", "typename": "ManagedFirewallContext"}

        (interface,) = _build_fw_interfaces(interfaces, [], {"ethernet1/1.3000": context})

        assert interface["context_name"] == "prod-policy"
