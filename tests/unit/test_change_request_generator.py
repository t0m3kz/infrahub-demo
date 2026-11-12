"""generators/change/change_request.py: what add_change_request reads, what it writes, and what it never touches."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from generators.change import change_request as module
from generators.change.change_request import ChangeRequestGenerator, changed_fields
from generators.logger import GeneratorError
from tests.integration.demo_data import ROOT
from tests.unit.change_text_helpers import raw_node, raw_segment, raw_summaries
from tests.unit.query_shape_helpers import query_tree, unselected

NOW = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
BRANCH = "feat/leaf"
AUTHOR_FIELDS = ("justification", "risk_override", "risk_override_reason", "requested_by", "external_id")
QUERIES = ROOT / "queries" / "change"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(*nodes: dict[str, Any]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def request_row(state: str = "open", **values: Any) -> dict[str, Any]:
    """A raw ChangeRequest row of change_request.gql, the fields unset unless given."""
    fields = (
        "short_description description change_type risk risk_score impact implementation_plan backout_plan "
        "test_plan communication_plan conflicts planned_start planned_end artifact_fingerprint external_id"
    ).split()
    row: dict[str, Any] = {"id": "cr1", "name": _v("CHG-leaf"), **{f: _v(values.get(f)) for f in fields}}
    row["affected_devices"] = _edges(*({"id": i} for i in values.get("devices", [])))
    row["affected_customers"] = _edges(*({"id": i} for i in values.get("customers", [])))
    row["proposed_change"] = {
        "node": {
            "id": "pc1",
            "name": _v("leaf change"),
            "description": _v("Moves a leaf."),
            "source_branch": _v(BRANCH),
            "state": _v(state),
        }
    }
    return row


ARTIFACTS = {
    "a1": {
        "branch": BRANCH,
        "id": "a1",
        "display_label": "dc1-leaf-01 - leaf",
        "action": "updated",
        "target": {"id": "d1", "kind": "DcimPhysicalDevice", "display_label": "dc1-leaf-01"},
        "item_new": {"storage_id": "s2", "checksum": "new1"},
        "item_previous": {"storage_id": "s1", "checksum": "old1"},
    },
    "a2": {
        "branch": BRANCH,
        "id": "a2",
        "display_label": "dc1-leaf-02 - leaf",
        "action": "updated",
        "target": {"id": "d2", "kind": "DcimPhysicalDevice", "display_label": "dc1-leaf-02"},
        "item_new": {"storage_id": "s4", "checksum": "new2"},
        "item_previous": {"storage_id": "s3", "checksum": "old2"},
    },
}

DIFF = {
    **raw_summaries((1, 1, 0), (1, 1, 0), (0, 0, 0)),
    "objects": {
        "nodes": [
            raw_segment("seg1", "c005-web", "10.5.1.1/24", status="UPDATED"),
            raw_node(
                "TopologyCustomerDC",
                "C005-P-DC-DC1",
                "ADDED",
                "fp1",
                {"environment": "p"},
                {"owner": [("C005", "cust-c005")]},
            ),
        ]
    },
}


def _device(device_id: str, name: str, peer: str) -> dict[str, Any]:
    return {
        "id": device_id,
        "__typename": "DcimPhysicalDevice",
        "name": _v(name),
        "role": _v("leaf"),
        "site": {"node": {"name": _v("DC1")}},
        "owner": {"node": {"__typename": "OrganizationProvider"}},
        "capabilities": _edges(
            {
                "__typename": "ManagedMLAG",
                "members": _edges({"id": "d1", "name": _v("dc1-leaf-01")}, {"id": "d2", "name": _v(peer)}),
            },
            {"__typename": "ManagedBGP", "peerings": {"count": 3}},
        ),
        "shared_capabilities": _edges({"__typename": "ManagedVRF", "security_zone": {"node": {"name": _v("PROD")}}}),
    }


FACTS = {
    "DcimDeviceManaged": _edges(
        _device("d1", "dc1-leaf-01", "dc1-leaf-02"), _device("d2", "dc1-leaf-02", "dc1-leaf-02")
    )
}


class FakeClient:
    """Records the GraphQL calls (query name, branch, variables) and answers by operation."""

    address = "http://infrahub:8000"

    def __init__(
        self, peers: dict[str, dict[str, Any]] | None = None, threads: list[dict[str, Any]] | None = None
    ) -> None:
        self.calls: list[tuple[str, str | None, dict[str, Any]]] = []
        self.threads = threads or []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[str] = []
        self.group_context = MagicMock(related_node_ids=[])
        self.peers = peers or {BRANCH: {"CoreProposedChange": _edges(), "ChangeRequest": _edges()}}

    async def _get(self, url: str) -> Any:
        self.gets.append(url)
        response = MagicMock()
        response.json.return_value = ARTIFACTS
        return response

    async def execute_graphql(
        self, query: str, branch_name: str | None = None, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        match = re.search(r"^(?:query|mutation)\s+(\w+)", query, re.MULTILINE)
        name = match.group(1) if match else ""
        self.calls.append((name, branch_name, variables or {}))
        if name == "change_request_diff":
            return DIFF
        if name == "change_request_facts":
            return FACTS
        if name == "change_request_peers":
            return self.peers[branch_name or ""]
        if name == "ChangeRequestPcThread":
            return {"CoreChangeThread": _edges(*self.threads)}
        return {"ok": True}

    async def create(self, kind: str, **fields: Any) -> Any:
        """A node that records itself in ``created`` when saved."""
        client = self

        class _Node(SimpleNamespace):
            async def save(self, **_: Any) -> None:
                client.created.append((kind, fields))

        return _Node(id=f"new-{kind}", **fields)

    def mutations(self, name: str) -> list[dict[str, Any]]:
        return [variables["data"] for op, _, variables in self.calls if op == name]


def _run(client: FakeClient, row: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(module, "utc_now", lambda: NOW)
    gen: Any = ChangeRequestGenerator.__new__(ChangeRequestGenerator)
    gen.client = client
    gen.branch = BRANCH
    gen.logger = MagicMock()
    gen.logger.error.side_effect = lambda message, *a, **k: (_ for _ in ()).throw(GeneratorError(message))
    asyncio.run(gen.generate({"ChangeRequest": _edges(row)}))
    return gen


def test_a_first_run_writes_the_derived_fields_and_the_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """One update with the derived fields, the two devices and the segment's customer added, the target tracked."""
    client = FakeClient()

    _run(client, request_row(), monkeypatch)

    (update,) = client.mutations("ChangeRequestDerive")
    assert update["id"] == "cr1"
    assert (
        update["short_description"]["value"] == "leaf change: 1 added, 1 updated, 0 removed objects; 2 device configs"
    )
    assert update["change_type"]["value"] == "normal"
    assert update["planned_start"]["value"] == "2026-11-12T21:00:00+00:00"
    assert "1. Deploy dc1-leaf-01 (leaf, DC1): leaf: old1 -> new1" in update["implementation_plan"]["value"]
    description = update["description"]["value"]
    assert "NETWORK SEGMENTS (1)" in description and "c005-web: C005, p, ipv4, 10.5.1.0/24" in description
    assert "Customers: C005" in description
    assert "3. Deploy dc1-leaf-02" in update["implementation_plan"]["value"]
    adds = {d["name"]: [n["id"] for n in d["nodes"]] for d in client.mutations("EdgeAdd")}
    assert adds == {"affected_devices": ["d1", "d2"], "affected_customers": ["cust-c005"]}
    assert client.group_context.related_node_ids == ["cr1"]
    assert client.gets == ["http://infrahub:8000/api/diff/artifacts?branch=feat%2Fleaf"]


