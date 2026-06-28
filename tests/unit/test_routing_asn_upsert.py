"""Unit tests for ASN allocation and upsert idempotency.

Tests verify:
- Existing AS path: preserved as _existing_id in autonomous_systems
- New device path: from_pool allocation for genuinely new devices
- Mixed scenarios: existing + new devices in same plan
- Rerun idempotency: identical plans from identical inputs
- BGP process references: existing AS by id, new AS by _for_device
"""

from typing import Any
from unittest.mock import MagicMock

from generators.helpers.routing import RoutingPlanInput, RoutingPlanner

# ================================================================
# Helpers
# ================================================================


def _make_pool(pool_id: str = "pool-1") -> str:
    """Return a pool ID string (planner uses string IDs, not SDK objects)."""
    return pool_id


def _make_loopback(
    name: str,
    device_id: str,
    role: str,
    ip: str,
) -> MagicMock:
    lb = MagicMock()
    lb.id = f"lb-{device_id}"
    lb.device.peer.name.value = name
    lb.device.peer.id = device_id
    lb.device.peer.role.value = role
    lb.ip_address.id = f"ip-{device_id}"
    lb.ip_address.display_label = f"{ip}/32"
    return lb


def _make_existing_bgp(
    name: str,
    device_name: str,
    as_id: str | None = None,
    asn: int | None = None,
) -> MagicMock:
    bgp = MagicMock()
    bgp.id = f"bgp-{device_name}-underlay"
    bgp.name.value = name
    bgp.capabilities.peers[0].name.value = device_name
    bgp.capabilities.peers[0].id = f"dev-{device_name}"
    bgp.local_as.id = as_id
    return bgp


def _make_p2p_interface(
    iface_id: str,
    iface_name: str,
    device_id: str,
    cable_id: str,
) -> MagicMock:
    iface = MagicMock()
    iface.id = iface_id
    iface.name.value = iface_name
    iface.cable.id = cable_id
    iface.device.id = device_id
    return iface


def _build_existing_bgp_for_device(
    device_name: str,
    as_id: str,
    asn: int,
) -> list[MagicMock]:
    """Build the underlay ManagedBGP SDK mock for a device with existing AS."""
    return [
        _make_existing_bgp(
            name=f"{device_name}-bgp-underlay",
            device_name=device_name,
            as_id=as_id,
            asn=asn,
        ),
    ]


def _existing_as(plan_result: Any) -> list[dict]:
    """Filter autonomous_systems for existing entries."""
    return [a for a in plan_result.autonomous_systems if "_existing_id" in a]


def _new_as(plan_result: Any) -> list[dict]:
    """Filter autonomous_systems for new entries (from_pool)."""
    return [a for a in plan_result.autonomous_systems if "_existing_id" not in a]


def _make_plan_input(
    loopbacks: list[MagicMock],
    pool: str | None = None,
    underlay: list[MagicMock] | None = None,
    interfaces: list[MagicMock] | None = None,
    design: Any = None,
    bottom_devices: list[str] | None = None,
    top_devices: list[str] | None = None,
) -> RoutingPlanInput:
    """Build a RoutingPlanInput with defaults."""
    device_names = [lb.device.peer.name.value for lb in loopbacks]
    # Deduplicate preserving order
    seen: set[str] = set()
    unique_names: list[str] = []
    for n in device_names:
        if n not in seen:
            seen.add(n)
            unique_names.append(n)

    options: dict[str, Any] = {}
    if pool is not None:
        options["asn_pool"] = pool
    if design is not None:
        options["design"] = design

    return RoutingPlanInput(
        bottom_devices=bottom_devices if bottom_devices is not None else unique_names,
        top_devices=top_devices if top_devices is not None else [],
        underlay=underlay or [],
        interfaces=interfaces or [],
        loopback_interfaces=loopbacks,
        options=options,
        routing_strategy="ebgp-ebgp",
        deployment_name="dc1",
    )


# ================================================================
# Existing AS Preservation Tests
# ================================================================


