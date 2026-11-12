"""Unit tests for the kick loader (tasks/kicks.py), tasks/kicks.yml and tests/integration/kicks/."""

from __future__ import annotations

from pathlib import Path

import pytest

from tasks.kicks import (
    ALL_DEMO_KICK,
    DEMO_DIR,
    ROOT,
    KickError,
    Stage,
    load_kick,
    load_kicks,
    load_suite_kicks,
)

KICKS = load_kicks()


def _demo_files(path: str) -> set[Path]:
    """The object files one load path stands for: the file, or every YAML file under the directory."""
    full = DEMO_DIR / path
    if full.is_file():
        return {full}
    return {file for pattern in ("*.yml", "*.yaml") for file in full.rglob(pattern)}


def _load(tmp_path: Path, text: str) -> dict:
    """Load a kick file written to tmp_path, against a tiny demo tree and registry."""
    demos = tmp_path / "demos"
    (demos / "a").mkdir(parents=True)
    (demos / "a" / "x.yml").write_text("---\n")
    (demos / "b.yml").write_text("---\n")
    registry = tmp_path / ".infrahub.yml"
    registry.write_text("generator_definitions:\n  - name: add_dc\n")
    path = tmp_path / "kicks.yml"
    path.write_text(text)
    return load_kicks(path, demo_dir=demos, registry=registry)


CHANGE = """
kicks:
  pb:
    description: Two changes.
    stages:
      - {name: one, load: [a], generator: add_dc, min_artifacts: 1}
      - {name: two, load: [b.yml], merge: false}
"""


# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------


def test_a_change_kick_is_parsed_with_its_defaults(tmp_path: Path) -> None:
    """No branch: one change per stage, on <kick>-<stage>; merge defaults to true, min_artifacts to 0."""
    kick = _load(tmp_path, CHANGE)["pb"]
    one, two = kick.stages
    assert not kick.staged
    demos = (tmp_path / "demos").resolve()
    base = demos.relative_to(ROOT).as_posix() if demos.is_relative_to(ROOT) else demos.as_posix()
    assert one == Stage(name="one", load=("a",), generator="add_dc", min_artifacts=1, base=base)
    assert two == Stage(name="two", load=("b.yml",), merge=False, base=base)
    assert one.paths == [f"{base}/a"]
    assert kick.stage_branch(two) == "pb-two"


def test_a_staged_kick_loads_every_stage_onto_its_branch(tmp_path: Path) -> None:
    """A branch: every stage on that branch, no proposed change."""
    text = "kicks:\n  pb:\n    description: d\n    branch: demo\n    stages: [{name: one, load: [a]}]\n"
    kick = _load(tmp_path, text)["pb"]
    assert kick.staged and kick.stage_branch(kick.stages[0]) == "demo"


def test_select_resumes_from_a_stage_and_refuses_an_unknown_one(tmp_path: Path) -> None:
    """select() drops the stages before the named one; an unknown name lists the stages."""
    kick = _load(tmp_path, CHANGE)["pb"]
    assert kick.select() == kick.stages
    assert [stage.name for stage in kick.select("two")] == ["two"]
    with pytest.raises(KickError, match="Stages: one, two"):
        kick.select("three")


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("kicks:\n  pb:\n    description: d\n    color: red\n    stages: [{name: s, load: [a]}]\n", "unknown key"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], wait: 1}]\n", "unknown key"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [missing]}]\n", "does not exist"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [../a]}]\n", "relative path"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: []}]\n", "non-empty list"),
        ("kicks:\n  pb:\n    description: d\n    stages: []\n", "non-empty list"),
        ("kicks:\n  pb:\n    stages: [{name: s, load: [a]}]\n", "'description' is required"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: S!, load: [a]}]\n", "must match"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], generator: x}]\n", "no generator"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], run: x}]\n", "run 'x' is no gen"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], min_artifacts: -1}]\n", ">= 0"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], merge: maybe}]\n", "true or false"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a]}, {name: s, load: [a]}]\n", "repeat"),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], expect: {dc: D}}]\n", "unknown key"),
        (
            "kicks:\n  pb:\n    description: d\n    branch: b\n    stages: [{name: s, load: [a], merge: false}]\n",
            "unknown key",
        ),
        ("kicks:\n  pb:\n    description: d\n    branch: main\n    stages: [{name: s, load: [a]}]\n", "main"),
        (
            "kicks:\n  pb:\n    description: d\n    stages:\n"
            "      - {name: one, load: [a], merge: false}\n      - {name: two, load: [a]}\n",
            "merges after the open stage",
        ),
        ("kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a]}]\n  pb:\n    x: 1\n", "duplicate"),
        ("kicks: {}\n", "must map"),
        ("stages: []\n", "unknown key"),
    ],
)
def test_a_broken_rule_is_refused(tmp_path: Path, text: str, error: str) -> None:
    """Every rule of the format is enforced on load, with a message naming it."""
    with pytest.raises(KickError, match=error):
        _load(tmp_path, text)


def test_a_run_names_a_registered_generator(tmp_path: Path) -> None:
    """`run` is kept on the stage once it is a generator of the registry."""
    text = "kicks:\n  pb:\n    description: d\n    stages: [{name: s, load: [a], run: add_dc}]\n"
    assert _load(tmp_path, text)["pb"].stages[0].run == "add_dc"


DEMO_TEXT = "kicks: {d: {description: d, stages: [{name: s, load: [06_servers]}]}}\n"


