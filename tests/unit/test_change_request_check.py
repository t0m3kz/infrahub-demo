"""checks/change_request.py: the open PC of the branch has exactly one complete and current change request (CR1-CR5)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml

from checks.change_request import ARTIFACT_FINGERPRINT_KEY, CheckChangeRequest
from tests.integration.demo_data import ROOT
from tests.unit.query_shape_helpers import query_tree, unselected
from utils.change_rules import artifact_fingerprint, parse_artifact_diff

QUERY = ROOT / "queries" / "change" / "change_request_validation.gql"
BRANCH = "feat/leaf"
FINGERPRINT = "f" * 64


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _pc(pc_id: str = "pc1", branch: str = BRANCH, description: str | None = "Moves a leaf.") -> dict[str, Any]:
    return {"id": pc_id, "name": _v(f"pc {pc_id}"), "description": _v(description), "source_branch": _v(branch)}


def _request(pc_id: str = "pc1", **values: Any) -> dict[str, Any]:
    """A complete, current, low-risk normal request unless overridden."""
    fields: dict[str, Any] = {
        "short_description": "leaf change: 1 updated",
        "implementation_plan": "1. Deploy leaf",
        "backout_plan": "back",
        "test_plan": "check",
        "justification": None,
        "change_type": "normal",
        "risk": "low",
        "risk_override": None,
        "risk_override_reason": None,
        "planned_start": None,
        "planned_end": None,
        "artifact_fingerprint": FINGERPRINT,
    }
    fields.update(values)
    return {
        "id": f"cr-{pc_id}",
        "name": _v(f"CHG-{pc_id}"),
        **{name: _v(value) for name, value in fields.items()},
        "proposed_change": {"node": {"id": pc_id}},
    }


def _logs(
    pcs: list[dict[str, Any]], requests: list[dict[str, Any]], *, fingerprint: str | None = FINGERPRINT, pc_id: str = ""
) -> list[dict[str, Any]]:
    check = cast(Any, CheckChangeRequest.__new__(CheckChangeRequest))
    check.logs = []
    check.output = None
    check.branch = BRANCH
    check.initializer = MagicMock(proposed_change_id=pc_id)
    data: dict[str, Any] = {"CoreProposedChange": _edges(*pcs), "ChangeRequest": _edges(*requests)}
    if fingerprint is not None:
        data[ARTIFACT_FINGERPRINT_KEY] = fingerprint
    check.validate(data)
    return check.logs


def _errors(logs: list[dict[str, Any]]) -> list[str]:
    return [log["message"] for log in logs if log["level"] == "ERROR"]


def _warnings(logs: list[dict[str, Any]]) -> list[str]:
    return [log["message"] for log in logs if log["level"] == "INFO" and log["message"].startswith("WARNING")]


def test_a_complete_current_request_passes() -> None:
    """Nothing to report."""
    assert _logs([_pc()], [_request()]) == []


def test_a_branch_with_no_open_pc_reports_nothing() -> None:
    """The open PCs are of other branches (a canceled, merged or closed one is not in the query at all)."""
    assert _logs([_pc(branch="feat/other")], []) == []


@pytest.mark.parametrize("count", [0, 2])
def test_cr1_the_pc_needs_exactly_one_request(count: int) -> None:
    """None or two requests naming the PC is an ERROR; a request of another PC does not count."""
    requests = [_request(), {**_request(), "id": "cr-dup", "name": _v("CHG-dup")}][:count] + [_request("pc9")]
    errors = _errors(_logs([_pc()], requests))
    assert len(errors) == 1
    assert errors[0].startswith(f"CR1 proposed change pc pc1: {count} change requests")


def test_cr1_the_pc_that_started_the_check_wins_over_the_branch_match() -> None:
    """With an initializer PC id, that PC is judged (here it has no request)."""
    errors = _errors(_logs([_pc(), _pc("pc2", branch="feat/other")], [_request()], pc_id="pc2"))
    assert errors and errors[0].startswith("CR1 proposed change pc pc2")


def test_cr2_empty_plans_are_errors() -> None:
    """Each empty derived text is named."""
    errors = _errors(_logs([_pc()], [_request(implementation_plan="", test_plan="  ")]))
    assert errors == ["CR2 CHG-pc1: implementation_plan, test_plan empty: re-run add_change_request"]


def test_cr2_a_justification_or_a_pc_description_is_needed() -> None:
    """No justification and no PC description is an ERROR; either one is enough."""
    assert _errors(_logs([_pc(description=None)], [_request()])) == [
        "CR2 CHG-pc1: no justification and the proposed change has no description"
    ]
    assert _logs([_pc(description=None)], [_request(justification="Capacity.")]) == []


@pytest.mark.parametrize(
    ("values", "failing"),
    [
        ({"risk": "high"}, True),
        ({"change_type": "emergency"}, True),
        ({"risk": "low", "risk_override": "high"}, True),
        ({"risk": "high", "planned_start": "2026-11-12T21:00:00Z", "planned_end": "2026-11-12T22:00:00Z"}, False),
        ({"risk": "moderate"}, False),
    ],
    ids=["high", "emergency", "overridden-high", "with-window", "moderate"],
)
def test_cr3_a_high_risk_or_emergency_change_needs_a_window(values: dict[str, Any], failing: bool) -> None:
    """The effective risk counts: an override to high needs the window too."""
    errors = [e for e in _errors(_logs([_pc()], [_request(**values)])) if e.startswith("CR3")]
    assert bool(errors) is failing


def test_cr4_a_lowered_risk_needs_a_reason() -> None:
    """high -> low without a reason is an ERROR (and the low effective risk needs no window); with one it passes; a
    raised override needs none."""
    errors = _errors(_logs([_pc()], [_request(risk="high", risk_override="low")]))
    assert errors == ["CR4 CHG-pc1: risk lowered from high to low without a reason"]
    assert _logs([_pc()], [_request(risk="high", risk_override="low", risk_override_reason="Lab-proven.")]) == []
    assert not [e for e in _errors(_logs([_pc()], [_request(risk="low", risk_override="moderate")])) if "CR4" in e]


def test_cr5_a_stale_fingerprint_is_a_warning() -> None:
    """The artifacts changed after the derivation: a WARNING, not an error; no current fingerprint skips the rule."""
    logs = _logs([_pc()], [_request(artifact_fingerprint="old")])
    assert _errors(logs) == []
    assert _warnings(logs) == [
        "WARNING: CR5 CHG-pc1: the artifacts changed since it was derived: re-run add_change_request"
    ]
    assert _logs([_pc()], [_request(artifact_fingerprint="old")], fingerprint=None) == []


def test_collect_data_adds_the_fingerprint_of_the_current_artifact_diff() -> None:
    """The REST artifact diff is read on the check's branch and hashed with the generator's function."""
    payload = {
        "a1": {
            "id": "a1",
            "display_label": "leaf-01 - leaf",
            "action": "updated",
            "target": {"id": "d1", "kind": "DcimPhysicalDevice", "display_label": "leaf-01"},
            "item_new": {"storage_id": "s", "checksum": "c"},
            "item_previous": None,
        }
    }
    client = MagicMock(address="http://infrahub:8000")

    async def query_gql_query(**kwargs: Any) -> dict[str, Any]:
        return {"data": {"CoreProposedChange": _edges(), "ChangeRequest": _edges()}}

    async def get(url: str) -> Any:
        client.url = url
        return MagicMock(json=MagicMock(return_value=payload))

    client.query_gql_query = query_gql_query
    client._get = get
    check = CheckChangeRequest(branch=BRANCH, client=client)

    data = asyncio.run(check.collect_data())

    assert data[ARTIFACT_FINGERPRINT_KEY] == artifact_fingerprint(parse_artifact_diff(payload))
    assert client.url == "http://infrahub:8000/api/diff/artifacts?branch=feat%2Fleaf"


def test_the_fixtures_emit_only_fields_the_query_selects() -> None:
    """The raw rows stand for change_request_validation.gql."""
    data = {"CoreProposedChange": _edges(_pc()), "ChangeRequest": _edges(_request())}
    assert unselected(data, query_tree(QUERY)) == []


def test_the_check_is_registered_targetless_with_its_query() -> None:
    """validate_change_request runs on every PC; its query is registered."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    checks = {c["name"]: c for c in config["check_definitions"]}
    assert checks["validate_change_request"]["class_name"] == "CheckChangeRequest"
    assert "targets" not in checks["validate_change_request"]
    assert CheckChangeRequest.query in {q["name"] for q in config["queries"]}
