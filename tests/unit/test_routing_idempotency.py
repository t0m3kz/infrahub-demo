"""Regression tests locking in routing-plan stability.

Two properties that make routing "as stable as p2p cabling":

1. Idempotency — building the plan twice from identical input yields identical
   output (names, AS refs, peerings). Re-runs must not churn.
2. Order-independence — the order loopbacks/interfaces arrive from the query must
   not change the plan. In particular router_id selection is deterministic
   (lowest loopback interface id wins), not "first in query-return order".
"""

from typing import Any
from unittest.mock import MagicMock

from generators.helpers.routing import RoutingPlan, RoutingPlanInput, RoutingPlanner


def _make_loopback(name: str, device_id: str, role: str, ip: str, lb_id: str | None = None) -> MagicMock:
    lb = MagicMock()
    lb.id = lb_id or f"lb-{device_id}"
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


def _design() -> MagicMock:
    d = MagicMock()
    d.model_dump = MagicMock(return_value={})
    return d


def _spine_leaf_topology() -> tuple[list, list]:
    """Two spines + two leafs, fully cabled — returns (loopbacks, interfaces)."""
    loopbacks = [
        _make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
        _make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
        _make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
        _make_loopback("leaf-2", "l2", "leaf", "10.0.1.2"),
    ]
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
    return loopbacks, interfaces


def _plan_input(loopbacks: list, interfaces: list, strategy: str = "ebgp-ebgp") -> RoutingPlanInput:
    options: dict[str, Any] = {"asn_pool": "pool-1", "design": _design()}
    if strategy != "ebgp-ebgp":
        options["overlay_as_id"] = "shared-as-1"
    if strategy.startswith("ospf"):
        options["ospf_area_id"] = "area-0"
    return RoutingPlanInput(
        bottom_devices=["spine-1", "spine-2", "leaf-1", "leaf-2"],
        interfaces=interfaces,
        loopback_interfaces=loopbacks,
        options=options,
        routing_strategy=strategy,
        deployment_name="dc1",
    )


def _signature(plan: RoutingPlan) -> dict[str, Any]:
    """Stable, comparable view of a plan (order-insensitive where order is irrelevant)."""
    return {
        "bgp_processes": sorted(
            (
                p["name"],
                tuple(sorted(p.get("local_as", {}).items())),
                p["router_id"]["id"],
                p["device_capabilities"][0]["id"],
            )
            for p in plan.bgp_processes
        ),
        "bgp_peerings": sorted(
            (p["name"], p["session_type"], tuple(sorted(r["hfid"] for r in p["bgp_processes"])))
            for p in plan.bgp_peerings
        ),
        "autonomous_systems": sorted(
            (a.get("_for_device", ""), a.get("_existing_id", ""), "from_pool" if "asn" in a else "")
            for a in plan.autonomous_systems
        ),
    }


class TestPlanIdempotency:
    def test_plan_built_twice_is_identical(self) -> None:
        loopbacks, interfaces = _spine_leaf_topology()
        planner = RoutingPlanner(deployment_id="dc-1")

        first = planner.build_routing_plan(_plan_input(loopbacks, interfaces))
        second = planner.build_routing_plan(_plan_input(loopbacks, interfaces))

        assert _signature(first) == _signature(second)

    def test_idempotent_for_ebgp_ibgp(self) -> None:
        loopbacks, interfaces = _spine_leaf_topology()
        planner = RoutingPlanner(deployment_id="dc-1")

        first = planner.build_routing_plan(_plan_input(loopbacks, interfaces, strategy="ebgp-ibgp"))
        second = planner.build_routing_plan(_plan_input(loopbacks, interfaces, strategy="ebgp-ibgp"))

        assert _signature(first) == _signature(second)


class TestOrderIndependence:
    def test_shuffled_loopbacks_give_same_plan(self) -> None:
        loopbacks, interfaces = _spine_leaf_topology()
        planner = RoutingPlanner(deployment_id="dc-1")

        normal = planner.build_routing_plan(_plan_input(loopbacks, interfaces))
        reversed_plan = planner.build_routing_plan(_plan_input(list(reversed(loopbacks)), interfaces))

        assert _signature(normal) == _signature(reversed_plan)

    def test_shuffled_interfaces_give_same_plan(self) -> None:
        loopbacks, interfaces = _spine_leaf_topology()
        planner = RoutingPlanner(deployment_id="dc-1")

        normal = planner.build_routing_plan(_plan_input(loopbacks, interfaces))
        reordered = planner.build_routing_plan(_plan_input(loopbacks, list(reversed(interfaces))))

        assert _signature(normal) == _signature(reordered)

    def test_router_id_picks_lowest_loopback_id_regardless_of_order(self) -> None:
        """Two loopbacks on one device: the lowest interface id wins deterministically."""
        # Same device "spine-1" with two loopbacks; lb id "lb-a" < "lb-z"
        lb_low = _make_loopback("spine-1", "s1", "spine", "10.0.0.1", lb_id="lb-a")
        lb_high = _make_loopback("spine-1", "s1", "spine", "10.0.0.9", lb_id="lb-z")
        # ip_address ids differ so we can tell which won
        lb_low.ip_address.id = "ip-low"
        lb_high.ip_address.id = "ip-high"

        planner = RoutingPlanner(deployment_id="dc-1")
        dm_forward = planner._build_device_map([lb_low, lb_high])
        dm_reverse = planner._build_device_map([lb_high, lb_low])

        assert dm_forward["spine-1"]["router_id"] == {"id": "ip-low"}
        assert dm_forward["spine-1"] == dm_reverse["spine-1"]
