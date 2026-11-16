"""The name of the proposed change of a kick stage: a plain `pc_name` text on the stage, the branch name without it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.utils import kicks
from tests.integration.utils.kicks import PC_NAME_MAX, Kick, KickError, Stage, load_kicks


def _kick(body: str, tmp_path: Path, stage: str = "{name: rack, load: [a]}") -> Kick:
    """Load one kick whose body is ``body`` and whose one stage is ``stage``, against a tiny demo tree and registry."""
    demos = tmp_path / "demos"
    (demos / "a").mkdir(parents=True, exist_ok=True)
    (demos / "a" / "x.yml").write_text("---\n")
    registry = tmp_path / ".infrahub.yml"
    registry.write_text("generator_definitions:\n  - name: add_dc\n")
    path = tmp_path / "kicks.yml"
    path.write_text(f"kicks:\n  grow:\n    description: d\n{body}    stages:\n      - {stage}\n")
    return load_kicks(path, demos, registry)["grow"]


def test_without_a_name_the_proposed_change_is_named_like_the_branch(tmp_path: Path) -> None:
    """No `pc_name` on the stage: the proposed change is named `<kick>-<stage>`, as before the key existed."""
    kick = _kick("", tmp_path)

    assert kick.stages[0].pc_name is None
    assert kick.proposed_change_name(kick.stages[0]) == kick.stage_branch(kick.stages[0]) == "grow-rack"


def test_a_stage_names_its_own_proposed_change(tmp_path: Path) -> None:
    """The text of `pc_name` is the name, as written (spaces and punctuation included)."""
    kick = _kick("", tmp_path, stage='{name: rack, load: [a], pc_name: "DC6 new rack (CHG0012345)"}')

    assert kick.proposed_change_name(kick.stages[0]) == "DC6 new rack (CHG0012345)"


def test_the_branch_keeps_its_own_name(tmp_path: Path) -> None:
    """The name is only the proposed change's: the stage still loads onto `<kick>-<stage>`."""
    kick = _kick("", tmp_path, stage='{name: rack, load: [a], pc_name: "Anything"}')

    assert kick.stage_branch(kick.stages[0]) == "grow-rack"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('"   "', "non-empty text"),
        ("[1, 2]", "non-empty text"),
        (f'"{"x" * (PC_NAME_MAX + 1)}"', "one line of at most"),
        ('"two\\nlines"', "one line of at most"),
    ],
)
def test_a_name_that_is_no_plain_line_is_refused(tmp_path: Path, text: str, message: str) -> None:
    """An empty or non-text value, a long one and one with a line break fail at load."""
    with pytest.raises(KickError, match=message):
        _kick("", tmp_path, stage=f"{{name: rack, load: [a], pc_name: {text}}}")


def test_a_kick_with_a_branch_has_no_proposed_change_to_name(tmp_path: Path) -> None:
    """A kick with a `branch` opens no proposed change: `pc_name` is not a key of its stages."""
    with pytest.raises(KickError, match="unknown key"):
        _kick("    branch: demo\n", tmp_path, stage='{name: rack, load: [a], pc_name: "x"}')


def test_the_deploy_step_opens_the_proposed_change_under_the_stage_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_deploy opens the proposed change under the stage's `pc_name`, and the change number stays in its description."""
    opened: dict[str, Any] = {}

    def create(**kwargs: Any) -> dict[str, Any]:
        opened.update(kwargs)
        return {"pc_id": "pc-1"}

    client: Any = type(
        "Client",
        (),
        {
            "branch": type(
                "Branch", (), {"all": staticmethod(lambda: []), "create": staticmethod(lambda **kwargs: None)}
            )
        },
    )
    monkeypatch.setattr(kicks, "_apply", lambda *args, **kwargs: {})
    monkeypatch.setattr(kicks, "create_and_validate_proposed_change", create)
    monkeypatch.setattr(kicks, "wait_for_quiet", lambda *args, **kwargs: None)
    monkeypatch.setattr(kicks, "fetch_artifacts", lambda **kwargs: {"total": 1, "failed": []})
    monkeypatch.setattr(kicks, "refuse_failed_tasks", lambda *args, **kwargs: None)
    monkeypatch.setattr(kicks, "failed_task_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(kicks, "merge_proposed_change", lambda **kwargs: {"success": True})
    stage = Stage(name="rack", load=("a",), pc_name="Rack for the audit")
    kick = Kick(name="grow", description="d", branch=None, stages=(stage,))

    kicks._deploy(client, lambda paths, branch: True, kick, stage, change="CHG0012345")

    assert opened["name"] == "Rack for the audit"
    assert opened["source_branch"] == "grow-rack"
    assert "Change CHG0012345." in opened["description"]


def _run_stages(monkeypatch: pytest.MonkeyPatch, kick: Kick) -> list[str]:
    """Run the kick with the stage step faked: the names of the stages that ran."""
    ran: list[str] = []
    client: Any = type("Client", (), {"branch": type("Branch", (), {"all": staticmethod(lambda: [])})})
    monkeypatch.setattr(kicks, "_deploy", lambda client, load, kick, stage, after, change: ran.append(stage.name) or {})
    monkeypatch.setattr(kicks, "check_idempotency", lambda client, kick: ran.append("idempotency"))

    kicks.run_kick(client, lambda paths, branch: True, kick)

    return ran


def test_a_stage_that_stays_open_ends_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """`merge: false`: the stages after it need that change on main, so the kick stops after it."""
    stages = (
        Stage(name="one", load=("a",)),
        Stage(name="two", load=("a",), merge=False),
        Stage(name="three", load=("a",)),
    )

    ran = _run_stages(monkeypatch, Kick(name="grow", description="d", branch=None, stages=stages))

    assert ran == ["one", "two"]


def test_every_stage_runs_when_all_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an open stage the whole kick runs, and its idempotency check follows."""
    stages = (Stage(name="one", load=("a",)), Stage(name="two", load=("a",)))

    ran = _run_stages(monkeypatch, Kick(name="grow", description="d", branch=None, stages=stages, idempotency=1))

    assert ran == ["one", "two", "idempotency"]


def test_stages_may_follow_an_open_stage_in_the_file(tmp_path: Path) -> None:
    """The file may hold stages after an open one: they run when the kick is resumed there, once it is merged."""
    kick = _kick("", tmp_path, stage="{name: rack, load: [a], merge: false}\n      - {name: pod, load: [a]}")

    assert [stage.merge for stage in kick.stages] == [False, True]
