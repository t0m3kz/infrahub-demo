"""generators/change/open_change_request.py: a completed artifact validator gives its PC a change request."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from generators.change.open_change_request import (
    OpenChangeRequestGenerator,
    change_request_comment,
    change_request_name,
)
from generators.logger import GeneratorError
from tests.integration.demo_data import ROOT

BRANCH = "feat/leaf"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def validator_row(
    state: str = "completed", pc_state: str = "open", branch: str | None = BRANCH, others: tuple[str, ...] = ()
) -> dict[str, Any]:
    """A raw CoreArtifactValidator row of open_change_request.gql; ``others`` are the PC's other artifact validators."""
    validations = [{"__typename": "CoreArtifactValidator", "state": _v(other)} for other in (state, *others)]
    validations.append({"__typename": "CoreUserValidator", "state": _v("in_progress")})
    pc = {
        "id": "pc1",
        "name": _v("leaf change"),
        "state": _v(pc_state),
        "source_branch": _v(branch),
        "validations": {"edges": [{"node": node} for node in validations]},
    }
    return {"id": "v1", "state": _v(state), "proposed_change": {"node": pc}}


class FakeNode(SimpleNamespace):
    """A created node that records its save."""

    def __init__(self, client: FakeClient, **fields: Any) -> None:
        super().__init__(id=f"new-{fields['kind']}", **fields)
        self._client = client

    async def save(self, **options: Any) -> None:
        """Record the save with its options."""
        self._client.saved.append((self.kind, {k: v for k, v in vars(self).items() if not k.startswith("_")}, options))


class FakeClient:
    """Answers filters/get/create/execute_graphql and records the writes."""

    def __init__(self, existing: list[Any] | None = None) -> None:
        self.existing = existing or []
        self.saved: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.queries: list[tuple[dict[str, Any], str | None]] = []
        self.filters_calls: list[dict[str, Any]] = []
        self.group_context = MagicMock(related_node_ids=[])

    async def filters(self, **kwargs: Any) -> list[Any]:
        """The change requests that already exist."""
        self.filters_calls.append(kwargs)
        return self.existing

    async def get(self, kind: str, **kwargs: Any) -> Any:
        """The change_requests group and the add_change_request definition."""
        return SimpleNamespace(id={"CoreStandardGroup": "g-cr", "CoreGeneratorDefinition": "def-acr"}[kind])

    async def create(self, kind: str, **fields: Any) -> FakeNode:
        """A node that records its save."""
        return FakeNode(self, kind=kind, **fields)

    async def execute_graphql(self, query: str, variables: dict[str, Any], branch_name: str | None = None) -> Any:
        """Record a generator run."""
        self.queries.append((variables, branch_name))
        return {"CoreGeneratorDefinitionRun": {"ok": True}}


def _run(client: FakeClient, row: dict[str, Any]) -> Any:
    gen: Any = OpenChangeRequestGenerator.__new__(OpenChangeRequestGenerator)
    gen.client = client
    gen.branch = "main"
    gen.logger = MagicMock()
    gen.logger.error.side_effect = lambda message, *a, **k: (_ for _ in ()).throw(GeneratorError(message))
    asyncio.run(gen.generate({"CoreArtifactValidator": {"edges": [{"node": row}]}}))
    return gen


def test_a_completed_validator_creates_the_change_request_and_links_it_in_a_comment() -> None:
    """CHG-<pc> on the source branch in change_requests, untracked, then a PC thread with one comment linking it."""
    client = FakeClient()

    _run(client, validator_row())

    (request, thread, comment) = client.saved
    assert request[0] == "ChangeRequest"
    expected = {"branch": BRANCH, "name": "CHG-leaf change", "proposed_change": "pc1", "member_of_groups": ["g-cr"]}
    assert {key: request[1][key] for key in expected} == expected
    assert request[2] == {"allow_upsert": True, "update_group_context": False}
    assert (thread[0], thread[1]["change"], thread[1]["resolved"]) == ("CoreChangeThread", "pc1", False)
    assert comment[0] == "CoreThreadComment"
    assert comment[1]["thread"] == "new-CoreChangeThread"
    assert comment[1]["text"] == change_request_comment("CHG-leaf change", "new-ChangeRequest", BRANCH)
    assert thread[2] == comment[2] == {"update_group_context": False}
    assert client.group_context.related_node_ids == ["v1"]


def test_an_existing_change_request_is_derived_again_without_a_new_comment() -> None:
    """The request exists: add_change_request runs on it on the source branch, nothing is created."""
    client = FakeClient(existing=[SimpleNamespace(id="cr1")])

    _run(client, validator_row())

    assert client.saved == []
    assert client.queries == [({"id": "def-acr", "nodes": ["cr1"]}, BRANCH)]
    assert client.filters_calls == [{"kind": "ChangeRequest", "name__value": "CHG-leaf change", "branch": BRANCH}]


@pytest.mark.parametrize(
    "row",
    [validator_row(state="in_progress"), validator_row(pc_state="merged"), validator_row(branch=None)],
    ids=["validator-running", "pc-not-open", "no-source-branch"],
)
def test_nothing_is_written_until_the_validator_completes_on_an_open_pc(row: dict[str, Any]) -> None:
    """A running validator, a closed PC or no source branch: no lookup, no write, the target still tracked."""
    client = FakeClient()

    _run(client, row)

    assert (client.saved, client.queries, client.filters_calls) == ([], [], [])
    assert client.group_context.related_node_ids == ["v1"]


def test_only_the_last_artifact_validator_of_the_pc_acts() -> None:
    """A PC has one artifact validator per definition: while another is running nothing is looked up or written."""
    client = FakeClient()

    _run(client, validator_row(others=("completed", "in_progress")))

    assert (client.saved, client.queries, client.filters_calls) == ([], [], [])
    assert client.group_context.related_node_ids == ["v1"]


def test_other_validator_kinds_do_not_hold_the_derivation() -> None:
    """A running user check is not an artifact validator: the last completed artifact validator still acts."""
    client = FakeClient()

    _run(client, validator_row(others=("completed",)))

    assert [kind for kind, _, _ in client.saved] == ["ChangeRequest", "CoreChangeThread", "CoreThreadComment"]


def test_no_validator_in_the_query_result_fails() -> None:
    """An empty result is an error, not a silent no-op."""
    with pytest.raises(GeneratorError, match="No CoreArtifactValidator"):
        gen: Any = OpenChangeRequestGenerator.__new__(OpenChangeRequestGenerator)
        gen.client, gen.branch, gen.logger = FakeClient(), "main", MagicMock()
        gen.logger.error.side_effect = lambda message, *a, **k: (_ for _ in ()).throw(GeneratorError(message))
        asyncio.run(gen.generate({"CoreArtifactValidator": {"edges": []}}))


def test_the_change_request_is_named_after_its_proposed_change() -> None:
    """The name the check and add_change_request expect."""
    assert change_request_name("dc1-dc1") == "CHG-dc1-dc1"


def test_the_generator_is_registered_for_the_artifact_validators_group() -> None:
    """The definition targets artifact_validators, passes the validator id, and stays out of the PC pipeline."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definition = {d["name"]: d for d in config["generator_definitions"]}["open_change_request"]
    assert definition["targets"] == "artifact_validators"
    assert definition["parameters"] == {"validator_id": "id"}
    assert definition["execute_in_proposed_change"] is False
    groups = yaml.safe_load((ROOT / "data" / "bootstrap" / "00_groups.yml").read_text())["spec"]["data"]
    assert "artifact_validators" in {group["name"] for group in groups}
