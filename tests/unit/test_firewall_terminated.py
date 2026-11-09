"""Unit tests for the segments a firewall context terminates (transforms/helpers/firewall_terminated.py).

The sub-interface of a terminated segment is derived, not stored: ``<uplink trunk>.<vlan>`` on the trunk the
context's legs ride on, the VLAN from the segment's row on the border leaf at the far end of the trunk's cable,
addressed with the segment's gateway as the pair's ``virtual_ip`` (VIP only, no per-member address) and in the
segment's zone. A segment with no such row yet is left out with a warning, never rendered with a guessed tag.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from transforms.config.firewall import apply_context_routes
from transforms.helpers.firewall import get_firewall_static_routes
from transforms.helpers.firewall_contexts import get_firewall_contexts
from transforms.helpers.firewall_terminated import terminated_interfaces

DEVICE_ID = "fw-1"
LEAF_ID = "bl-1"
HA: dict[str, Any] = {"name": "ha-1", "members": ["fw-1", "fw-2"]}


def _segment(
    name: str = "seg-a",
    *,
    gateway: str | None = "10.9.0.1/24",
    rows: list[tuple[str, int]] | None = None,
    zone: str | None = "PROD",
) -> dict[str, Any]:
    """A terminated segment as FirewallContextConfigFields returns it, its rows being ``(device id, vlan id)``."""
    return {
        "id": f"id-{name}",
        "name": name,
        "gateway": {"address": gateway} if gateway else None,
        "security_zone": {"name": zone} if zone else None,
        "segment_deployments": [
            {"vlans": [{"vlan_id": vlan, "device": {"id": device}} for device, vlan in rows or []]}
        ],
    }


def _context(*segments: dict[str, Any], trunk: str | None = "eth1") -> dict[str, Any]:
    """A context in the shape get_firewall_contexts returns, with one leg on ``trunk`` (None: no leg)."""
    legs = [{"interface": f"{trunk}.3000", "parent_interface": {"name": trunk}}] if trunk else []
    return {
        "id": "ctx-1",
        "name": "shared-fw-prod",
        "legs": legs,
        "segments": list(segments),
        "terminated_segments": list(segments),
    }


def _trunk(name: str = "eth1", *, far: list[str | None] | None = None) -> dict[str, Any]:
    """The firewall's own uplink trunk; ``far`` are the device ids of the cable's endpoints (both ends)."""
    endpoints = [{"device": {"id": device} if device else None} for device in (far or [DEVICE_ID, LEAF_ID])]
    return {"name": name, "role": "uplink", "cable": {"endpoints": endpoints}}


class TestTerminatedInterfaces:
    def test_the_interface_is_derived_from_the_facing_leafs_row(self) -> None:
        """Trunk from the legs, VLAN from the row of the device at the far end, VIP from the gateway, zone from the
        segment: the shape the vendor templates render for any firewall interface."""
        segment = _segment(rows=[(LEAF_ID, 101), ("other-leaf", 202)])

        (iface,) = terminated_interfaces([_context(segment)], [_trunk()], HA, DEVICE_ID, "fw-1")

        assert iface == {
            "name": "eth1.101",
            "description": "seg-a",
            "status": None,
            "vlan_id": 101,
            "parent_interface": {"name": "eth1"},
            "ip_address": {"address": "10.9.0.1/24"},
            "security_zone": {"name": "PROD"},
            "virtual_ip": "10.9.0.1/24",
        }

    def test_the_row_of_another_device_is_never_taken(self) -> None:
        """A row on a leaf that is not at the far end of the trunk is another switch's VLAN, not the cable's tag."""
        interfaces = terminated_interfaces(
            [_context(_segment(rows=[("other-leaf", 202)]))], [_trunk()], HA, DEVICE_ID, "fw-1"
        )

        assert interfaces == []

    def test_a_segment_with_no_row_yet_is_left_out_with_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """The segment reconcile has not converged: nothing is rendered, and the log says why."""
        with caplog.at_level(logging.WARNING):
            interfaces = terminated_interfaces([_context(_segment(rows=[]))], [_trunk()], HA, DEVICE_ID, "fw-1")

        assert interfaces == []
        assert "no VLAN row on the facing leaf yet" in caplog.text

    def test_a_segment_with_no_gateway_is_left_out_with_a_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """No VIP to own: not rendered."""
        with caplog.at_level(logging.WARNING):
            interfaces = terminated_interfaces(
                [_context(_segment(gateway=None, rows=[(LEAF_ID, 101)]))], [_trunk()], HA, DEVICE_ID, "fw-1"
            )

        assert interfaces == []
        assert "no gateway" in caplog.text

    @pytest.mark.parametrize(
        "interfaces",
        [
            [],
            [_trunk(far=[DEVICE_ID])],
            [_trunk(far=[DEVICE_ID, None])],
            [{"name": "eth1", "role": "uplink", "cable": None}],
        ],
        ids=["no trunk interface", "one end only", "far end not a port", "not cabled"],
    )
    def test_a_trunk_with_no_facing_device_renders_nothing(
        self, interfaces: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hosted firewall's NIC (no cable of its own) or a trunk ending on a patch panel has no facing leaf."""
        with caplog.at_level(logging.WARNING):
            found = terminated_interfaces(
                [_context(_segment(rows=[(LEAF_ID, 101)]))], interfaces, HA, DEVICE_ID, "fw-1"
            )

        assert found == []
        assert "no uplink trunk cabled to a facing leaf" in caplog.text

    def test_a_context_with_no_leg_on_this_device_has_no_trunk(self) -> None:
        """A context that renders no legs here (a colocation one) cannot name the trunk."""
        context = _context(_segment(rows=[(LEAF_ID, 101)]), trunk=None)

        assert terminated_interfaces([context], [_trunk()], HA, DEVICE_ID, "fw-1") == []

    def test_a_context_that_terminates_nothing_adds_nothing(self) -> None:
        """The common case: no terminated segment, no interface, no warning."""
        assert terminated_interfaces([_context()], [_trunk()], HA, DEVICE_ID, "fw-1") == []

    def test_without_an_ha_pair_there_is_no_virtual_ip(self) -> None:
        """The address is still the gateway, but no VIP is claimed for a device without a pair."""
        (iface,) = terminated_interfaces([_context(_segment(rows=[(LEAF_ID, 101)]))], [_trunk()], None, DEVICE_ID, "x")

        assert iface["virtual_ip"] is None
        assert iface["ip_address"] == {"address": "10.9.0.1/24"}

    def test_interfaces_are_sorted_by_trunk_and_vlan(self) -> None:
        """Stable output whatever order the query returned the segments in."""
        segments = [_segment("b", rows=[(LEAF_ID, 120)]), _segment("a", rows=[(LEAF_ID, 105)])]

        found = terminated_interfaces([_context(*segments)], [_trunk()], HA, DEVICE_ID, "fw-1")

        assert [iface["name"] for iface in found] == ["eth1.105", "eth1.120"]


