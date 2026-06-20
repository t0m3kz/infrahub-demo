"""Unit tests for strict validation mode in RoutingPlanner.

When strict=True, the planner raises ValueError instead of silently
skipping devices with missing prerequisites.
"""

from unittest.mock import MagicMock

import pytest

from generators.helpers.routing import RoutingPlan, RoutingPlanner


def _make_loopback(name: str, device_id: str, role: str, ip: str) -> MagicMock:
    lb = MagicMock()
    lb.id = f"lb-{device_id}"
    lb.device.peer.name.value = name
    lb.device.peer.id = device_id
    lb.device.peer.role.value = role
    lb.ip_address.id = f"ip-{device_id}"
    lb.ip_address.display_label = f"{ip}/32"
    return lb


def _make_p2p_interface(iface_id: str, iface_name: str, device_id: str, cable_id: str) -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name.value = iface_name
    iface.cable.id = cable_id
    iface.device.id = device_id
    return iface


def _device_map_no_router_id(name: str, device_id: str, role: str) -> dict[str, dict]:
    """Device map entry WITHOUT router_id — triggers strict validation."""
    return {name: {"id": device_id, "role": role}}


def _device_map_with_router_id(name: str, device_id: str, role: str, ip: str) -> dict[str, dict]:
    """Device map entry WITH router_id."""
    return {name: {"id": device_id, "role": role, "router_id": {"id": f"ip-{device_id}"}, "loopback_ip": ip}}


class TestStrictUnderlayValidation:
    def test_missing_router_id_raises_in_ebgp_underlay(self) -> None:
        # Device in device_map but without router_id
        device_map = _device_map_no_router_id("leaf-1", "l1", "leaf")
        # Need a cable pair to trigger the underlay loop
        device_map.update(_device_map_with_router_id("spine-1", "s1", "spine", "10.0.0.1"))
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1", "c1"),
        ]
        planner = RoutingPlanner(deployment_id="dc-1", strict=True)
        plan = RoutingPlan()
        with pytest.raises(ValueError, match="No router-id for leaf-1"):
            planner._plan_ebgp_underlay(
                plan,
                device_map=device_map,
                interfaces=interfaces,
                existing_as_by_device={},
                asn_pool="pool-1",
            )

    def test_missing_router_id_warns_in_default_mode(self) -> None:
        # Device without router_id, but not strict → skipped silently
        device_map = _device_map_no_router_id("leaf-1", "l1", "leaf")
        device_map.update(_device_map_with_router_id("spine-1", "s1", "spine", "10.0.0.1"))
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1", strict=False)
        plan = RoutingPlan()
        planner._plan_ebgp_underlay(
            plan,
            device_map=device_map,
            interfaces=interfaces,
            existing_as_by_device={},
            asn_pool="pool-1",
        )

        # leaf-1 has no router_id → no BGP process for it
        leaf_bgp = [b for b in plan.bgp_processes if "leaf-1" in b["name"]]
        assert len(leaf_bgp) == 0

    def test_missing_router_id_raises_in_ibgp_overlay(self) -> None:
        device_map = _device_map_no_router_id("spine-1", "s1", "spine")
        plan = RoutingPlan()

        planner = RoutingPlanner(deployment_id="dc-1", strict=True)
        with pytest.raises(ValueError, match="No router-id for spine-1"):
            planner._plan_overlay_processes(
                plan,
                device_map=device_map,
                overlay_as_id="shared-as-1",
            )

    def test_missing_router_id_raises_in_ospf_underlay(self) -> None:
        device_map = _device_map_no_router_id("spine-1", "s1", "spine")
        plan = RoutingPlan()

        planner = RoutingPlanner(deployment_id="dc-1", strict=True)
        with pytest.raises(ValueError, match="No router-id for spine-1"):
            planner._plan_ospf_underlay(
                plan,
                device_map=device_map,
                interfaces=[],
                deployment_name="dc1",
                existing_area_id="area-0-id",
            )


class TestStrictOverlayValidation:
    def test_missing_loopback_raises(self) -> None:
        # spine has loopback_ip, leaf does NOT
        device_map = {
            "spine-1": {"id": "s1", "role": "spine", "router_id": {"id": "ip-s1"}, "loopback_ip": "10.0.0.1"},
            "leaf-1": {"id": "l1", "role": "leaf", "router_id": {"id": "ip-l1"}},  # no loopback_ip
        }
        bgp_procs = [
            {"name": "spine-1-bgp-overlay", "device_capabilities": [{"id": "s1"}]},
            {"name": "leaf-1-bgp-overlay", "device_capabilities": [{"id": "l1"}]},
        ]
        plan = RoutingPlan()

        planner = RoutingPlanner(deployment_id="dc-1", strict=True)
        with pytest.raises(ValueError, match="No loopback IP for leaf-1"):
            planner._plan_overlay_peerings(
                plan,
                overlay_type="ibgp",
                bgp_processes=bgp_procs,
                device_map=device_map,
            )

    def test_strict_false_skips_silently(self) -> None:
        device_map = {
            "spine-1": {"id": "s1", "role": "spine", "router_id": {"id": "ip-s1"}, "loopback_ip": "10.0.0.1"},
            "leaf-1": {"id": "l1", "role": "leaf", "router_id": {"id": "ip-l1"}},  # no loopback_ip
        }
        bgp_procs = [
            {"name": "spine-1-bgp-overlay", "device_capabilities": [{"id": "s1"}]},
            {"name": "leaf-1-bgp-overlay", "device_capabilities": [{"id": "l1"}]},
        ]
        plan = RoutingPlan()

        planner = RoutingPlanner(deployment_id="dc-1", strict=False)
        planner._plan_overlay_peerings(
            plan,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
        )

        assert plan.bgp_peerings == []
