"""Unit tests for RackFabricModel (generators/model/rack_model.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.model.pod_profile import spine_downlink_layout
from generators.model.rack_model import RackFabricModel, role_compatibility_errors
from tests.unit.fabric_model_helpers import cleaned
from tests.unit.simulators.fabric_designs import pod_design

_TEMPLATE = {"id": "tpl-1", "template_kind": "TemplateDcimPhysicalDevice"}


def _entry(role: str, quantity: int = 2) -> dict[str, Any]:
    return {"role": role, "quantity": quantity, "template": _TEMPLATE}


def _compile(design: str = "S_MIXED", **overrides: Any) -> RackFabricModel:
    """Compile a rack through the raw GraphQL shape and clean_data(), as the generator receives it."""
    node: dict[str, Any] = {
        "id": "rack-1",
        "name": "RACK-1",
        "index": 1,
        "rack_type": "network",
        "row_index": 1,
        "deployment": {"id": "pod-1", "design": pod_design(design)},
        "leafs": [_entry("leaf")],
        "tors": [],
        "l2_leafs": [],
        "access_leafs": [],
        **overrides,
    }
    return RackFabricModel.compile(cleaned("LocationRack", node))


def test_identity_and_deployment_type_come_from_the_payload() -> None:
    model = _compile(index=2, row_index=2)

    assert (model.rack_name, model.rack_index, model.row_index, model.rack_type) == ("RACK-1", 2, 2, "network")
    assert model.deployment_type == "mixed"


def test_present_roles_are_the_roles_with_a_positive_quantity() -> None:
    model = _compile(leafs=[_entry("leaf")], tors=[_entry("tor", 0)], access_leafs=[_entry("access-leaf")])

    assert model.present_roles == {"leaf", "access_leaf"}


def test_a_rack_without_any_switch_template_has_no_roles() -> None:
    assert _compile(leafs=[]).present_roles == frozenset()


def test_compatible_roles_have_no_errors() -> None:
    model = _compile(leafs=[_entry("leaf")], l2_leafs=[_entry("l2-leaf")])

    assert role_compatibility_errors(model.present_roles, model.deployment_type) == []
    assert model.capacity_errors == ()


def test_a_tor_in_a_middle_rack_pod_is_a_role_error() -> None:
    model = _compile("S_MIDDLE", leafs=[], tors=[_entry("tor")])

    errors = role_compatibility_errors(model.present_roles, model.deployment_type)
    assert len(errors) == 1
    assert "['tor']" in errors[0]
    assert "deployment_type='middle_rack'" in errors[0]


def test_a_leaf_in_a_tor_pod_is_a_role_error() -> None:
    model = _compile("S_TOR", rack_type="compute", leafs=[_entry("leaf")])

    assert "['leaf']" in role_compatibility_errors(model.present_roles, model.deployment_type)[0]


def test_tor_and_access_leaf_exclude_each_other_in_any_pod() -> None:
    model = _compile(rack_type="compute", leafs=[], tors=[_entry("tor")], access_leafs=[_entry("access-leaf")])

    assert role_compatibility_errors(model.present_roles, model.deployment_type) == [
        "role(s) ['access_leaf', 'tor'] are mutually exclusive on the same rack"
    ]


def test_role_compatibility_errors_take_the_deployment_type_it_is_given() -> None:
    assert role_compatibility_errors({"tor"}, "tor") == []
    assert role_compatibility_errors({"leaf"}, "tor") != []
    assert role_compatibility_errors(set(), "mixed") == []


@pytest.mark.parametrize(
    ("design", "row_index", "expected"),
    [
        # 8 compute + 1 network rack positions per row: a mixed row numbers both
        ("S_MIXED", 1, 0),
        ("S_MIXED", 3, 18),
        ("S_TOR", 3, 16),
        ("S_MIDDLE", 3, 0),
    ],
)
def test_planned_previous_row_rack_slots_come_from_the_design_capacity(
    design: str, row_index: int, expected: int
) -> None:
    """Never from the racks that exist: a rack keeps its ports when a neighbour is added or removed."""
    assert _compile(design, row_index=row_index).planned_previous_row_rack_slots == expected


def test_a_row_past_the_design_is_a_capacity_error() -> None:
    model = _compile(row_index=3)

    assert model.capacity_errors == ("row_index=3 exceeds profile rows=2 for design=S_MIXED",)


def test_a_rack_index_past_the_row_slots_is_a_capacity_error() -> None:
    model = _compile(index=10)

    assert model.capacity_errors == ("rack index=10 exceeds per-row rack slots=9 (compute=8, network=1)",)


def test_more_leafs_than_a_network_rack_holds_is_a_capacity_error() -> None:
    model = _compile(leafs=[_entry("leaf", 3)])

    assert model.capacity_errors == ("leaf count=3 exceeds profile max_leafs_per_network_rack=2",)


def test_more_tors_than_a_compute_rack_holds_is_a_capacity_error() -> None:
    model = _compile(rack_type="compute", leafs=[], tors=[_entry("tor", 3)])

    assert model.capacity_errors == ("tor count=3 exceeds profile max_tors_per_compute_rack=2",)


def test_leafs_are_not_counted_against_a_compute_rack() -> None:
    """The leaf cap is a network rack's."""
    assert _compile(rack_type="compute", leafs=[_entry("leaf", 9)]).capacity_errors == ()


@pytest.mark.parametrize(("row_index", "expected"), [(1, 0), (3, 4), (5, 8)])
def test_mixed_leaf_offset_steps_by_row_and_leaf_count(row_index: int, expected: int) -> None:
    model = _compile(row_index=row_index, index=1)

    assert model.cabling_offset(device_count=2, device_type="leaf") == expected


def test_middle_rack_tor_offset_is_zero() -> None:
    """ToRs of a middle-rack pod cable to the leafs of their own rack."""
    model = _compile("S_MIDDLE")

    assert model.cabling_offset(device_count=2, device_type="tor", racks_in_previous_rows=5) == 0


def test_tor_offset_starts_at_the_tor_block_and_steps_by_rack_and_previous_rows() -> None:
    design = pod_design("S_MIXED")
    block = spine_downlink_layout(design)["tor"]
    model = _compile(rack_type="compute", index=3, row_index=2, leafs=[])

    offset = model.cabling_offset(device_count=2, device_type="tor", racks_in_previous_rows=9)

    assert model.tor_block_start == block
    assert offset == block + 9 * 2 + 2 * 2


def test_tor_pod_tor_block_starts_at_port_zero() -> None:
    model = _compile("S_TOR", rack_type="compute", index=1, leafs=[])

    assert model.cabling_offset(device_count=2, device_type="tor") == 0


def test_other_device_types_get_no_offset() -> None:
    assert _compile().cabling_offset(device_count=2, device_type="l2_leaf") == 0


@pytest.mark.parametrize("missing", ["name", "index", "rack_type", "row_index", "deployment"])
def test_a_payload_without_a_required_field_raises_key_error(missing: str) -> None:
    node = cleaned(
        "LocationRack",
        {
            "id": "rack-1",
            "name": "RACK-1",
            "index": 1,
            "rack_type": "network",
            "row_index": 1,
            "deployment": {"id": "pod-1", "design": pod_design("S_MIXED")},
        },
    )
    del node[missing]

    with pytest.raises(KeyError):
        RackFabricModel.compile(node)
