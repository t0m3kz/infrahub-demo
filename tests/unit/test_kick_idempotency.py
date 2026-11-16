"""The idempotency check of a kick: its generators run again, together, and the branch's diff must not move."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from infrahub_sdk import InfrahubClientSync

from tests.integration.utils import kicks
from tests.integration.utils.idempotency import Fingerprint, changed_objects, diff_fingerprint, idempotency_message
from tests.integration.utils.kicks import IDEMPOTENCY, Kick, KickError, Stage, load_kicks


def _stage(name: str, generator: str | None = None, run: str | None = None, merge: bool = True) -> Stage:
    """A stage that loads one path, naming the generators it dispatches and runs."""
    return Stage(name=name, load=("a",), generator=generator, run=run, merge=merge)


def _kick(branch: str | None = None, rounds: int = 2) -> Kick:
    """A kick of two stages whose generators are add_dc, add_rack and add_pod (add_dc named twice)."""
    stages = (
        _stage("dc", generator="add_dc"),
        _stage("rack", generator="add_rack", run="add_dc"),
        _stage("pod", "add_pod"),
    )
    return Kick(name="grow", description="d", branch=branch, stages=stages, idempotency=rounds)


def _row(label: str, changed: str = "t1", status: str = "ADDED") -> tuple[Any, ...]:
    """One fingerprint row of a DcimPhysicalDevice."""
    return ("DcimPhysicalDevice", label, status, changed, 1, 0, 0)


def test_the_components_are_the_generators_of_the_stages_each_once() -> None:
    """The check runs every generator the kick exercised, in stage order, none twice."""
    assert _kick().components == ("add_dc", "add_rack", "add_pod")


def test_nothing_changed_is_no_line() -> None:
    """The same fingerprint before and after is idempotent."""
    fingerprint: Fingerprint = {"u1": _row("leaf-1")}

    assert changed_objects(fingerprint, dict(fingerprint)) == []


def test_an_added_a_removed_and_a_touched_object_are_each_named() -> None:
    """A run that adds an object, deletes one or rewrites one moves the fingerprint, and each says which."""
    before: Fingerprint = {"u1": _row("leaf-1"), "u2": _row("leaf-2")}
    after: Fingerprint = {"u1": _row("leaf-1", changed="t2"), "u3": _row("leaf-3")}

    assert changed_objects(before, after) == [
        "added DcimPhysicalDevice leaf-3",
        "changed DcimPhysicalDevice leaf-1",
        "removed DcimPhysicalDevice leaf-2",
    ]


def test_the_message_names_the_run_and_cuts_a_long_list() -> None:
    """The failure text says which run and how many objects, and lists the first ones only."""
    lines = [f"added DcimPhysicalDevice leaf-{n}" for n in range(40)]

    message = idempotency_message(2, 3, lines)

    assert message.startswith("run 2 of 3 changed 40 object(s)")
    assert "and 25 more" in message
    assert "leaf-39" not in message


class _World:
    """The fakes `kicks.check_idempotency` talks to: what ran, in which order, and what the diff reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, fingerprints: list[Fingerprint]) -> None:
        """Replace the server calls of the check; ``fingerprints`` are the diffs read in turn."""
        self.events: list[str] = []
        self.fingerprints = list(fingerprints)
        self.branches = SimpleNamespace(existing=["main"])
        self.client = cast(
            InfrahubClientSync,
            SimpleNamespace(
                branch=SimpleNamespace(
                    all=lambda: list(self.branches.existing), create=self._create, delete=self._delete
                )
            ),
        )
        monkeypatch.setattr(kicks, "update_diff", lambda client, branch: self.events.append(f"diff {branch}"))
        monkeypatch.setattr(kicks, "diff_fingerprint", lambda client, branch: self.fingerprints.pop(0))
        monkeypatch.setattr(kicks, "start_generator_run", self._start)
        monkeypatch.setattr(
            kicks, "wait_for_generator_run", lambda client, branch, name, task_id: self.events.append(f"wait {name}")
        )
        monkeypatch.setattr(kicks, "wait_for_quiet", lambda client, branch, **kwargs: self.events.append("idle"))
        monkeypatch.setattr(kicks, "failed_task_ids", lambda client, branch: set())
        monkeypatch.setattr(kicks, "failed_tasks", lambda client, branch: {})

    def _create(self, branch_name: str, **_: Any) -> None:
        self.events.append(f"create {branch_name}")
        self.branches.existing.append(branch_name)

    def _delete(self, branch_name: str) -> None:
        self.events.append(f"delete {branch_name}")
        self.branches.existing.remove(branch_name)

    def _start(self, client: Any, branch: str, name: str) -> str:
        self.events.append(f"start {name}")
        return f"task-{name}"


def test_all_generators_are_started_before_any_is_waited_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """The components run together: every start comes before the first wait, in each round."""
    same: Fingerprint = {}
    world = _World(monkeypatch, [same, same, same])

    kicks.check_idempotency(world.client, _kick(rounds=2))

    starts = [i for i, event in enumerate(world.events) if event.startswith("start")]
    waits = [i for i, event in enumerate(world.events) if event.startswith("wait")]
    assert len(starts) == 6
    assert max(starts[:3]) < min(waits[:3])
    assert max(starts[3:]) < min(waits[3:])


def test_a_merged_kick_runs_on_a_new_branch_from_main_that_is_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Main holds the kick's result: the check forks it, runs there and deletes the fork."""
    world = _World(monkeypatch, [{}, {}, {}])

    kicks.check_idempotency(world.client, _kick())

    assert world.events[0] == "create grow-idempotency"
    assert world.events[-1] == "delete grow-idempotency"


