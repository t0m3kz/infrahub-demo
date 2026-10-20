"""generators/pod_config.py: a pod's deployment type and rack-grid profile, derived from its design.

The designs are the bootstrap rows (data/bootstrap/14b_fabric_templates.yml).
"""

from __future__ import annotations

import pytest

from generators.pod_config import pod_deployment_type, pod_profile
from tests.unit.simulators.fabric_designs import POD_DESIGN_NAMES, custom_pod_design, pod_design

_EXPECTED_DEPLOYMENT_TYPE = {
    "S_MIDDLE": "middle_rack",
    "S_TOR": "tor",
    "S_MIXED": "mixed",
    "S_BORDER_SPINE_POD": "middle_rack",
    "M_MIXED": "mixed",
    "M_MIDDLE": "middle_rack",
}


def test_every_design_is_covered_by_the_expected_table() -> None:
    """A new pod design must add its expected deployment type here."""
    assert set(POD_DESIGN_NAMES) == set(_EXPECTED_DEPLOYMENT_TYPE)


@pytest.mark.parametrize("name", POD_DESIGN_NAMES)
def test_deployment_type_is_derived_from_the_device_mix(name: str) -> None:
    assert pod_deployment_type(pod_design(name)) == _EXPECTED_DEPLOYMENT_TYPE[name]


@pytest.mark.parametrize(
    ("leafs", "tors", "expected"),
    [(4, 0, "middle_rack"), (0, 0, "middle_rack"), (0, 2, "tor"), (2, 2, "mixed")],
)
def test_deployment_type_rule(leafs: int, tors: int, expected: str) -> None:
    """No ToRs -> middle_rack; ToRs and no leafs -> tor; ToRs and leafs -> mixed."""
    design = custom_pod_design(max_leafs_per_network_rack=leafs, max_tors_per_compute_rack=tors)

    assert pod_deployment_type(design) == expected


def test_pod_profile_carries_the_design_rack_grid() -> None:
    design = pod_design("S_MIXED")
    profile = pod_profile(design)

    assert profile["profile_template"] == "S_MIXED"
    assert profile["deployment_type"] == "mixed"
    for field in (
        "max_rows",
        "max_compute_racks_per_row",
        "max_network_racks_per_row",
        "max_leafs_per_network_rack",
        "max_tors_per_compute_rack",
    ):
        assert profile[field] == design[field], field


def test_pod_profile_row_dependent_rack_slots() -> None:
    mixed_profile = pod_profile(pod_design("S_MIXED"))
    middle_profile = pod_profile(pod_design("S_MIDDLE"))

    assert mixed_profile["row_dependent_rack_slots_per_row"] == mixed_profile["max_compute_racks_per_row"]
    assert middle_profile["row_dependent_rack_slots_per_row"] == 0


@pytest.mark.parametrize("name", POD_DESIGN_NAMES)
def test_designs_with_tors_declare_a_spine_budget_matching_compute_racks_per_row(name: str) -> None:
    """max_compute_racks_per_row is hard-enforced from the spine/ToR port budget for every
    design that has ToRs (the others declare no budget at all)."""
    design = pod_design(name)
    has_budget = design.get("spine_downlink_ports_per_spine") is not None or (
        design.get("tor_uplinks_to_spine") is not None
    )

    assert has_budget == (design["max_tors_per_compute_rack"] > 0)
    if has_budget:
        total_downlinks = design["spine_downlink_ports_per_spine"] * design["max_spines_per_pod"]
        links_per_compute_rack = design["max_tors_per_compute_rack"] * design["tor_uplinks_to_spine"]
        assert design["max_compute_racks_per_row"] == (total_downlinks // links_per_compute_rack) // design["max_rows"]
