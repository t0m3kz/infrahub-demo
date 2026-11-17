"""tasks.py: register_repo loads the event actions only once the repository is in sync and its generators exist."""

from __future__ import annotations

from typing import Any

import pytest

import tasks


def _answer(status: str | None, definitions: int) -> dict[str, Any]:
    """The preflight query result: one repository with ``status`` (none when None) and ``definitions`` generators."""
    repos = [] if status is None else [{"node": {"name": {"value": "test"}, "sync_status": {"value": status}}}]
    return {
        "CoreGenericRepository": {"edges": repos},
        "CoreGeneratorDefinition": {"edges": [{"node": {"name": {"value": f"g{i}"}}} for i in range(definitions)]},
        "TopologyTemplateDC": {"count": 0},
    }


class _Client:
    """Answers the preflight query with the next result of ``answers`` (the last one repeats)."""

    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = answers
        self.calls = 0

    def execute_graphql(self, query: str) -> dict[str, Any]:  # noqa: ARG002 - one query
        """The next answer."""
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        return answer


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait polls without sleeping."""
    monkeypatch.setattr(tasks.time, "sleep", lambda _seconds: None)


def test_the_wait_ends_when_the_repository_is_in_sync_with_its_generators() -> None:
    """A sync that is still running (no generator yet) is waited out; in-sync with definitions ends the wait."""
    client = _Client([_answer("syncing", 0), _answer("in-sync", 0), _answer("in-sync", 36)])

    assert tasks._wait_for_repository_sync(client) is True  # ty: ignore[invalid-argument-type]
    assert client.calls == 3


@pytest.mark.parametrize("status", ["error-import", "sync-error"])
def test_a_failed_sync_stops_the_wait(status: str) -> None:
    """An error status is reported at once, so the event actions are not loaded against missing generators."""
    client = _Client([_answer("syncing", 0), _answer(status, 0)])

    assert tasks._wait_for_repository_sync(client) is False  # ty: ignore[invalid-argument-type]
    assert client.calls == 2


def test_no_repository_or_a_timeout_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no repository registered the wait runs into its timeout and says so."""
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(tasks.time, "monotonic", lambda: next(clock))

    assert tasks._wait_for_repository_sync(_Client([_answer(None, 0)]), timeout=500) is False  # ty: ignore[invalid-argument-type]
