from __future__ import annotations

from typing import Any

import pytest

from generators.pod_config import POD_LAYOUTS, pod_deployment_type, pod_profile, resolve_pod_layout

# What the layout table used to store (and every pod was sized from) before the
# loopback pool size and the deployment type were derived from the layout's
# device mix.
_EXPECTED_LOOPBACK_HOST_BITS = {
    "S_MIDDLE": 4,
    "S_TOR": 6,
    "S_MIXED": 6,
    "S_BORDER_SPINE_POD": 4,
    "M_MIXED": 7,
    "M_MIDDLE": 5,
}
_EXPECTED_DEPLOYMENT_TYPE = {
    "S_MIDDLE": "middle_rack",
    "S_TOR": "tor",
    "S_MIXED": "mixed",
    "S_BORDER_SPINE_POD": "middle_rack",
    "M_MIXED": "mixed",
    "M_MIDDLE": "middle_rack",
}


def _spine_budget_compute_racks_per_row(entry: dict[str, Any]) -> int:
    """compute_racks_per_row a layout's spine/ToR port budget allows: every
    compute rack burns (ToRs per rack x uplinks per ToR) spine downlinks."""
    total_downlinks = entry["spine_downlink_ports_per_spine"] * entry["max_spines_per_pod"]
    links_per_compute_rack = entry["max_tors_per_compute_rack"] * entry["tor_uplinks_to_spine"]
    return (total_downlinks // links_per_compute_rack) // entry["rows"]


def test_pod_profile_resolves_layout_fields() -> None:
    profile = pod_profile("S_MIXED")

    assert profile["profile_template"] == "S_MIXED"
    assert profile["deployment_type"] == "mixed"
    assert profile["rows"] == resolve_pod_layout("S_MIXED")["rows"]
    assert profile["compute_racks_per_row"] == resolve_pod_layout("S_MIXED")["compute_racks_per_row"]


def test_pod_profile_row_dependent_rack_slots() -> None:
    mixed_profile = pod_profile("S_MIXED")
    middle_profile = pod_profile("S_MIDDLE")

    assert mixed_profile["row_dependent_rack_slots_per_row"] == mixed_profile["compute_racks_per_row"]
    assert middle_profile["row_dependent_rack_slots_per_row"] == 0


def test_every_layout_is_covered_by_the_expected_tables() -> None:
    """A new layout must add its expected derived values here."""
    assert set(POD_LAYOUTS) == set(_EXPECTED_LOOPBACK_HOST_BITS) == set(_EXPECTED_DEPLOYMENT_TYPE)


@pytest.mark.parametrize("layout_name", sorted(POD_LAYOUTS))
def test_derived_loopback_host_bits_and_deployment_type(layout_name: str) -> None:
    layout = resolve_pod_layout(layout_name)

    assert layout["name"] == layout_name
    assert layout["loopback_host_bits"] == _EXPECTED_LOOPBACK_HOST_BITS[layout_name]
    assert layout["deployment_type"] == pod_deployment_type(layout_name) == _EXPECTED_DEPLOYMENT_TYPE[layout_name]


@pytest.mark.parametrize("layout_name", sorted(POD_LAYOUTS))
def test_layouts_with_tors_declare_a_spine_budget_matching_compute_racks_per_row(layout_name: str) -> None:
    """compute_racks_per_row is hard-enforced from the spine/ToR port budget for
    every layout that has ToRs (the others declare no budget at all)."""
    entry = POD_LAYOUTS[layout_name]
    has_budget = "spine_downlink_ports_per_spine" in entry or "tor_uplinks_to_spine" in entry

    assert has_budget == (entry["max_tors_per_compute_rack"] > 0)
    if has_budget:
        assert entry["compute_racks_per_row"] == _spine_budget_compute_racks_per_row(entry)