def test_the_generator_never_writes_an_author_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """justification, the override and its reason, requested_by and external_id are not in any mutation."""
    client = FakeClient()

    _run(client, request_row(), monkeypatch)

    (update,) = client.mutations("ChangeRequestDerive")
    assert not set(update) & set(AUTHOR_FIELDS)
    assert {d["name"] for d in client.mutations("EdgeAdd")} <= {"affected_devices", "affected_customers"}


def test_a_rerun_with_nothing_new_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request already holds what the run derives: no update, no edge write."""
    first = FakeClient()
    _run(first, request_row(), monkeypatch)
    written = {
        name: value["value"] for name, value in first.mutations("ChangeRequestDerive")[0].items() if name != "id"
    }

    second = FakeClient()
    _run(second, request_row(devices=["d1", "d2"], customers=["cust-c005"], **written), monkeypatch)

    assert second.mutations("ChangeRequestDerive") == []
    assert second.mutations("EdgeAdd") == []
    assert second.mutations("EdgeRemove") == []


def test_a_device_no_longer_affected_is_removed_from_the_edge(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale peer goes peer by peer; the others stay."""
    client = FakeClient()

    _run(client, request_row(devices=["d1", "d2", "d9"], customers=["cust-c005"]), monkeypatch)

    assert client.mutations("EdgeRemove") == [{"id": "cr1", "name": "affected_devices", "nodes": [{"id": "d9"}]}]
    assert client.mutations("EdgeAdd") == []


