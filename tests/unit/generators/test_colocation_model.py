"""Unit tests for ColocationFabricModel (generators/model/colocation_model.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.model.colocation_model import (
    COLO_SERVICE_ROLES,
    COLO_TEMPLATE_KIND_APPLIANCE,
    COLO_TEMPLATE_KIND_PHYSICAL,
    COLO_VALID_FABRIC_ROLES,
    ColocationFabricModel,
)
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _EDGE_TEMPLATE_WITH_FIREWALL_PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
)
from tests.unit.fabric_model_helpers import cleaned
from tests.unit.simulators.fabric_designs import colo_metro_design, colo_zone_design


def _entry(role: str, quantity: int, template: dict[str, Any] | None) -> dict[str, Any]:
    return {"role": role, "quantity": quantity, "template": template}


def _compile(
    *entries: dict[str, Any],
    label: str = "zone",
    template_kind: str = COLO_TEMPLATE_KIND_PHYSICAL,
    **overrides: Any,
) -> ColocationFabricModel:
    """Compile a zone through the raw GraphQL shape and clean_data(), as the generator receives it."""
    node: dict[str, Any] = {
        "id": "zone-1",
        "name": "FR2",
        "parent": {"id": "metro-1", "name": "FR"},
        "fabric_templates": list(entries),
        **overrides,
    }
    return ColocationFabricModel.compile(
        cleaned("TopologyColocationZone", node), label=label, template_kind=template_kind
    )


def test_every_entry_of_a_zone_with_matching_ports_is_kept_without_warnings() -> None:
    entries = [
        _entry("edge", 2, _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS),
        _entry("internal-firewall", 2, _EDGE_TEMPLATE),
        _entry("load-balancer", 2, _EDGE_TEMPLATE),
    ]

    model = _compile(*entries)

    assert [entry["role"] for entry in model.usable_templates] == ["edge", "internal-firewall", "load-balancer"]
    assert model.warnings == ()
    assert model.fabric_name == "fr2"
    assert model.label == "zone"


def test_the_service_roles_are_valid_colocation_roles() -> None:
    assert COLO_SERVICE_ROLES < COLO_VALID_FABRIC_ROLES


def test_no_fabric_templates_is_nothing_to_do_without_a_warning() -> None:
    model = _compile()

    assert model.usable_templates == ()
    assert model.warnings == ()


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        (_entry("super-spine", 2, _EDGE_TEMPLATE), "role='super-spine' is not valid here"),
        (_entry("edge", 0, _EDGE_TEMPLATE), "has quantity 0"),
        (_entry("edge", 2, None), "has no template"),
        (_entry("edge", 2, _VIRTUAL_EDGE_TEMPLATE), "which a zone cannot host"),
    ],
)
def test_an_unusable_entry_is_dropped_with_one_warning(entry: dict[str, Any], fragment: str) -> None:
    model = _compile(entry)

    assert model.usable_templates == ()
    assert len(model.warnings) == 1
    assert model.warnings[0].startswith("Colocation zone fr2: ")
    assert fragment in model.warnings[0]


def test_a_virtual_colocation_hosts_appliances_and_refuses_physical_templates() -> None:
    kind = COLO_TEMPLATE_KIND_APPLIANCE

    kept = _compile(_entry("edge", 1, _VIRTUAL_EDGE_TEMPLATE), label="virtual colocation", template_kind=kind)
    refused = _compile(_entry("edge", 1, _EDGE_TEMPLATE), label="virtual colocation", template_kind=kind)

    assert len(kept.usable_templates) == 1
    assert refused.usable_templates == ()
    assert "which a virtual colocation cannot host" in refused.warnings[0]


def test_a_physical_service_entry_without_a_matching_edge_port_is_dropped() -> None:
    """The edge template has firewall ports only: a load-balancer would have nothing to cable to."""
    entries = [
        _entry("edge", 2, _EDGE_TEMPLATE_WITH_FIREWALL_PORTS),
        _entry("internal-firewall", 2, _EDGE_TEMPLATE),
        _entry("load-balancer", 2, _EDGE_TEMPLATE),
    ]

    model = _compile(*entries)

    assert [entry["role"] for entry in model.usable_templates] == ["edge", "internal-firewall"]
    assert len(model.warnings) == 1
    assert "'load-balancer'" in model.warnings[0]
    assert "available edge port roles: ['firewall']" in model.warnings[0]


def test_services_are_not_checked_without_a_physical_edge_entry() -> None:
    model = _compile(_entry("internal-firewall", 2, _EDGE_TEMPLATE))

    assert len(model.usable_templates) == 1
    assert model.warnings == ()


def test_entries_are_held_to_the_design_maximum_per_role() -> None:
    """EDGE allows two edges; a third edge entry is skipped whole, in template-id order."""
    first = {**_EDGE_TEMPLATE, "id": "tpl-a"}
    second = {**_EDGE_TEMPLATE, "id": "tpl-b"}

    model = _compile(_entry("edge", 2, first), _entry("edge", 1, second), design=colo_zone_design("EDGE"))

    assert [entry["template"]["id"] for entry in model.usable_templates] == ["tpl-a"]
    assert len(model.warnings) == 1
    assert "would bring the edge total to 3, past the max_edges=2 of design 'EDGE'" in model.warnings[0]


def test_without_a_design_nothing_bounds_the_entries() -> None:
    model = _compile(_entry("edge", 50, _EDGE_TEMPLATE))

    assert len(model.usable_templates) == 1
    assert model.warnings == ()


def test_a_design_outside_the_metro_design_warns_and_keeps_the_entries() -> None:
    design = {**colo_zone_design("EDGE"), "name": "ODD"}
    parent = {"id": "metro-1", "name": "FR", "design": colo_metro_design("S")}

    model = _compile(_entry("edge", 1, _EDGE_TEMPLATE), design=design, parent=parent)

    assert len(model.usable_templates) == 1
    assert len(model.warnings) == 1
    assert "design 'ODD' is not one of metro design 'S' zone_templates" in model.warnings[0]


def test_a_design_among_the_metro_zone_templates_does_not_warn() -> None:
    parent = {"id": "metro-1", "name": "FR", "design": colo_metro_design("S")}

    model = _compile(_entry("edge", 1, _EDGE_TEMPLATE), design=colo_zone_design("EDGE_FW_LB"), parent=parent)

    assert model.warnings == ()


def test_a_payload_without_a_name_raises_key_error() -> None:
    node = cleaned("TopologyColocationZone", {"id": "zone-1", "name": "FR2", "fabric_templates": []})
    del node["name"]

    with pytest.raises(KeyError):
        ColocationFabricModel.compile(node, label="zone", template_kind=COLO_TEMPLATE_KIND_PHYSICAL)
