from __future__ import annotations

from generators.pod_config import pod_profile, resolve_pod_layout


def test_pod_profile_resolves_layout_fields() -> None:
    profile = pod_profile(layout_name="S_MIXED", deployment_type="mixed")

    assert profile["profile_template"] == "S_MIXED"
    assert profile["deployment_type"] == "mixed"
    assert profile["rows"] == resolve_pod_layout("S_MIXED")["rows"]
    assert profile["compute_racks_per_row"] == resolve_pod_layout("S_MIXED")["compute_racks_per_row"]


def test_pod_profile_row_dependent_rack_slots() -> None:
    mixed_profile = pod_profile(layout_name="S_MIXED", deployment_type="mixed")
    middle_profile = pod_profile(layout_name="S_MIDDLE", deployment_type="middle_rack")

    assert mixed_profile["row_dependent_rack_slots_per_row"] == mixed_profile["compute_racks_per_row"]
    assert middle_profile["row_dependent_rack_slots_per_row"] == 0


def test_hard_enforced_layouts_match_spine_port_budget() -> None:
    for layout_name in ("S_TOR", "S_MIXED", "M_MIXED", "L_MIXED"):
        layout = resolve_pod_layout(layout_name)
        assert layout["enforce_compute_racks_from_spine_budget"] is True
        assert layout["compute_racks_per_row"] > 0
