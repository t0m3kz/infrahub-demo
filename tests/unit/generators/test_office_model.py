"""Unit tests for OfficeFabricModel (generators/model/office_model.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.model.office_model import (
    OFFICE_VALID_FABRIC_ROLES,
    PHYSICAL_TEMPLATE_KIND,
    OfficeFabricModel,
)
from tests.unit.fabric_model_helpers import cleaned
from tests.unit.office_fabric_generator_helpers import (
    _ACCESS_ENTRY,
    _ACCESS_TEMPLATE,
    _DISTRIBUTION_ENTRY,
    _DISTRIBUTION_TEMPLATE,
    _VIRTUAL_TEMPLATE,
)
from tests.unit.office_fabric_generator_helpers import EDGE_TEMPLATE as _EDGE_TEMPLATE


def _compile(*entries: dict[str, Any], **overrides: Any) -> OfficeFabricModel:
    """Compile an office through the raw GraphQL shape and clean_data(), as the generator receives it."""
    node: dict[str, Any] = {
        "id": "office-1",
        "name": "C005-OFFICE",
        "naming_convention": "standard",
        "fabric_templates": list(entries),
        **overrides,
    }
    return OfficeFabricModel.compile(cleaned("TopologyCustomerOffice", node))


def test_identity_comes_from_the_payload() -> None:
    model = _compile(_DISTRIBUTION_ENTRY)

    assert (model.office_id, model.office_name, model.fabric_name) == ("office-1", "C005-OFFICE", "c005-office")
    assert model.naming_convention == "standard"


def test_naming_convention_is_lower_cased_and_defaults_to_standard() -> None:
    assert _compile(naming_convention="Hierarchical").naming_convention == "hierarchical"
    assert _compile(naming_convention=None).naming_convention == "standard"


def test_valid_entries_are_kept_in_order_without_warnings() -> None:
    model = _compile(_DISTRIBUTION_ENTRY, _ACCESS_ENTRY)

    assert [entry["role"] for entry in model.usable_templates] == ["distribution-switch", "access-switch"]
    assert model.usable_templates[0]["template"]["interfaces"][0] == {
        "name": "HundredGigE1/0/1",
        "role": "downlink",
    }
    assert model.warnings == ()


def test_an_office_without_fabric_templates_has_none_usable_and_no_warning() -> None:
    model = _compile()

    assert model.usable_templates == ()
    assert model.warnings == ()


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"role": "leaf", "quantity": 2, "template": _ACCESS_TEMPLATE}, "role='leaf' is not valid"),
        ({"role": "border-leaf", "quantity": 2, "template": _DISTRIBUTION_TEMPLATE}, "role='border-leaf'"),
        ({"role": "access-switch", "quantity": 0, "template": _ACCESS_TEMPLATE}, "has quantity 0"),
        ({"role": "access-switch", "quantity": 2, "template": None}, "has no template"),
        ({"role": "access-switch", "quantity": 2, "template": _VIRTUAL_TEMPLATE}, "TemplateDcimVirtualDevice"),
    ],
)
def test_an_unusable_entry_is_dropped_with_one_warning(entry: dict[str, Any], fragment: str) -> None:
    model = _compile(entry)

    assert model.usable_templates == ()
    assert len(model.warnings) == 1
    assert model.warnings[0].startswith("Office c005-office: ")
    assert fragment in model.warnings[0]


def test_one_bad_entry_does_not_drop_the_good_ones() -> None:
    model = _compile({"role": "leaf", "quantity": 2, "template": _ACCESS_TEMPLATE}, _DISTRIBUTION_ENTRY)

    assert [entry["role"] for entry in model.usable_templates] == ["distribution-switch"]
    assert len(model.warnings) == 1


def test_a_second_entry_for_a_role_is_dropped() -> None:
    """Device names derive from the role alone, so a second entry would rename the first one's devices."""
    second = {"role": "access-switch", "quantity": 4, "template": _ACCESS_TEMPLATE}

    model = _compile(_ACCESS_ENTRY, second)

    assert [entry["quantity"] for entry in model.usable_templates] == [2]
    assert "second fabric_templates entry" in model.warnings[0]


def test_the_valid_roles_are_the_two_campus_roles_the_edge_and_the_template_kind_is_physical() -> None:
    assert OFFICE_VALID_FABRIC_ROLES == {"access-switch", "distribution-switch", "sdwan-edge"}
    assert PHYSICAL_TEMPLATE_KIND == "TemplateDcimPhysicalDevice"


@pytest.mark.parametrize("missing", ["id", "name"])
def test_a_payload_without_a_required_field_raises_key_error(missing: str) -> None:
    node = cleaned("TopologyCustomerOffice", {"id": "office-1", "name": "OFFICE", "fabric_templates": []})
    del node[missing]

    with pytest.raises(KeyError):
        OfficeFabricModel.compile(node)


def test_the_sdwan_edge_entry_is_the_edge_template_and_not_a_switch_entry() -> None:
    edge = {"role": "sdwan-edge", "quantity": 1, "template": _EDGE_TEMPLATE}

    model = _compile(_DISTRIBUTION_ENTRY, edge)

    assert [entry["role"] for entry in model.usable_templates] == ["distribution-switch"]
    assert model.edge_template is not None and model.edge_template["template_name"] == "VCE-610_EDGE"
    assert model.warnings == () and model.errors == ()


def test_an_office_without_an_edge_entry_has_no_edge_template() -> None:
    assert _compile(_ACCESS_ENTRY).edge_template is None


def test_more_than_one_edge_is_an_error_and_zero_is_a_warning() -> None:
    many = _compile({"role": "sdwan-edge", "quantity": 2, "template": _EDGE_TEMPLATE})
    none = _compile({"role": "sdwan-edge", "quantity": 0, "template": _EDGE_TEMPLATE})

    assert many.edge_template is None and "one SD-WAN edge" in many.errors[0]
    assert none.edge_template is None and none.errors == () and "quantity 0" in none.warnings[0]


def test_an_edge_on_a_virtual_template_is_dropped() -> None:
    model = _compile({"role": "sdwan-edge", "quantity": 1, "template": _VIRTUAL_TEMPLATE})

    assert model.edge_template is None and len(model.warnings) == 1
