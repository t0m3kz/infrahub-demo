"""tasks.py: the API token comes from the environment, and a task that needs it fails by name without it."""

from __future__ import annotations

import pytest
from invoke import Exit

import tasks


def test_the_token_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """A task gets the value of INFRAHUB_API_TOKEN, nothing is hardcoded."""
    monkeypatch.setenv("INFRAHUB_API_TOKEN", "abc-123")

    assert tasks._api_token() == "abc-123"


@pytest.mark.parametrize("value", [None, ""])
def test_a_missing_token_stops_the_task_with_a_message(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """An unset or empty token is an Exit that names the variable, never a None handed to the SDK."""
    if value is None:
        monkeypatch.delenv("INFRAHUB_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("INFRAHUB_API_TOKEN", value)

    with pytest.raises(Exit) as raised:
        tasks._api_token()

    assert "INFRAHUB_API_TOKEN is not set" in str(raised.value)
    assert raised.value.code == 1
