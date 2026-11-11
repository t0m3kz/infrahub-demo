"""The merge retry of the integration harness runs the checks again for a validator that is not green.

The platform refuses a merge while a validator has not concluded or says failure; a repeated merge changes nothing, so
``_rerun_unfinished_validators`` runs the checks again (the GUI's "run checks"), but only when no check itself failed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.integration import workflow_helpers
from tests.integration.workflow_helpers import _fetch_failed_checks, _rerun_unfinished_validators


def _response(*validators: dict[str, Any]) -> dict[str, Any]:
    """A ``CoreProposedChange`` answer holding the given validators."""
    return {"CoreProposedChange": {"edges": [{"node": {"validations": {"edges": [{"node": v} for v in validators]}}}]}}


def _validator(label: str, state: str, conclusion: str, checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One validator node with its checks."""
    return {
        "label": {"value": label},
        "state": {"value": state},
        "conclusion": {"value": conclusion},
        "checks": {"edges": [{"node": check} for check in checks or []]},
    }


def _check(conclusion: str, message: str = "boom") -> dict[str, Any]:
    """One check node."""
    return {
        "kind": {"value": "CoreStandardCheck"},
        "label": {"value": "a check"},
        "conclusion": {"value": conclusion},
        "message": {"value": message},
    }


def _client(response: dict[str, Any]) -> MagicMock:
    """A sync client answering every GraphQL call with ``response``."""
    client = MagicMock()
    client.execute_graphql.return_value = response
    return client


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Skip the sleep and the wait for validations: only the decision is under test."""
    monkeypatch.setattr(workflow_helpers.time, "sleep", lambda _seconds: None)
    waiter = MagicMock()
    monkeypatch.setattr(workflow_helpers, "wait_for_validations", waiter)
    return waiter


def test_a_validator_that_never_concluded_is_reported_with_its_state() -> None:
    """A queued validator with no check under it is named, so the log says what blocked the merge."""
    client = _client(_response(_validator("Artifact Validator: x", "queued", "unknown")))
    assert _fetch_failed_checks(client, "pc") == [
        "[Artifact Validator: x] validator state=queued conclusion=unknown (0 check(s) under it)"
    ]


def test_the_checks_of_a_validator_that_is_not_green_are_listed_with_their_message() -> None:
    """A failing validator over a check that does not say failure still shows what that check says."""
    client = _client(
        _response(_validator("Check: validate_x", "completed", "failure", [_check("unknown", "rack R1 over the grid")]))
    )
    assert _fetch_failed_checks(client, "pc") == [
        "[Check: validate_x] validator state=completed conclusion=failure (1 check(s) under it)",
        "[Check: validate_x] check conclusion=unknown: rack R1 over the grid",
    ]


def test_a_green_validator_is_not_reported() -> None:
    """A completed validator with a success conclusion adds nothing."""
    client = _client(_response(_validator("Check: ok", "completed", "success", [_check("success")])))
    assert _fetch_failed_checks(client, "pc") == []


def test_the_checks_run_again_when_only_a_validator_is_not_green(_no_waiting: MagicMock) -> None:
    """Nothing failed under a failing validator: run the checks again and wait for them."""
    client = _client(_response(_validator("Artifact Validator: x", "completed", "failure", [_check("success")])))
    _rerun_unfinished_validators(client, "pc-1")
    mutation = client.execute_graphql.call_args_list[-1]
    assert "CoreProposedChangeRunCheck" in mutation.kwargs["query"]
    assert "check_type: ALL" in mutation.kwargs["query"]
    assert mutation.kwargs["variables"] == {"id": "pc-1"}
    _no_waiting.assert_called_once()


def test_a_failing_check_is_the_finding_and_the_checks_do_not_run_again(_no_waiting: MagicMock) -> None:
    """A check that really failed blocks the merge for a reason a rerun cannot change."""
    client = _client(_response(_validator("Check: bad", "completed", "failure", [_check("failure", "wrong data")])))
    _rerun_unfinished_validators(client, "pc-1")
    assert client.execute_graphql.call_count == 1  # the diagnostic read only
    _no_waiting.assert_not_called()


def test_nothing_is_rerun_when_every_validator_is_green(_no_waiting: MagicMock) -> None:
    """No validator blocks the merge: no call beyond the read."""
    client = _client(_response(_validator("Check: ok", "completed", "success")))
    _rerun_unfinished_validators(client, "pc-1")
    assert client.execute_graphql.call_count == 1
    _no_waiting.assert_not_called()
