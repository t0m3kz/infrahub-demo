"""transforms/change/servicenow.py and the ChangeRequest schema: the ServiceNow change_request body of a request."""

from __future__ import annotations

import asyncio
from typing import Any

import yaml

from tests.integration.demo_data import ROOT, schema
from tests.unit.query_shape_helpers import query_tree, unselected
from transforms.change.servicenow import ChangeRequestServiceNow

QUERY = ROOT / "queries" / "change" / "change_request_servicenow.gql"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _row(**values: Any) -> dict[str, Any]:
    """A raw row of change_request_servicenow.gql."""
    fields: dict[str, Any] = {
        "name": "CHG-leaf change",
        "external_id": None,
        "short_description": "leaf change: 0 added, 1 updated, 0 removed objects; 2 device configs",
        "description": "Updated:\n  DcimPhysicalDevice (1): leaf-01",
        "justification": "Capacity.",
        "change_type": "normal",
        "risk": "high",
        "risk_override": None,
        "impact": 2,
        "implementation_plan": "1. Deploy leaf-01",
        "backout_plan": "Revoking the approval rolls every device back automatically.",
        "test_plan": "Deployment verification",
        "communication_plan": "Notify the affected customers: C005",
        "conflicts": None,
        "planned_start": "2026-11-12T21:00:00+00:00",
        "planned_end": "2026-11-12T21:30:00Z",
    }
    fields.update(values)
    return {
        "id": "cr1",
        **{name: _v(value) for name, value in fields.items()},
        "proposed_change": {"node": {"id": "pc1"}},
        "affected_devices": _edges({"id": "d2", "name": _v("leaf-02")}, {"id": "d1", "name": _v("leaf-01")}),
        "affected_customers": _edges({"id": "c5", "name": _v("C005")}),
    }


def _render(row: dict[str, Any] | None) -> dict[str, Any]:
    transform: Any = ChangeRequestServiceNow.__new__(ChangeRequestServiceNow)
    return asyncio.run(transform.transform({"ChangeRequest": _edges(*([row] if row else []))}))


def test_the_payload_uses_the_servicenow_field_names() -> None:
    """Every field of the brief, ServiceNow dates in UTC, the devices as CIs, the PC as correlation."""
    payload = _render(_row())

    assert payload == {
        "number": None,
        "short_description": "leaf change: 0 added, 1 updated, 0 removed objects; 2 device configs",
        "description": "Updated:\n  DcimPhysicalDevice (1): leaf-01",
        "justification": "Capacity.",
        "type": "normal",
        "risk": "high",
        "impact": 2,
        "category": "Network",
        "implementation_plan": "1. Deploy leaf-01",
        "backout_plan": "Revoking the approval rolls every device back automatically.",
        "test_plan": "Deployment verification",
        "start_date": "2026-11-12 21:00:00",
        "end_date": "2026-11-12 21:30:00",
        "cmdb_ci": ["leaf-01", "leaf-02"],
        "correlation_id": "pc1",
        "correlation_display": "CHG-leaf change",
        "u_affected_customers": ["C005"],
        "u_conflicts": None,
    }


def test_the_risk_is_the_effective_one_and_the_number_is_the_external_id() -> None:
    """An author's override is sent; once filed, the CHG number is."""
    payload = _render(_row(risk_override="moderate", external_id="CHG0030012", planned_start=None))
    assert payload["risk"] == "moderate"
    assert payload["number"] == "CHG0030012"
    assert payload["start_date"] is None


def test_no_request_renders_an_empty_payload() -> None:
    """An unknown name gives {}."""
    assert _render(None) == {}


def test_the_fixture_emits_only_fields_the_query_selects() -> None:
    """The raw row stands for the registered query."""
    assert unselected({"ChangeRequest": _edges(_row())}, query_tree(QUERY)) == []


def test_the_transform_is_registered_and_not_an_artifact() -> None:
    """A python transform rendered on demand by name; no artifact definition uses it."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    transforms = {t["name"]: t for t in config["python_transforms"]}
    assert transforms["change_request_servicenow"]["class_name"] == "ChangeRequestServiceNow"
    assert "change_request_servicenow" not in {a["transformation"] for a in config["artifact_definitions"]}
    assert ChangeRequestServiceNow.query in {q["name"] for q in config["queries"]}


def test_the_schema_has_unique_weights_name_first_and_the_author_fields() -> None:
    """Weights unique and explicit, name first, the HFID on the name, the author and derived fields declared."""
    kind = schema()["ChangeRequest"]
    fields = {**kind.attributes, **kind.relationships}
    weights = [field["order_weight"] for field in fields.values()]
    assert len(weights) == len(set(weights))
    assert min(fields, key=lambda name: fields[name]["order_weight"]) == "name"
    assert kind.human_friendly_id == ("name__value",)
    assert {"justification", "risk_override", "risk_override_reason", "external_id", "requested_by"} <= set(fields)
    assert kind.attributes["impact"]["parameters"] == {"min_value": 1, "max_value": 3}
    assert kind.relationships["proposed_change"]["optional"] is False
