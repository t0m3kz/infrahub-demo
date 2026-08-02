from __future__ import annotations

from generators.models import (
    STANDARD_PROFILE_ASSUMPTIONS,
    DCModel,
    Device,
    PodLayout,
    PodModel,
    PodParent,
)


def _build_pod_parent(dc_id: str, dc_name: str, size: str = "S") -> PodParent:
    return PodParent(
        id=dc_id,
        name=dc_name,
        index=1,
        size=size,
        devices=[Device(name="dummy-spine", role="spine")],
    )


def test_dc_profile_is_resolved_per_dc_instance() -> None:
    dc_a = DCModel(id="dc-a", name="DC-A", index=1, size="M")
    dc_b = DCModel(id="dc-b", name="DC-B", index=2, size="M")

    profile_a = dc_a.profile
    profile_b = dc_b.profile

    assert profile_a.owner_id == "dc-a"
    assert profile_b.owner_id == "dc-b"
    assert profile_a.profile_template == "M"
    assert profile_b.profile_template == "M"
    assert profile_a.profile_name != profile_b.profile_name
    assert profile_a is not profile_b


def test_pod_profile_is_resolved_per_pod_instance() -> None:
    parent = _build_pod_parent(dc_id="dc-1", dc_name="DC1")
    pod_a = PodModel(
        id="pod-a",
        name="POD-A",
        index=1,
        deployment_type="mixed",
        layout="S_MIXED",
        parent=parent,
    )
    pod_b = PodModel(
        id="pod-b",
        name="POD-B",
        index=2,
        deployment_type="mixed",
        layout="S_MIXED",
        parent=parent,
    )

    profile_a = pod_a.profile
    profile_b = pod_b.profile

    assert profile_a.owner_id == "pod-a"
    assert profile_b.owner_id == "pod-b"
    assert profile_a.profile_template == "S_MIXED"
    assert profile_b.profile_template == "S_MIXED"
    assert profile_a.profile_name != profile_b.profile_name
    assert profile_a is not profile_b


def test_pod_profile_suite_container_and_row_slots() -> None:
    parent = _build_pod_parent(dc_id="dc-2", dc_name="DC2")
    pod_mixed = PodModel(
        id="pod-mixed",
        name="POD-MIXED",
        index=1,
        deployment_type="mixed",
        layout="S_MIXED",
        parent=parent,
    )
    pod_middle = PodModel(
        id="pod-middle",
        name="POD-MIDDLE",
        index=2,
        deployment_type="middle_rack",
        layout="S_MIDDLE",
        parent=parent,
    )

    mixed_profile = pod_mixed.profile
    middle_profile = pod_middle.profile

    assert mixed_profile.suite_container_mode is True
    assert mixed_profile.oversubscription == STANDARD_PROFILE_ASSUMPTIONS.oversubscription
    assert mixed_profile.homing == STANDARD_PROFILE_ASSUMPTIONS.homing
    assert mixed_profile.row_dependent_rack_slots_per_row == mixed_profile.compute_racks_per_row
    assert middle_profile.row_dependent_rack_slots_per_row == 0


def test_hard_enforced_layouts_match_spine_port_budget() -> None:
    for layout_name in ("S_TOR", "S_MIXED", "M_MIXED", "L_MIXED"):
        layout = PodLayout.from_name(layout_name)
        assert layout.enforce_compute_racks_from_spine_budget is True
        assert layout.computed_max_compute_racks_per_row is not None
        assert layout.compute_racks_per_row == layout.computed_max_compute_racks_per_row
