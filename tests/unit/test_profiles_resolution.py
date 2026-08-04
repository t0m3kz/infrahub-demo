from __future__ import annotations

from generators.pod_config import STANDARD_PROFILE_ASSUMPTIONS, pod_profile, resolve_pod_layout


def test_pod_profile_is_resolved_per_pod_instance() -> None:
    profile_a = pod_profile(
        pod_id="pod-a", pod_name="POD-A", layout_name="S_MIXED", deployment_type="mixed", dc_id="dc-1", dc_name="DC1"
    )
    profile_b = pod_profile(
        pod_id="pod-b", pod_name="POD-B", layout_name="S_MIXED", deployment_type="mixed", dc_id="dc-1", dc_name="DC1"
    )

    assert profile_a["owner_id"] == "pod-a"
    assert profile_b["owner_id"] == "pod-b"
    assert profile_a["profile_template"] == "S_MIXED"
    assert profile_b["profile_template"] == "S_MIXED"
    assert profile_a["profile_name"] != profile_b["profile_name"]


def test_pod_profile_suite_container_and_row_slots() -> None:
    mixed_profile = pod_profile(
        pod_id="pod-mixed",
        pod_name="POD-MIXED",
        layout_name="S_MIXED",
        deployment_type="mixed",
        dc_id="dc-2",
        dc_name="DC2",
    )
    middle_profile = pod_profile(
        pod_id="pod-middle",
        pod_name="POD-MIDDLE",
        layout_name="S_MIDDLE",
        deployment_type="middle_rack",
        dc_id="dc-2",
        dc_name="DC2",
    )

    assert mixed_profile["suite_container_mode"] is True
    assert mixed_profile["oversubscription"] == STANDARD_PROFILE_ASSUMPTIONS.oversubscription
    assert mixed_profile["homing"] == STANDARD_PROFILE_ASSUMPTIONS.homing
    assert mixed_profile["row_dependent_rack_slots_per_row"] == mixed_profile["compute_racks_per_row"]
    assert middle_profile["row_dependent_rack_slots_per_row"] == 0


def test_hard_enforced_layouts_match_spine_port_budget() -> None:
    for layout_name in ("S_TOR", "S_MIXED", "M_MIXED", "L_MIXED"):
        layout = resolve_pod_layout(layout_name)
        assert layout["enforce_compute_racks_from_spine_budget"] is True
        assert layout["compute_racks_per_row"] > 0
