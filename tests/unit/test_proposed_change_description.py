"""tests/integration/utils/proposed_change.py: a kick's proposed change carries a description beside its name."""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.integration.utils import proposed_change


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A client whose create mutation answers with a PC id; the waits and the diff update are skipped."""
    monkeypatch.setattr(proposed_change, "wait_for_quiet", lambda *args, **kwargs: None)
    monkeypatch.setattr(proposed_change, "update_diff", lambda *args, **kwargs: "diff-feature")
    mock = MagicMock()
    mock.execute_graphql.return_value = {"CoreProposedChangeCreate": {"object": {"id": "pc-1"}}}
    return mock


def _mutation(client: MagicMock) -> str:
    """The text of the create mutation the client was asked to run."""
    query: Any = client.execute_graphql.call_args.kwargs["query"]
    return str(query)


def test_the_description_is_sent_with_the_name(client: MagicMock) -> None:
    """A given description is an attribute of the created proposed change."""
    pc_id = proposed_change.create_proposed_change(
        client, "dc6-suite-pod", "dc6-suite-pod", description="Kick `dc6-suite`, stage `pod`."
    )

    assert pc_id == "pc-1"
    mutation = _mutation(client)
    assert "dc6-suite-pod" in mutation
    assert "description" in mutation
    assert "Kick `dc6-suite`, stage `pod`." in mutation


def test_without_a_description_the_attribute_is_not_sent(client: MagicMock) -> None:
    """The other callers (the integration tests) keep creating a proposed change with a name only."""
    proposed_change.create_proposed_change(client, "feature", "feature")

    assert "description" not in _mutation(client)


def test_a_given_change_number_is_kept() -> None:
    """The number the user passes is the number written on the proposed change."""
    assert proposed_change.change_number("CHG0012345") == "CHG0012345"


def test_without_a_change_number_it_is_chg_and_six_digits() -> None:
    """No number given: CHG and six digits, zero-padded, a new one on each call."""
    numbers = {proposed_change.change_number() for _ in range(50)}

    assert all(re.fullmatch(r"CHG\d{6}", number) for number in numbers)
    assert len(numbers) > 1