class TestExistingASPreservation:
    """Test that existing AS objects are tracked via _existing_id."""

    def test_existing_as_excluded_from_batch(self) -> None:
        """Device with existing AS should have _existing_id, not from_pool."""
        loopbacks = [_make_loopback("spine-1", "s1", "spine", "10.0.0.1")]
        pool = _make_pool()
        underlay = _build_existing_bgp_for_device("spine-1", "as-uuid-1", 65001)
        # Need a cable pair so the interface-driven loop processes the device
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1-peer", "c1"),
        ]
        # Add the peer device to loopbacks so it's in device_map
        loopbacks.append(_make_loopback("peer-1", "s1-peer", "spine", "10.0.0.2"))

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        )

        # spine-1 has existing AS
        existing = [a for a in plan.autonomous_systems if a.get("_existing_id") == "as-uuid-1"]
        assert len(existing) == 1
        assert existing[0]["_for_device"] == "spine-1"

    def test_existing_as_bgp_process_references_id_directly(self) -> None:
        """BGP process for existing AS should reference by id."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
        ]
        underlay = _build_existing_bgp_for_device("spine-1", "as-1", 65000)
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=_make_pool(), underlay=underlay, interfaces=interfaces)
        )

        underlay_bgp = [b for b in plan.bgp_processes if b["name"] == "spine-1-bgp-underlay"]
        assert len(underlay_bgp) == 1
        assert underlay_bgp[0]["local_as"] == {"id": "as-1"}

    def test_new_device_uses_from_pool(self) -> None:
        """Device without existing AS allocates from pool."""
        loopbacks = [
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
            _make_loopback("spine-1", "s1", "spine", "10.0.0.2"),
        ]
        pool = _make_pool("pool-abc")
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(_make_plan_input(loopbacks, pool=pool, interfaces=interfaces))

        new = _new_as(plan)
        assert len(new) == 2
        for a in new:
            assert isinstance(a["asn"], dict)
            assert a["asn"]["from_pool"]["id"] == "pool-abc"
        assert _existing_as(plan) == []

    def test_mixed_existing_and_new_devices(self) -> None:
        """Plan with both existing and new devices separates them correctly."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.0.3"),
            _make_loopback("tor-1", "t1", "tor", "10.0.0.4"),
        ]
        underlay = [
            *_build_existing_bgp_for_device("spine-1", "as-spine", 65000),
            *_build_existing_bgp_for_device("leaf-1", "as-leaf", 65001),
        ]
        pool = _make_pool()
        # Create cables: s1-l1, s1-l2, l1-t1, l2-t1 (enough to reach all devices)
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if3", "Ethernet1/2", "s1", "c2"),
            _make_p2p_interface("if4", "Ethernet1/1", "l2", "c2"),
            _make_p2p_interface("if5", "Ethernet2/1", "l1", "c3"),
            _make_p2p_interface("if6", "Ethernet1/1", "t1", "c3"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        )

        new = _new_as(plan)
        assert len(new) == 2
        new_devices = {obj["_for_device"] for obj in new}
        assert new_devices == {"leaf-2", "tor-1"}

        existing = _existing_as(plan)
        assert {o["_existing_id"] for o in existing} == {"as-spine", "as-leaf"}

    def test_existing_as_without_id_treated_as_new(self) -> None:
        """existing_as with None id falls through to pool allocation."""
        loopbacks = [
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
            _make_loopback("spine-1", "s1", "spine", "10.0.0.2"),
        ]
        bgp_with_no_as_id = _make_existing_bgp(
            name="leaf-1-bgp-underlay",
            device_name="leaf-1",
            as_id=None,
            asn=None,
        )
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=[bgp_with_no_as_id], interfaces=interfaces)
        )

        new = _new_as(plan)
        assert len(new) == 2
        for a in new:
            assert isinstance(a["asn"], dict)

    def test_no_pool_no_existing_as_skips_device(self) -> None:
        """Device with no pool and no existing AS is skipped."""
        loopbacks = [
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
            _make_loopback("spine-1", "s1", "spine", "10.0.0.2"),
        ]
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(_make_plan_input(loopbacks, interfaces=interfaces))

        assert plan.autonomous_systems == []


# ================================================================
# eBGP Overlay Process Tests (existing AS by id)
# ================================================================


class TestEbgpOverlayWithExistingAS:
    """Test that eBGP overlay processes correctly reference existing AS."""

    def test_overlay_process_uses_existing_as_id(self) -> None:
        """Overlay BGP process for existing device references AS by id."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
        ]
        underlay = _build_existing_bgp_for_device("spine-1", "as-1", 65000)
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        )

        overlay_procs = [p for p in plan.bgp_processes if p["name"] == "spine-1-bgp-overlay"]
        assert len(overlay_procs) == 1
        assert overlay_procs[0]["local_as"] == {"id": "as-1"}

    def test_overlay_process_uses_for_device_for_new(self) -> None:
        """Overlay BGP process for new device references AS via _for_device."""
        loopbacks = [
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
            _make_loopback("spine-1", "s1", "spine", "10.0.0.2"),
        ]
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "s1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(_make_plan_input(loopbacks, pool=pool, interfaces=interfaces))

        overlay_procs = [p for p in plan.bgp_processes if p["name"] == "leaf-1-bgp-overlay"]
        assert len(overlay_procs) == 1
        assert overlay_procs[0]["local_as"] == {"_for_device": "leaf-1"}

    def test_mixed_overlay_processes(self) -> None:
        """Overlay processes: existing by id, new by _for_device."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
        ]
        underlay = _build_existing_bgp_for_device("spine-1", "as-s1", 65000)
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        )

        overlay_procs = {p["name"]: p["local_as"] for p in plan.bgp_processes if p["name"].endswith("-bgp-overlay")}
        assert overlay_procs["spine-1-bgp-overlay"] == {"id": "as-s1"}
        assert overlay_procs["leaf-1-bgp-overlay"] == {"_for_device": "leaf-1"}