def test_a_staged_kick_runs_on_its_own_branch_and_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A staged kick's result is on its branch: the check runs there and leaves it."""
    world = _World(monkeypatch, [{}, {}, {}])

    kicks.check_idempotency(world.client, _kick(branch="demo"))

    assert not [event for event in world.events if event.startswith(("create", "delete"))]
    assert "diff demo" in world.events


def test_a_run_that_changes_an_object_fails_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second round touches an object: AssertionError names the run and the object."""
    baseline: Fingerprint = {"u1": _row("leaf-1")}
    world = _World(monkeypatch, [baseline, dict(baseline), {"u1": _row("leaf-1", changed="t9")}])

    with pytest.raises(AssertionError, match=r"(?s)run 2 of 2 changed 1 object.*changed DcimPhysicalDevice leaf-1"):
        kicks.check_idempotency(world.client, _kick())


def test_a_task_that_failed_during_a_round_fails_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A generator task that failed is reported before the diff is read."""
    world = _World(monkeypatch, [{}, {}])
    monkeypatch.setattr(kicks, "failed_tasks", lambda client, branch: {"t1": "Generator add_dc"})

    with pytest.raises(AssertionError, match="(?s)1 task.*failed.*Generator add_dc"):
        kicks.check_idempotency(world.client, _kick())


def test_the_check_is_a_resume_point() -> None:
    """`--from-stage idempotency` resumes at the check, with no stage left to run."""
    assert _kick().select(IDEMPOTENCY) == ()
    with pytest.raises(KickError, match="Stages: dc, rack, pod, idempotency"):
        _kick().select("nope")


def _load(tmp_path: Any, kick_body: str) -> Kick:
    """Load one kick whose body is ``kick_body`` against a tiny demo tree and registry."""
    demos = tmp_path / "demos"
    (demos / "a").mkdir(parents=True)
    (demos / "a" / "x.yml").write_text("---\n")
    registry = tmp_path / ".infrahub.yml"
    registry.write_text("generator_definitions:\n  - name: add_dc\n")
    path = tmp_path / "kicks.yml"
    path.write_text(f"kicks:\n  one:\n    description: d\n{kick_body}")
    return load_kicks(path, demos, registry)["one"]


def test_the_loader_reads_the_key(tmp_path: Any) -> None:
    """`idempotency: 2` is the number of re-runs of the kick's generators."""
    kick = _load(tmp_path, "    idempotency: 2\n    stages:\n      - {name: s, load: [a], generator: add_dc}\n")

    assert kick.idempotency == 2


def test_the_loader_refuses_the_key_without_a_generator(tmp_path: Any) -> None:
    """With no generator or run in any stage there is nothing to run again."""
    with pytest.raises(KickError, match="needs a stage that names a `generator` or a `run`"):
        _load(tmp_path, "    idempotency: 2\n    stages:\n      - {name: s, load: [a]}\n")


def test_the_loader_refuses_the_key_with_an_open_stage(tmp_path: Any) -> None:
    """A stage left open is not on main, which is where the check forks its branch from."""
    with pytest.raises(KickError, match="every stage merged"):
        _load(
            tmp_path,
            "    idempotency: 2\n    stages:\n      - {name: s, load: [a], generator: add_dc, merge: false}\n",
        )


def _node(kind: str, status: str = "UPDATED", attributes: tuple[str, ...] = (), edges: tuple[str, ...] = ()) -> dict:
    """One row of DiffTree: the attributes and edges named changed, one unchanged attribute besides."""
    return {
        "uuid": f"{kind}-{status}-{attributes}-{edges}",
        "kind": kind,
        "label": kind.lower(),
        "status": status,
        "last_changed_at": "t1",
        "num_added": 0,
        "num_updated": 0,
        "num_removed": 0,
        "attributes": [
            {"name": "name", "status": "UNCHANGED"},
            *({"name": a, "status": "UPDATED"} for a in attributes),
        ],
        "relationships": [{"name": e, "status": "UPDATED"} for e in edges],
    }


def _fingerprint_of(*nodes: dict) -> Fingerprint:
    """The fingerprint the check takes of a diff with these nodes."""
    client = cast(
        InfrahubClientSync,
        SimpleNamespace(execute_graphql=lambda query, variables: {"DiffTree": {"nodes": list(nodes)}}),
    )
    return diff_fingerprint(client, "grow")


def test_the_tracking_groups_are_not_a_change() -> None:
    """A run adds what it generated to its tracking groups: the groups are bookkeeping of the SDK."""
    assert _fingerprint_of(_node("CoreGeneratorAwareGroup", "ADDED"), _node("CoreGeneratorAwareGroup")) == {}


def test_joining_a_tracking_group_is_not_a_change_of_the_member() -> None:
    """The only change of a member is its `member_of_groups` edge: that is the run joining its tracking group."""
    assert _fingerprint_of(_node("DcimCable", edges=("member_of_groups",))) == {}


def test_an_unchanged_parent_listed_for_context_is_not_a_change() -> None:
    """The diff lists the parent of a changed object (DC6 above its pods) as UNCHANGED; its child counts move when a
    child only joins a tracking group, so it is no change of its own (dc6-flow, 2026-11-16).
    """
    parent = {**_node("TopologyDataCenter", "UNCHANGED"), "num_updated": 3}

    assert _fingerprint_of(parent, _node("TopologyPod", edges=("member_of_groups",))) == {}


def test_a_new_object_a_new_value_and_another_edge_are_changes() -> None:
    """An added object, a changed attribute and a changed edge other than the group one all stay in the fingerprint."""
    nodes = (
        _node("DcimCable", "ADDED"),
        _node("DcimPhysicalDevice", attributes=("status",)),
        _node("ManagedBGPPeering", edges=("member_of_groups", "local_interface")),
    )

    assert len(_fingerprint_of(*nodes)) == 3
