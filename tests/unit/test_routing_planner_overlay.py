"""Unit tests for overlay peering planning.

Tests verify correct overlay BGP peering creation through
RoutingPlanner._plan_overlay_peerings() for:
- iBGP overlay (route_reflector, hierarchical_route_reflector)
- eBGP overlay (spine_leaf)
- Scoped filtering (bottom/top device names)
- Missing prerequisites (loopback, BGP process)
- Integration via build_routing_plan()
"""

from typing import Any
from unittest.mock import MagicMock

from generators.helpers.routing import RoutingPlan, RoutingPlanInput, RoutingPlanner

# ================================================================
# Test Data Helpers
# ================================================================


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


def _device_map_entry(
    name: str, device_id: str, role: str, loopback_ip: str | None = None
) -> tuple[str, dict[str, Any]]:
    """Return (name, info) for building a device_map."""
    info: dict[str, Any] = {"id": device_id, "role": role}
    if loopback_ip:
        info["router_id"] = {"id": f"ip-{device_id}"}
        info["loopback_ip"] = loopback_ip
    return name, info


def _bgp_process(name: str, device_id: str) -> dict[str, Any]:
    return {"name": name, "capabilities": [{"id": device_id}]}


def _design() -> MagicMock:
    d = MagicMock()
    d.model_dump = MagicMock(return_value={})
    return d


# ================================================================
# Standard spine-leaf data
# ================================================================

_SPINE_LEAF_DEVICE_MAP: dict[str, dict[str, Any]] = dict(
    [
        _device_map_entry("spine-1", "s1", "spine", "10.0.0.1"),
        _device_map_entry("spine-2", "s2", "spine", "10.0.0.2"),
        _device_map_entry("leaf-1", "l1", "leaf", "10.0.1.1"),
        _device_map_entry("leaf-2", "l2", "leaf", "10.0.1.2"),
    ]
)


def _spine_leaf_bgp_processes(suffix: str = "overlay") -> list[dict]:
    return [
        _bgp_process(f"spine-1-bgp-{suffix}", "s1"),
        _bgp_process(f"spine-2-bgp-{suffix}", "s2"),
        _bgp_process(f"leaf-1-bgp-{suffix}", "l1"),
        _bgp_process(f"leaf-2-bgp-{suffix}", "l2"),
    ]


# ================================================================
# Helper to call _plan_overlay_peerings with plain device_map
# ================================================================


def _call_plan_overlay(
    planner: RoutingPlanner,
    overlay_type: str,
    bgp_processes: list[dict],
    device_map: dict[str, dict[str, Any]],
    bottom_device_names: set[str] | None = None,
    top_device_names: set[str] | None = None,
) -> list[dict]:
    """Call _plan_overlay_peerings and return plan.bgp_peerings."""
    plan = RoutingPlan()
    planner._plan_overlay_peerings(
        plan,
        overlay_type=overlay_type,
        bgp_processes=bgp_processes,
        device_map=device_map,
        bottom_device_names=bottom_device_names,
        top_device_names=top_device_names,
    )
    return plan.bgp_peerings


# ================================================================
# iBGP Overlay Tests
# ================================================================