def _suite(folder: Path, files: dict[str, str]) -> tuple[dict, dict]:
    """Load a suite ``folder`` of ``files`` (paths from the repository root) next to a one-kick demo file."""
    folder.mkdir(parents=True)
    for name, text in files.items():
        (folder / name).write_text(text)
    registry = folder / "registry.yml.txt"
    registry.write_text("generator_definitions:\n  - name: add_dc\n")
    demo_file = folder.parent / f"{folder.name}-demo.yml"
    demo_file.write_text(DEMO_TEXT)
    return load_suite_kicks(folder, demo_file=demo_file, registry=registry)


SUITE_STAGE = "    stages: [{name: s, load: [tasks/kicks.yml], run: add_dc, expect: {removed: 0}}]\n"


def test_a_suite_folder_loads_every_file_with_its_expect_set_aside(tmp_path: Path) -> None:
    """Each *.yml of the folder; paths from the repository root; `expect` returned raw per (kick, stage)."""
    kicks, expect = _suite(
        tmp_path / "suite",
        {
            "a.yml": "kicks:\n  one:\n    description: d\n" + SUITE_STAGE,
            "b.yml": "kicks:\n  two:\n    description: d\n" + SUITE_STAGE,
            "notes.txt": "ignored",
        },
    )
    assert list(kicks) == ["one", "two"]
    assert kicks["one"].suite and kicks["one"].stages[0].paths == ["tasks/kicks.yml"]
    assert expect == {("one", "s"): {"removed": 0}, ("two", "s"): {"removed": 0}}


def test_a_suite_name_is_unique_across_files_and_against_the_demo_kicks(tmp_path: Path) -> None:
    """The same name in two suite files, or a demo kick's name, is refused."""
    text = "kicks:\n  one:\n    description: d\n" + SUITE_STAGE
    with pytest.raises(KickError, match="'one' is already used"):
        _suite(tmp_path / "twice", {"a.yml": text, "b.yml": text})
    with pytest.raises(KickError, match="'d' is already used"):
        _suite(tmp_path / "demo-name", {"a.yml": text.replace("one:", "d:")})


def test_a_missing_suite_folder_is_refused(tmp_path: Path) -> None:
    """A path that is neither a file nor a folder names itself."""
    with pytest.raises(KickError, match="no such kick file or folder"):
        load_kicks(tmp_path / "nope.yml")


def test_load_kick_refuses_an_unknown_name() -> None:
    """An unknown kick lists the known ones."""
    with pytest.raises(KickError, match=ALL_DEMO_KICK):
        load_kick("nope")


# ---------------------------------------------------------------------------
# tasks/kicks.yml
# ---------------------------------------------------------------------------


def test_every_kick_path_holds_object_files() -> None:
    """Every load path of every kick is a file or a directory with object files."""
    for kick in KICKS.values():
        for stage in kick.stages:
            for path in stage.load:
                assert _demo_files(path), f"{kick.name}/{stage.name}: {path} holds no object file"


def test_every_demo_with_object_files_is_in_a_kick() -> None:
    """A demo folder with object files is loaded by at least one kick."""
    loaded = {Path(path).parts[0] for p in KICKS.values() for stage in p.stages for path in stage.load}
    demos = {folder.name for folder in DEMO_DIR.iterdir() if folder.is_dir() and _demo_files(folder.name)}
    assert demos - loaded == set()


def test_the_all_demo_kick_is_a_staged_load_of_30_all() -> None:
    """all-demo stays the four-stage 30_all load test_59 - test_66 assert against."""
    kick = KICKS[ALL_DEMO_KICK]
    assert kick.staged
    assert [stage.name for stage in kick.stages] == [
        "foundation",
        "compute_and_customers",
        "applications",
        "interconnects",
    ]
    assert all(path.startswith("30_all/") for stage in kick.stages for path in stage.load)


def test_internet_access_edge_loads_in_foundation_before_boarding() -> None:
    """add_sdwan_edge (boarding stage) needs the ISP and its access PE, so 04_internet is a foundation path."""
    stages = {stage.name: stage.load for stage in KICKS[ALL_DEMO_KICK].stages}
    assert "30_all/04_internet" in stages["foundation"]
    assert "30_all/04_internet" not in stages["interconnects"]


def test_no_file_is_loaded_by_two_stages_of_a_change_kick() -> None:
    """A file belongs to one change: loaded twice, the second change would re-upsert the first one's objects."""
    for kick in KICKS.values():
        owner: dict[Path, str] = {}
        for stage in kick.stages:
            for file in {file for path in stage.load for file in _demo_files(path)}:
                assert file not in owner, f"{kick.name}: {file} is in '{owner[file]}' and '{stage.name}'"
                owner[file] = stage.name


def test_the_kick_loads_only_what_the_staged_load_loads() -> None:
    """all-demo-reviewed loads a subset of the all-demo files."""
    staged = {f for stage in KICKS[ALL_DEMO_KICK].stages for path in stage.load for f in _demo_files(path)}
    for stage in KICKS["all-demo-reviewed"].stages:
        for path in stage.load:
            assert _demo_files(path) <= staged, f"{stage.name}: {path} is not part of the staged load"


def test_a_single_dc_kick_is_its_stage_of_dc_deployments() -> None:
    """dc1 ... dc7 each run exactly their stage of dc-deployments."""
    for stage in KICKS["dc-deployments"].stages:
        assert KICKS[stage.name].stages == (stage,)


# ---------------------------------------------------------------------------
# tests/integration/kicks/
# ---------------------------------------------------------------------------


def test_the_suite_kicks_load_from_the_repository_root() -> None:
    """Every suite kick loads (paths exist from the root) and no name repeats a demo kick."""
    kicks, _ = load_suite_kicks()
    assert {"dc6-suite", "full-topology-suite"} <= set(kicks)
    assert not set(kicks) & set(KICKS)
    for kick in kicks.values():
        for stage in kick.stages:
            assert all((ROOT / path).exists() for path in stage.paths), f"{kick.name}/{stage.name}"
