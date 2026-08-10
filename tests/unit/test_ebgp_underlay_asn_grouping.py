"""Unit tests for eBGP-eBGP underlay ASN grouping (.dev/bgp.txt).

ASN is allocated per GROUP, not per device:
  - MLAG-paired leaf/tor/l2-leaf/access-leaf: both devices in the pair
    share ONE underlay ASN (RoutingPlanInput.mlag_pairs, keyed by MLAG
    domain name — see generators/routing.py's ManagedMLAG query).
  - Pod-shared spine / fabric-wide super-spine ASN: every bottom device in
    the call shares ONE pre-resolved ASN, bypassing the pool entirely
    (RoutingPlanInput.options["shared_underlay_as_id"], resolved by
    pod.py/dc.py before calling create_routing()).
  - A standalone (non-MLAG) device keeps the original per-device behavior.

Tests call ``RoutingPlanner._plan_ebgp_underlay`` directly (bypassing
``build_routing_plan``) since it's the sole owner of this grouping logic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from generators.helpers.routing import PendingASRef, RoutingPlan, RoutingPlanner


def _device_map(names: list[str]) -> dict[str, dict]:
    return {name: {"id": f"dev-{name}", "router_id": {"id": f"ip-{name}"}} for name in names}


def _planner() -> RoutingPlanner:
    return RoutingPlanner(deployment_id="dc-1", logger=MagicMock())


class TestMlagPairSharedAsn:
    """Two MLAG-paired devices must share one ASN — one autonomous_systems
    entry per domain, both BGP processes point local_as at the same group key."""

    def test_new_mlag_pair_shares_one_pending_as_ref(self) -> None:
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["leaf-01", "leaf-02"])
        mlag_pairs = {"leaf-01": "leaf-01-leaf-02-mlag", "leaf-02": "leaf-01-leaf-02-mlag"}

        planner._plan_ebgp_underlay(
            plan, device_map, interfaces=[], existing_as_by_device={}, asn_pool="pool-1", mlag_pairs=mlag_pairs
        )

        # Exactly ONE autonomous_systems entry for the pair, not two.
        assert len(plan.autonomous_systems) == 1
        assert plan.autonomous_systems[0]["_for_device"] == "leaf-01-leaf-02-mlag"
        assert "from_pool" in plan.autonomous_systems[0]["asn"]

        assert len(plan.bgp_processes) == 2
        for bgp in plan.bgp_processes:
            local_as = bgp["local_as"]
            assert isinstance(local_as, PendingASRef)
            assert local_as.device == "leaf-01-leaf-02-mlag"

    def test_standalone_device_keeps_per_device_asn(self) -> None:
        """A device NOT present in mlag_pairs (e.g. odd device out) keeps
        today's behavior: its own name is its own group."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["leaf-01", "leaf-02", "leaf-03"])
        mlag_pairs = {"leaf-01": "leaf-01-leaf-02-mlag", "leaf-02": "leaf-01-leaf-02-mlag"}

        planner._plan_ebgp_underlay(
            plan, device_map, interfaces=[], existing_as_by_device={}, asn_pool="pool-1", mlag_pairs=mlag_pairs
        )

        # One shared entry for the pair + one standalone entry for leaf-03.
        assert len(plan.autonomous_systems) == 2
        for_devices = {entry["_for_device"] for entry in plan.autonomous_systems}
        assert for_devices == {"leaf-01-leaf-02-mlag", "leaf-03"}

        leaf03_bgp = next(b for b in plan.bgp_processes if b["name"] == "leaf-03-bgp-underlay")
        assert leaf03_bgp["local_as"].device == "leaf-03"

    def test_no_mlag_pairs_matches_original_per_device_behavior(self) -> None:
        """Backward compatibility: omitting mlag_pairs entirely (default)
        allocates one ASN per device, exactly like before this feature."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["leaf-01", "leaf-02"])

        planner._plan_ebgp_underlay(plan, device_map, interfaces=[], existing_as_by_device={}, asn_pool="pool-1")

        assert len(plan.autonomous_systems) == 2
        for_devices = {entry["_for_device"] for entry in plan.autonomous_systems}
        assert for_devices == {"leaf-01", "leaf-02"}

    def test_existing_mismatched_pair_asns_converge_on_canonical_id(self) -> None:
        """A pair whose two devices already have DIFFERENT existing underlay
        AS ids (today's bug, on a fabric that ran once before) converges
        onto the lower-sorted id — self-heals with no migration script."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["leaf-01", "leaf-02"])
        mlag_pairs = {"leaf-01": "leaf-01-leaf-02-mlag", "leaf-02": "leaf-01-leaf-02-mlag"}
        existing_as_by_device = {"leaf-01": "as-id-bbb", "leaf-02": "as-id-aaa"}

        planner._plan_ebgp_underlay(
            plan,
            device_map,
            interfaces=[],
            existing_as_by_device=existing_as_by_device,
            asn_pool="pool-1",
            mlag_pairs=mlag_pairs,
        )

        assert len(plan.autonomous_systems) == 1
        assert plan.autonomous_systems[0]["_existing_id"] == "as-id-aaa"
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": "as-id-aaa"}

    def test_existing_matching_pair_asns_reused_without_new_allocation(self) -> None:
        """Both devices already share the SAME existing AS id (the healthy,
        already-correct case) — reused as-is, no new pool draw."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["leaf-01", "leaf-02"])
        mlag_pairs = {"leaf-01": "leaf-01-leaf-02-mlag", "leaf-02": "leaf-01-leaf-02-mlag"}
        existing_as_by_device = {"leaf-01": "as-id-shared", "leaf-02": "as-id-shared"}

        planner._plan_ebgp_underlay(
            plan,
            device_map,
            interfaces=[],
            existing_as_by_device=existing_as_by_device,
            asn_pool="pool-1",
            mlag_pairs=mlag_pairs,
        )

        assert len(plan.autonomous_systems) == 1
        assert plan.autonomous_systems[0]["_existing_id"] == "as-id-shared"
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": "as-id-shared"}


class TestSharedAsIdBypass:
    """shared_as_id bypasses grouping AND the pool entirely — every bottom
    device reuses the single pre-resolved AS id directly (pod-shared spine
    ASN / fabric-wide super-spine ASN)."""

    def test_every_device_uses_the_shared_id_directly(self) -> None:
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["spine-01", "spine-02", "spine-03"])

        planner._plan_ebgp_underlay(
            plan, device_map, interfaces=[], existing_as_by_device={}, asn_pool="pool-1", shared_as_id="as-shared-1"
        )

        # No pool draw at all — shared_as_id bypasses autonomous_systems entirely.
        assert plan.autonomous_systems == []
        assert len(plan.bgp_processes) == 3
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": "as-shared-1"}

    def test_shared_as_id_ignores_existing_as_by_device(self) -> None:
        """shared_as_id takes priority even when some devices already have
        a DIFFERENT existing per-device AS — the pod/super-spine generator
        is the single source of truth once it resolves the shared id."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["spine-01", "spine-02"])
        existing_as_by_device = {"spine-01": "as-old-1", "spine-02": "as-old-2"}

        planner._plan_ebgp_underlay(
            plan,
            device_map,
            interfaces=[],
            existing_as_by_device=existing_as_by_device,
            asn_pool="pool-1",
            shared_as_id="as-shared-new",
        )

        assert plan.autonomous_systems == []
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": "as-shared-new"}

    def test_shared_as_id_and_mlag_pairs_together_shared_wins(self) -> None:
        """Spines are never MLAG-paired in this project, but if mlag_pairs
        were ever passed alongside shared_as_id, shared_as_id must win —
        it's the caller's explicit, pre-resolved single source of truth."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["spine-01", "spine-02"])
        mlag_pairs = {"spine-01": "spine-01-spine-02-mlag", "spine-02": "spine-01-spine-02-mlag"}

        planner._plan_ebgp_underlay(
            plan,
            device_map,
            interfaces=[],
            existing_as_by_device={},
            asn_pool="pool-1",
            mlag_pairs=mlag_pairs,
            shared_as_id="as-shared-1",
        )

        assert plan.autonomous_systems == []
        for bgp in plan.bgp_processes:
            assert bgp["local_as"] == {"id": "as-shared-1"}


class TestTopDevicesExcludedFromGrouping:
    def test_top_devices_never_get_as_or_bgp(self) -> None:
        """Top devices (e.g. super-spines when called from pod.py for
        spines) are never planned here — their BGP is owned by an upper
        generator layer, regardless of MLAG/shared_as_id grouping."""
        planner = _planner()
        plan = RoutingPlan()
        device_map = _device_map(["spine-01", "ss-01"])

        planner._plan_ebgp_underlay(
            plan,
            device_map,
            interfaces=[],
            existing_as_by_device={},
            asn_pool="pool-1",
            top_device_names={"ss-01"},
        )

        for_devices = {entry["_for_device"] for entry in plan.autonomous_systems}
        assert for_devices == {"spine-01"}
        assert len(plan.bgp_processes) == 1
        assert plan.bgp_processes[0]["name"] == "spine-01-bgp-underlay"