class TestIBGPOverlayPeerings:
    """Test iBGP overlay peering creation via _plan_overlay_peerings."""

    def test_route_reflector_basic(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        assert len(peerings) == 4
        for p in peerings:
            assert p["session_type"] == "IBGP"
            assert p["ttl"] == 255
            assert p["bfd_enabled"] is True
            assert p["send_community"] is True
            assert p["send_extended_community"] is True
            assert p["route_reflector_client"] is True

    def test_route_reflector_peering_names_sorted(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        for p in peerings:
            name = p["name"]
            parts = name.split("--")
            assert parts[1] <= parts[2], f"Peering name not sorted: {name}"

    def test_route_reflector_bgp_process_refs(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        all_hfids = set()
        for p in peerings:
            for ref in p["bgp_processes"]:
                all_hfids.add(ref["hfid"])

        expected_names = {"spine-1-bgp-overlay", "spine-2-bgp-overlay", "leaf-1-bgp-overlay", "leaf-2-bgp-overlay"}
        assert all_hfids == expected_names

    def test_with_tors(self) -> None:
        device_map = dict(
            [
                *_SPINE_LEAF_DEVICE_MAP.items(),
                _device_map_entry("tor-1", "t1", "tor", "10.0.2.1"),
                _device_map_entry("tor-2", "t2", "tor", "10.0.2.2"),
            ]
        )
        bgp_procs = _spine_leaf_bgp_processes() + [
            _bgp_process("tor-1-bgp-overlay", "t1"),
            _bgp_process("tor-2-bgp-overlay", "t2"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
        )

        # (2 leafs + 2 tors) x 2 spines = 8
        assert len(peerings) == 8


# ================================================================
# eBGP Overlay Tests
# ================================================================


class TestEBGPOverlayPeerings:
    def test_ebgp_uses_spine_leaf_topology(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ebgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        assert len(peerings) == 4
        for p in peerings:
            assert p["session_type"] == "EBGP_MULTIHOP"
            assert p["ttl"] == 2
            assert p["route_reflector_client"] is False

    def test_ebgp_with_tors(self) -> None:
        device_map = dict(
            [
                *_SPINE_LEAF_DEVICE_MAP.items(),
                _device_map_entry("tor-1", "t1", "tor", "10.0.2.1"),
                _device_map_entry("tor-2", "t2", "tor", "10.0.2.2"),
            ]
        )
        bgp_procs = _spine_leaf_bgp_processes() + [
            _bgp_process("tor-1-bgp-overlay", "t1"),
            _bgp_process("tor-2-bgp-overlay", "t2"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ebgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
        )

        assert len(peerings) == 8


# ================================================================
# Scoped Filtering Tests
# ================================================================


class TestOverlayScopeFiltering:
    def test_scope_filters_to_process_devices(self) -> None:
        device_map = dict(
            [
                *_SPINE_LEAF_DEVICE_MAP.items(),
                _device_map_entry("tor-1", "t1", "tor", "10.0.2.1"),
                _device_map_entry("tor-2", "t2", "tor", "10.0.2.2"),
            ]
        )
        bgp_procs = _spine_leaf_bgp_processes() + [
            _bgp_process("tor-1-bgp-overlay", "t1"),
            _bgp_process("tor-2-bgp-overlay", "t2"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")

        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
            bottom_device_names={"leaf-1", "leaf-2"},
            top_device_names={"spine-1", "spine-2"},
        )

        for p in peerings:
            desc = p["description"]
            assert "leaf-1" in desc or "leaf-2" in desc

    def test_scope_none_includes_all(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")

        all_peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        scoped_peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
            bottom_device_names={"spine-1", "spine-2", "leaf-1", "leaf-2"},
            top_device_names=set(),
        )

        assert len(all_peerings) == len(scoped_peerings)

    def test_scope_single_device_only_its_peerings(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map=_SPINE_LEAF_DEVICE_MAP,
            bottom_device_names={"leaf-1"},
            top_device_names={"spine-1", "spine-2"},
        )

        assert len(peerings) == 2
        for p in peerings:
            assert "leaf-1" in p["description"]


# ================================================================
# Missing Prerequisites Tests
# ================================================================


class TestOverlayMissingPrerequisites:
    def test_missing_loopback_skips_device(self) -> None:
        device_map = dict(
            [
                _device_map_entry("spine-1", "s1", "spine", "10.0.0.1"),
                _device_map_entry("leaf-1", "l1", "leaf", "10.0.1.1"),
                ("leaf-2", {"id": "l2", "role": "leaf", "router_id": {"id": "ip-l2"}}),  # no loopback_ip
            ]
        )
        bgp_procs = [
            _bgp_process("spine-1-bgp-overlay", "s1"),
            _bgp_process("leaf-1-bgp-overlay", "l1"),
            _bgp_process("leaf-2-bgp-overlay", "l2"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
        )

        assert len(peerings) == 1
        assert "leaf-2" not in peerings[0]["description"]

    def test_missing_bgp_process_skips_session(self) -> None:
        bgp_procs = [
            _bgp_process("spine-1-bgp-overlay", "s1"),
            _bgp_process("leaf-1-bgp-overlay", "l1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        assert len(peerings) == 1
        hfids = {ref["hfid"] for p in peerings for ref in p["bgp_processes"]}
        assert hfids == {"spine-1-bgp-overlay", "leaf-1-bgp-overlay"}

    def test_empty_bgp_processes_returns_empty(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=[],
            device_map=_SPINE_LEAF_DEVICE_MAP,
        )

        assert peerings == []

    def test_empty_devices_returns_empty(self) -> None:
        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=_spine_leaf_bgp_processes(),
            device_map={},
        )

        assert peerings == []

    def test_no_loopback_ip_omits_device(self) -> None:
        """Device without loopback_ip is excluded from peerings."""
        device_map = dict(
            [
                _device_map_entry("spine-1", "s1", "spine", "10.0.0.1"),
                ("leaf-1", {"id": "l1", "role": "leaf", "router_id": {"id": "ip-l1"}}),  # no loopback_ip
            ]
        )
        bgp_procs = [
            _bgp_process("spine-1-bgp-overlay", "s1"),
            _bgp_process("leaf-1-bgp-overlay", "l1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        peerings = _call_plan_overlay(
            planner,
            overlay_type="ibgp",
            bgp_processes=bgp_procs,
            device_map=device_map,
        )

        assert len(peerings) == 0


# ================================================================
# Integration via build_routing_plan
# ================================================================


class TestOverlayIntegration:
    def test_ebgp_ebgp_creates_overlay_peerings(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.1.2"),
        ]
        pool = "pool-1"
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if3", "Ethernet1/2", "s1", "c2"),
            _make_p2p_interface("if4", "Ethernet1/1", "l2", "c2"),
            _make_p2p_interface("if5", "Ethernet1/1", "s2", "c3"),
            _make_p2p_interface("if6", "Ethernet1/2", "l1", "c3"),
            _make_p2p_interface("if7", "Ethernet1/2", "s2", "c4"),
            _make_p2p_interface("if8", "Ethernet1/2", "l2", "c4"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1", "spine-2", "leaf-1", "leaf-2"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": pool, "design": _design()},
                routing_strategy="ebgp-ebgp",
                deployment_name="dc1",
            )
        )

        overlay = [p for p in result.bgp_peerings if p["name"].startswith("overlay")]
        assert len(overlay) == 4
        for p in overlay:
            assert p["session_type"] == "EBGP_MULTIHOP"

    def test_ebgp_ibgp_creates_overlay_peerings(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.1.2"),
        ]
        pool = "pool-1"
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if3", "Ethernet1/2", "s1", "c2"),
            _make_p2p_interface("if4", "Ethernet1/1", "l2", "c2"),
            _make_p2p_interface("if5", "Ethernet1/1", "s2", "c3"),
            _make_p2p_interface("if6", "Ethernet1/2", "l1", "c3"),
            _make_p2p_interface("if7", "Ethernet1/2", "s2", "c4"),
            _make_p2p_interface("if8", "Ethernet1/2", "l2", "c4"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1", "spine-2", "leaf-1", "leaf-2"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={
                    "asn_pool": pool,
                    "overlay_as_id": "shared-as-1",
                    "design": _design(),
                },
                routing_strategy="ebgp-ibgp",
                deployment_name="dc1",
            )
        )

        overlay = [p for p in result.bgp_peerings if p["name"].startswith("overlay")]
        assert len(overlay) == 4
        for p in overlay:
            assert p["session_type"] == "IBGP"

    def test_no_design_skips_overlay(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
        ]
        pool = "pool-1"
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["spine-1", "leaf-1"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": pool},
                routing_strategy="ebgp-ebgp",
                deployment_name="dc1",
            )
        )

        overlay = [p for p in result.bgp_peerings if p["name"].startswith("overlay")]
        assert overlay == []

    def test_scoped_overlay_via_build_routing_plan(self) -> None:
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.1.2"),
        ]
        pool = "pool-1"
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if3", "Ethernet1/2", "s1", "c2"),
            _make_p2p_interface("if4", "Ethernet1/1", "l2", "c2"),
            _make_p2p_interface("if5", "Ethernet1/1", "s2", "c3"),
            _make_p2p_interface("if6", "Ethernet1/2", "l1", "c3"),
            _make_p2p_interface("if7", "Ethernet1/2", "s2", "c4"),
            _make_p2p_interface("if8", "Ethernet1/2", "l2", "c4"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")

        result = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["leaf-1"],
                top_devices=["spine-1", "spine-2"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": pool, "design": _design()},
                routing_strategy="ebgp-ebgp",
                deployment_name="dc1",
            )
        )

        overlay = [p for p in result.bgp_peerings if p["name"].startswith("overlay")]
        for p in overlay:
            assert "leaf-1" in p["description"]