@pytest.mark.parametrize("state", ["merged", "closed", "canceled"])
def test_a_pc_that_is_not_open_is_left_alone(state: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing is read or written for a merged, closed or canceled PC; the target is still tracked."""
    client = FakeClient()

    _run(client, request_row(state=state), monkeypatch)

    assert client.calls == []
    assert client.gets == []
    assert client.group_context.related_node_ids == ["cr1"]


def test_no_request_or_no_pc_fails_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty query result or a request with no proposed change is an error."""
    gen: Any = ChangeRequestGenerator.__new__(ChangeRequestGenerator)
    gen.client = FakeClient()
    gen.branch = BRANCH
    gen.logger = MagicMock()
    with pytest.raises(GeneratorError):
        asyncio.run(gen.generate({"ChangeRequest": _edges()}))
    row = request_row()
    row["proposed_change"] = {"node": None}
    with pytest.raises(GeneratorError):
        asyncio.run(gen.generate({"ChangeRequest": _edges(row)}))


def test_conflicts_are_read_from_the_other_open_pcs_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other open PC's branch is queried; its request lists dc1-leaf-02, so the conflict names it."""
    open_pcs = _edges(
        {"id": "pc1", "name": _v("leaf change"), "source_branch": _v(BRANCH)},
        {"id": "pc2", "name": _v("spine change"), "source_branch": _v("feat/spine")},
    )
    other_request = {
        "id": "cr2",
        "name": _v("CHG-spine"),
        "proposed_change": {"node": {"id": "pc2"}},
        "affected_devices": _edges({"id": "d2", "name": _v("dc1-leaf-02")}),
    }
    client = FakeClient(
        peers={
            BRANCH: {"CoreProposedChange": open_pcs, "ChangeRequest": _edges()},
            "feat/spine": {"CoreProposedChange": open_pcs, "ChangeRequest": _edges(other_request)},
        }
    )

    _run(client, request_row(), monkeypatch)

    assert [branch for op, branch, _ in client.calls if op == "change_request_peers"] == [BRANCH, "feat/spine"]
    (update,) = client.mutations("ChangeRequestDerive")
    assert update["conflicts"]["value"] == "spine change (feat/spine): dc1-leaf-02"


def test_the_reads_of_a_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The artifact targets in one facts call (nothing else is asked of the devices); the data diff is read once."""
    client = FakeClient()

    _run(client, request_row(), monkeypatch)

    (facts,) = [variables for op, _, variables in client.calls if op == "change_request_facts"]
    assert facts == {"device_ids": ["d1", "d2"]}
    (diff,) = [variables for op, _, variables in client.calls if op == "change_request_diff"]
    assert diff["with_summary"] is True and diff["offset"] == 0
    assert "SecurityPolicyRule" in diff["kinds"] and "ChangeRequest" in diff["bookkeeping"]
    writes = ("ChangeRequestDerive", "EdgeAdd", "ChangeRequestPcThread", "ChangeRequestPcComment")
    assert [op for op, _, _ in client.calls if op not in writes] == [
        "change_request_diff",
        "change_request_facts",
        "change_request_peers",
    ]
    assert client.gets == ["http://infrahub:8000/api/diff/artifacts?branch=feat%2Fleaf"]


def _thread(text: str) -> dict[str, Any]:
    """A raw "Change request" thread with one comment."""
    return {"id": "t1", "comments": _edges({"id": "c1", "text": _v(text)})}


def test_the_first_run_opens_the_pc_thread_with_the_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    """No thread yet: one "Change request" thread with one comment: link, type, risk, devices, window, ticket."""
    client = FakeClient()

    _run(client, request_row(), monkeypatch)

    (thread, comment) = client.created
    assert thread == ("CoreChangeThread", {"label": "Change request", "resolved": False, "change": "pc1"})
    assert comment[0] == "CoreThreadComment" and comment[1]["thread"] == "new-CoreChangeThread"
    text = comment[1]["text"]
    assert text.startswith("Change request [CHG-leaf](/objects/ChangeRequest/cr1?branch=feat/leaf):")
    assert "2 devices" in text and "(Europe/Warsaw)" in text and "Ticket: not filed yet." in text


def test_the_pc_comment_follows_the_request_and_a_settled_one_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale comment is rewritten in place (the ticket appeared); the same text again writes nothing."""
    first = FakeClient()
    _run(first, request_row(external_id="CHG0012345"), monkeypatch)
    text = first.created[1][1]["text"]
    assert "Ticket: CHG0012345." in text

    stale = FakeClient(threads=[_thread("old text")])
    _run(stale, request_row(external_id="CHG0012345"), monkeypatch)
    assert stale.created == []
    ((_, _, update),) = [call for call in stale.calls if call[0] == "ChangeRequestPcComment"]
    assert update == {"id": "c1", "text": text}

    settled = FakeClient(threads=[_thread(text)])
    _run(settled, request_row(external_id="CHG0012345"), monkeypatch)
    assert settled.created == []
    assert not [call for call in settled.calls if call[0] == "ChangeRequestPcComment"]


def test_no_artifact_target_asks_no_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A data-only change reads no device."""
    client = FakeClient()
    monkeypatch.setattr(module, "fetch_artifact_diff", _no_artifacts)

    _run(client, request_row(), monkeypatch)

    assert not [op for op, _, _ in client.calls if op == "change_request_facts"]


async def _no_artifacts(*_: Any) -> list:
    return []


def test_changed_fields_compares_datetimes_as_instants() -> None:
    """The server's Z form of the same instant is no change; another instant is written in ISO UTC."""
    derived = {"planned_start": NOW, "risk": "low"}
    assert changed_fields({"planned_start": "2026-11-12T09:00:00Z", "risk": "low"}, derived) == {}
    assert changed_fields({"planned_start": None, "risk": "high"}, derived) == {
        "planned_start": "2026-11-12T09:00:00+00:00",
        "risk": "low",
    }


def test_the_fixtures_emit_only_fields_the_queries_select() -> None:
    """The raw rows stand for the registered queries."""
    assert unselected({"ChangeRequest": _edges(request_row())}, query_tree(QUERIES / "change_request.gql")) == []
    assert unselected(FACTS, query_tree(QUERIES / "change_request_facts.gql")) == []
    assert unselected(DIFF, query_tree(QUERIES / "change_request_diff.gql")) == []


def test_the_generator_is_registered_on_its_group() -> None:
    """add_change_request targets change_requests, runs neither in the PC pipeline nor after merge."""
    config = yaml.safe_load((ROOT / ".infrahub.yml").read_text())
    definition = {d["name"]: d for d in config["generator_definitions"]}["add_change_request"]
    assert definition["targets"] == "change_requests"
    assert definition["class_name"] == "ChangeRequestGenerator"
    assert definition["execute_in_proposed_change"] is False
    assert definition["execute_after_merge"] is False
    assert definition["parameters"] == {"name": "name__value"}