# ================================================================
# Mixed Scenario: 2 existing ToRs + 2 new ToRs (the 02_switch case)
# ================================================================


class TestMixedExistingNewToRs:
    """Reproduce the 02_switch scenario: adding new ToRs to existing rack."""

    def test_two_existing_two_new_tors(self) -> None:
        """2 existing ToRs keep their AS, 2 new ToRs get from_pool."""
        loopbacks = [
            _make_loopback("leaf-1", "lf1", "leaf", "10.0.0.10"),
            _make_loopback("tor-1", "t1", "tor", "10.0.0.1"),
            _make_loopback("tor-2", "t2", "tor", "10.0.0.2"),
            _make_loopback("tor-3", "t3", "tor", "10.0.0.3"),
            _make_loopback("tor-4", "t4", "tor", "10.0.0.4"),
        ]
        underlay = [
            *_build_existing_bgp_for_device("tor-1", "as-t1", 65101),
            *_build_existing_bgp_for_device("tor-2", "as-t2", 65102),
        ]
        pool = _make_pool("tor-asn-pool")
        # Each tor connects to leaf via cable
        interfaces = [
            _make_p2p_interface("if-lf1-1", "Ethernet1/1", "lf1", "c1"),
            _make_p2p_interface("if-t1-1", "Ethernet1/1", "t1", "c1"),
            _make_p2p_interface("if-lf1-2", "Ethernet1/2", "lf1", "c2"),
            _make_p2p_interface("if-t2-1", "Ethernet1/1", "t2", "c2"),
            _make_p2p_interface("if-lf1-3", "Ethernet1/3", "lf1", "c3"),
            _make_p2p_interface("if-t3-1", "Ethernet1/1", "t3", "c3"),
            _make_p2p_interface("if-lf1-4", "Ethernet1/4", "lf1", "c4"),
            _make_p2p_interface("if-t4-1", "Ethernet1/1", "t4", "c4"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        )

        new = _new_as(plan)
        new_devices = {obj["_for_device"] for obj in new}
        # leaf-1, tor-3, tor-4 are new (no existing underlay BGP)
        assert "tor-3" in new_devices
        assert "tor-4" in new_devices

        existing = _existing_as(plan)
        assert {o["_existing_id"] for o in existing} == {"as-t1", "as-t2"}

    def test_rerun_with_all_existing(self) -> None:
        """After first run, all ToRs have existing AS — no new allocations."""
        loopbacks = [
            _make_loopback("leaf-1", "lf1", "leaf", "10.0.0.10"),
            _make_loopback("tor-1", "t1", "tor", "10.0.0.1"),
            _make_loopback("tor-2", "t2", "tor", "10.0.0.2"),
            _make_loopback("tor-3", "t3", "tor", "10.0.0.3"),
            _make_loopback("tor-4", "t4", "tor", "10.0.0.4"),
        ]
        underlay = [
            *_build_existing_bgp_for_device("leaf-1", "as-lf1", 65100),
            *_build_existing_bgp_for_device("tor-1", "as-t1", 65101),
            *_build_existing_bgp_for_device("tor-2", "as-t2", 65102),
            *_build_existing_bgp_for_device("tor-3", "as-t3", 65103),
            *_build_existing_bgp_for_device("tor-4", "as-t4", 65104),
        ]
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if-lf1-1", "Ethernet1/1", "lf1", "c1"),
            _make_p2p_interface("if-t1-1", "Ethernet1/1", "t1", "c1"),
            _make_p2p_interface("if-lf1-2", "Ethernet1/2", "lf1", "c2"),
            _make_p2p_interface("if-t2-1", "Ethernet1/1", "t2", "c2"),
            _make_p2p_interface("if-lf1-3", "Ethernet1/3", "lf1", "c3"),
            _make_p2p_interface("if-t3-1", "Ethernet1/1", "t3", "c3"),
            _make_p2p_interface("if-lf1-4", "Ethernet1/4", "lf1", "c4"),
            _make_p2p_interface("if-t4-1", "Ethernet1/1", "t4", "c4"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        )

        assert _new_as(plan) == []
        assert len(_existing_as(plan)) == 5  # all devices have existing AS


# ================================================================
# Rerun Idempotency Tests
# ================================================================


class TestRoutingPlanIdempotency:
    """Test that identical inputs produce identical plans."""

    def test_underlay_plan_deterministic(self) -> None:
        """Two runs with same input produce identical plans."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.0.3"),
        ]
        underlay = [
            *_build_existing_bgp_for_device("spine-1", "as-1", 65000),
            *_build_existing_bgp_for_device("leaf-1", "as-2", 65001),
            *_build_existing_bgp_for_device("leaf-2", "as-3", 65002),
        ]
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if3", "Ethernet1/2", "s1", "c2"),
            _make_p2p_interface("if4", "Ethernet1/1", "l2", "c2"),
        ]
        design = MagicMock(bgp_topology="route_reflector", model_dump=MagicMock(return_value={}))

        planner = RoutingPlanner(deployment_id="dc-1")
        inp1 = _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces, design=design)
        inp2 = _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces, design=design)
        plan1 = planner.build_routing_plan(inp1)
        plan2 = planner.build_routing_plan(inp2)

        assert _new_as(plan1) == _new_as(plan2) == []
        ids1 = sorted(o["_existing_id"] for o in _existing_as(plan1))
        ids2 = sorted(o["_existing_id"] for o in _existing_as(plan2))
        assert ids1 == ids2

        bgp1 = [(p["name"], p["capabilities"][0]["id"]) for p in plan1.bgp_processes]
        bgp2 = [(p["name"], p["capabilities"][0]["id"]) for p in plan2.bgp_processes]
        assert bgp1 == bgp2

    def test_overlay_peerings_deterministic(self) -> None:
        """Overlay peerings are identical across runs."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.3"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.0.4"),
        ]
        underlay = [
            *_build_existing_bgp_for_device("spine-1", "as-1", 65000),
            *_build_existing_bgp_for_device("spine-2", "as-2", 65000),
            *_build_existing_bgp_for_device("leaf-1", "as-3", 65001),
            *_build_existing_bgp_for_device("leaf-2", "as-4", 65002),
        ]
        pool = _make_pool()
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
        design = MagicMock(bgp_topology="route_reflector", model_dump=MagicMock(return_value={}))

        planner = RoutingPlanner(deployment_id="dc-1")
        inp1 = _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces, design=design)
        inp2 = _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces, design=design)
        plan1 = planner.build_routing_plan(inp1)
        plan2 = planner.build_routing_plan(inp2)

        overlay1 = sorted(p["name"] for p in plan1.bgp_peerings if p["name"].startswith("overlay"))
        overlay2 = sorted(p["name"] for p in plan2.bgp_peerings if p["name"].startswith("overlay"))
        assert overlay1 == overlay2

    def test_underlay_peerings_deterministic(self) -> None:
        """Underlay peering names are stable across runs."""
        loopbacks = [
            _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.2"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.0.3"),
        ]
        underlay = [
            *_build_existing_bgp_for_device("spine-1", "a1", 65000),
            *_build_existing_bgp_for_device("leaf-1", "a2", 65001),
            *_build_existing_bgp_for_device("leaf-2", "a3", 65002),
        ]
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            _make_p2p_interface("if3", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/2", "s1", "c2"),
            _make_p2p_interface("if4", "Ethernet1/2", "l2", "c2"),
        ]
        pool = _make_pool()

        planner = RoutingPlanner(deployment_id="dc-1")
        inp1 = _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        inp2 = _make_plan_input(loopbacks, pool=pool, underlay=underlay, interfaces=interfaces)
        plan1 = planner.build_routing_plan(inp1)
        plan2 = planner.build_routing_plan(inp2)

        underlay1 = sorted(p["name"] for p in plan1.bgp_peerings if p["name"].startswith("underlay"))
        underlay2 = sorted(p["name"] for p in plan2.bgp_peerings if p["name"].startswith("underlay"))
        assert underlay1 == underlay2
        assert len(underlay1) >= 1

    def test_device_order_does_not_affect_as_allocation(self) -> None:
        """AS allocation order is deterministic regardless of loopback input order."""
        loopbacks_a = [
            _make_loopback("leaf-2", "l2", "leaf", "10.0.0.2"),
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
        ]
        loopbacks_b = [
            _make_loopback("leaf-1", "l1", "leaf", "10.0.0.1"),
            _make_loopback("leaf-2", "l2", "leaf", "10.0.0.2"),
        ]
        pool = _make_pool()
        interfaces = [
            _make_p2p_interface("if1", "Ethernet1/1", "l1", "c1"),
            _make_p2p_interface("if2", "Ethernet1/1", "l2", "c1"),
        ]

        planner = RoutingPlanner(deployment_id="dc-1")
        plan_a = planner.build_routing_plan(_make_plan_input(loopbacks_a, pool=pool, interfaces=interfaces))
        plan_b = planner.build_routing_plan(_make_plan_input(loopbacks_b, pool=pool, interfaces=interfaces))

        devs_a = [a["_for_device"] for a in plan_a.autonomous_systems]
        devs_b = [a["_for_device"] for a in plan_b.autonomous_systems]
        assert devs_a == devs_b