class TestContextShape:
    def test_the_context_carries_its_terminated_segments(self) -> None:
        """get_firewall_contexts keeps the terminated segments of the context beside the served ones."""
        segment = _segment(rows=[(LEAF_ID, 101)])
        capability = {
            "typename": "ManagedFirewallContext",
            "id": "ctx-1",
            "name": "shared-fw-prod",
            "vlan_id": 3000,
            "terminated_segments": [segment],
            "served_deployments": [{"id": "dep", "network_segments": [{"id": segment["id"]}]}],
        }
        leg = {
            "vrf": {
                "id": "vrf-prod",
                "security_zone": {"id": "zone-prod", "name": "PROD", "vrf_type": "prod", "l3_vni": 60000},
            },
            "name": "eth1.3000",
            "parent_interface": {"name": "eth1"},
            "ip_address": {"address": "10.0.0.5/29", "ip_namespace": {"node": {"name": {"value": "default"}}}},
            "capabilities": [capability],
        }

        (context,) = get_firewall_contexts([leg], HA)

        assert context["terminated_segments"] == [segment]
        assert context["legs"][0]["parent_interface"] == {"name": "eth1"}

    def test_a_context_without_the_field_terminates_nothing(self) -> None:
        """A cluster context (colocation) read by a query that has no terminated_segments."""
        (context,) = get_firewall_contexts([], HA, [{"id": "ctx-1", "name": "c"}])

        assert context["terminated_segments"] == []


class TestRoutes:
    """A terminated segment is connected to the pair: it is nobody's static route."""

    @staticmethod
    def _leg() -> dict[str, Any]:
        return {
            "interface": "eth1.3000",
            "vlan": 3000,
            "ns_type": "prod",
            "zone": "PROD",
            "nameif": "prod",
            "vrf": "PROD",
            "anycast": "10.0.0.1",
            "vip": "10.0.0.4",
        }

    def test_the_terminated_prefix_is_not_routed_to_the_border_leaf(self) -> None:
        """The served segment gets a route via the tenant leg's anycast; the terminated one does not."""
        routed = {
            "id": "id-routed",
            "gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}},
            "security_zone": {"name": "PROD"},
        }
        terminated = {
            "id": "id-seg-a",
            "gateway": {"ip_prefix": {"prefix": "10.9.0.0/24"}},
            "security_zone": {"name": "PROD"},
        }
        context = {
            "legs": [self._leg()],
            "segments": [routed, terminated],
            "terminated_segments": [{"id": "id-seg-a"}],
            "snat": None,
        }

        apply_context_routes(context, {})

        assert [route["destination"] for route in context["routes"] or []] == ["10.1.0.0/24"]
        assert context["legs"][0]["prefixes"] == ["10.1.0.0/24"]

    def test_an_interface_with_a_virtual_ip_gets_no_static_route(self) -> None:
        """get_firewall_static_routes derives a leaf next hop in a /30; a VIP interface has no leaf behind it."""
        base = {
            "name": "eth1.101",
            "ip_address": {"address": "10.9.0.1/30", "ip_namespace": {"node": {"name": {"value": "default"}}}},
            "vrf": {"id": "vrf-prod", "security_zone": {"id": "zone-prod", "name": "PROD"}},
        }
        zones = [{"name": "PROD", "member_cidrs": ["10.1.0.0/24"]}]

        assert len(get_firewall_static_routes([base], zones)) == 1
        assert get_firewall_static_routes([base | {"virtual_ip": "10.9.0.1/30"}], zones) == []
