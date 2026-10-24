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

from generators.helpers.routing import PendingASRef, RoutingPlan, RoutingPlanInput, RoutingPlanner
from generators.types import RoutingOptions
from tests.unit.routing_helpers import make_loopback, make_p2p_interface


def _local_as_signature(local_as: Any) -> tuple:
    if isinstance(local_as, PendingASRef):
        return ("pending", local_as.device)
    return tuple(sorted(local_as.items()))


def _design() -> MagicMock:
    d = MagicMock(routing_strategy="ebgp-ebgp")
    d.model_dump = MagicMock(return_value={})
    return d


def _spine_leaf_topology() -> tuple[list, list]:
    """Two spines + two leafs, fully cabled — returns (loopbacks, interfaces)."""
    loopbacks = [
        make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
        make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
        make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
        make_loopback("leaf-2", "l2", "leaf", "10.0.1.2"),
    ]
    interfaces = [
        make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
        make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
        make_p2p_interface("if3", "Ethernet1/2", "s1", "c2"),
        make_p2p_interface("if4", "Ethernet1/1", "l2", "c2"),
        make_p2p_interface("if5", "Ethernet1/1", "s2", "c3"),
        make_p2p_interface("if6", "Ethernet1/2", "l1", "c3"),
        make_p2p_interface("if7", "Ethernet1/2", "s2", "c4"),
        make_p2p_interface("if8", "Ethernet1/2", "l2", "c4"),
    ]
    return loopbacks, interfaces


def _plan_input(loopbacks: list, interfaces: list, strategy: str = "ebgp-ebgp") -> RoutingPlanInput:
    options: RoutingOptions = RoutingOptions(asn_pool="pool-1", design=_design())
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
    )


def _signature(plan: RoutingPlan) -> dict[str, Any]:
    """Stable, comparable view of a plan (order-insensitive where order is irrelevant)."""
    return {
        "bgp_processes": sorted(
            (
                p["name"],
                _local_as_signature(p.get("local_as", {})),
                p["router_id"]["id"],
                p["capabilities"][0]["id"],
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
        lb_low = make_loopback("spine-1", "s1", "spine", "10.0.0.1", lb_id="lb-a")
        lb_high = make_loopback("spine-1", "s1", "spine", "10.0.0.9", lb_id="lb-z")
        # ip_address ids differ so we can tell which won
        lb_low.ip_address.id = "ip-low"
        lb_high.ip_address.id = "ip-high"

        planner = RoutingPlanner(deployment_id="dc-1")
        dm_forward = planner._build_device_map([lb_low, lb_high])
        dm_reverse = planner._build_device_map([lb_high, lb_low])

        assert dm_forward["spine-1"]["router_id"] == {"id": "ip-low"}
        assert dm_forward["spine-1"] == dm_reverse["spine-1"]


class TestSelfLoopGuard:
    """Regression tests for the self-loop peering bug.

    Parallel rack generators can race such that both fabric-p2p endpoints of a
    cable temporarily appear to belong to the same device (e.g., a spine gets its
    interface tagged twice before the TOR side is committed).  The planner must
    silently drop these entries rather than emitting a peering that references
    only one device.
    """

    def test_self_loop_cable_produces_no_peering(self) -> None:
        """A cable whose both endpoints resolve to the same device must be skipped."""
        loopbacks = [
            make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            make_loopback("spine-2", "s2", "spine", "10.0.0.2"),
            make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
        ]
        interfaces = [
            # Normal cable: spine-1 ↔ leaf-1
            make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            # Self-loop: both endpoints on spine-2 (race-condition artefact)
            make_p2p_interface("if3", "Ethernet1/8", "s2", "c-selfloop"),
            make_p2p_interface("if4", "Ethernet1/8", "s2", "c-selfloop"),
        ]
        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["leaf-1"],
                top_devices=["spine-1", "spine-2"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": "pool-1", "design": _design()},
                routing_strategy="ebgp-ebgp",
            )
        )
        peering_names = [p["name"] for p in plan.bgp_peerings]
        assert not any("spine-2--Ethernet1_8--spine-2" in n for n in peering_names), (
            f"Self-loop peering was created: {peering_names}"
        )

    def test_normal_peerings_still_created_alongside_self_loop(self) -> None:
        """Self-loop guard must not suppress valid peerings on the same run."""
        loopbacks = [
            make_loopback("spine-1", "s1", "spine", "10.0.0.1"),
            make_loopback("leaf-1", "l1", "leaf", "10.0.1.1"),
        ]
        interfaces = [
            make_p2p_interface("if1", "Ethernet1/1", "s1", "c1"),
            make_p2p_interface("if2", "Ethernet1/1", "l1", "c1"),
            # Self-loop on spine-1 (race artefact)
            make_p2p_interface("if3", "Ethernet1/99", "s1", "c-selfloop"),
            make_p2p_interface("if4", "Ethernet1/99", "s1", "c-selfloop"),
        ]
        planner = RoutingPlanner(deployment_id="dc-1")
        plan = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=["leaf-1"],
                top_devices=["spine-1"],
                interfaces=interfaces,
                loopback_interfaces=loopbacks,
                options={"asn_pool": "pool-1", "design": _design()},
                routing_strategy="ebgp-ebgp",
            )
        )
        peering_names = [p["name"] for p in plan.bgp_peerings]
        # The valid leaf-1 ↔ spine-1 peering must exist
        assert any("leaf-1" in n and "spine-1" in n for n in peering_names), f"Valid peering missing: {peering_names}"
        # The self-loop must not exist
        assert not any("spine-1--Ethernet1_99--spine-1" in n for n in peering_names), (
            f"Self-loop peering was created: {peering_names}"
        )
