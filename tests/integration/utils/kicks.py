"""Deployment kicks: the dataclasses, the strict loader of tests/integration/demos/kicks.yml and the suite folder, and the runner."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from infrahub_sdk import InfrahubClientSync
from infrahub_sdk.graphql import Mutation
from infrahub_sdk.task.models import TaskFilter, TaskState
from yaml.resolver import BaseResolver

from utils.limits import PARENT_GENERATOR_WAIT, QUEUE_SETTLE
from utils.task_states import FAILURE_STATES

from .idempotency import changed_objects, diff_fingerprint, idempotency_message
from .proposed_change import (
    change_number,
    create_and_validate_proposed_change,
    fetch_artifacts,
    merge_proposed_change,
    update_diff,
    wait_for_quiet,
)

log = logging.getLogger("tasks")

ROOT = Path(__file__).resolve().parents[3]
KICK_FILE = ROOT / "tests" / "integration" / "demos" / "kicks.yml"
DEMO_DIR = ROOT / "data" / "demos"
# The integration suite's kicks, one file each; their load paths are relative to the repository root.
SUITE_DIR = ROOT / "tests" / "integration" / "kicks"
REGISTRY_FILE = ROOT / ".infrahub.yml"
# The check that follows the stages of a kick with `idempotency`; `--from-stage idempotency` resumes at it.
IDEMPOTENCY = "idempotency"
# The staged 30_all load.
ALL_DEMO_KICK = "all-demo"

# Names become branch names (<kick>-<stage>).
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_KICK_KEYS = {"description", "branch", "stages", "idempotency"}
# The name of a stage's proposed change is a plain text of one line: it is the name of its change request too (CHG-<name>).
PC_NAME_MAX = 100
_STAGE_KEYS = {"name", "load", "generator", "run", "min_artifacts", "merge", "pc_name"}
_CHANGE_ONLY_KEYS = {"min_artifacts", "merge", "pc_name"}
# Assertions of a suite stage: kept raw for tests/integration/kick_expect.py, never read here.
_SUITE_ONLY_KEYS = {"expect"}


class KickError(ValueError):
    """A kick file that does not follow the format."""


@dataclass(frozen=True)
class Stage:
    """One load of paths relative to ``base`` (repository-relative, empty for the root), with what to wait for."""

    name: str
    load: tuple[str, ...]
    generator: str | None = None
    min_artifacts: int = 0
    merge: bool = True
    run: str | None = None
    base: str = "data/demos"
    pc_name: str | None = None  # the name of this stage's proposed change; the stage branch's name when unset

    @property
    def paths(self) -> list[str]:
        """The load paths, relative to the repository."""
        return [f"{self.base}/{path}" if self.base else path for path in self.load]


@dataclass(frozen=True)
class Kick:
    """Stages loaded onto one ``branch`` in turn, or (no branch) one proposed change per stage."""

    name: str
    description: str
    branch: str | None
    stages: tuple[Stage, ...]
    suite: bool = False
    idempotency: int = 0

    @property
    def components(self) -> tuple[str, ...]:
        """The generators the kick exercises, each once, in stage order: what the idempotency check runs again."""
        names = [name for stage in self.stages for name in (stage.generator, stage.run) if name]
        return tuple(dict.fromkeys(names))

    @property
    def staged(self) -> bool:
        """True when every stage loads onto the one branch, with no proposed change."""
        return self.branch is not None

    def stage_branch(self, stage: Stage) -> str:
        """The branch a stage loads onto."""
        return self.branch or f"{self.name}-{stage.name}"

    def proposed_change_name(self, stage: Stage) -> str:
        """The name of the stage's proposed change: its own ``pc_name``, else the name of its branch."""
        return stage.pc_name or self.stage_branch(stage)

    def select(self, from_stage: str = "") -> tuple[Stage, ...]:
        """The stages from ``from_stage`` on (all when empty); KickError for an unknown name."""
        if not from_stage:
            return self.stages
        if from_stage == IDEMPOTENCY and self.idempotency:
            return ()  # resume at the check: every stage is done
        names = [stage.name for stage in self.stages]
        if from_stage not in names:
            raise KickError(f"{self.name}: unknown stage '{from_stage}'. Stages: {', '.join([*names, *self._checks])}")
        return self.stages[names.index(from_stage) :]

    @property
    def _checks(self) -> list[str]:
        """The names after the stages that `--from-stage` accepts."""
        return [IDEMPOTENCY] if self.idempotency else []


class _UniqueKeyLoader(yaml.SafeLoader):
    """A safe loader that refuses a mapping key given twice."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    """Build a mapping, refusing a duplicate key."""
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise KickError(f"line {key_node.start_mark.line + 1}: duplicate key '{key}'")
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _mapping(value: Any, where: str, allowed: set[str]) -> dict[str, Any]:
    """``value`` as a mapping with only ``allowed`` keys."""
    if not isinstance(value, dict):
        raise KickError(f"{where}: expected a mapping")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise KickError(f"{where}: unknown key(s) {unknown}; allowed: {sorted(allowed)}")
    return value


def _count(value: Any, where: str, minimum: int = 0) -> int:
    """``value`` as an integer of at least ``minimum`` (a bool is refused)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise KickError(f"{where}: expected an integer >= {minimum}, got {value!r}")
    return value


def _name(value: Any, where: str) -> str:
    """``value`` as a lowercase name usable in a branch name."""
    if not isinstance(value, str) or not _NAME.match(value):
        raise KickError(f"{where}: name {value!r} must match {_NAME.pattern}")
    return value


def _load_paths(value: Any, where: str, base_dir: Path) -> tuple[str, ...]:
    """The stage's load paths: non-empty, relative to ``base_dir`` and present on disk."""
    under = base_dir.relative_to(ROOT).as_posix() if base_dir.is_relative_to(ROOT) else base_dir.name
    under = under if under != "." else "the repository root"
    if not isinstance(value, list) or not value:
        raise KickError(f"{where}: 'load' must be a non-empty list of paths under {under}")
    for path in value:
        if not isinstance(path, str) or not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise KickError(f"{where}: {path!r} is not a relative path under {under}")
        if not (base_dir / path).exists():
            raise KickError(f"{where}: {path} does not exist under {under}")
    return tuple(value)


@dataclass(frozen=True)
class _Source:
    """Where a file's load paths start and whether its stages may carry ``expect``."""

    base_dir: Path
    generators: set[str]
    suite: bool

    @property
    def base(self) -> str:
        """``base_dir`` relative to the repository ('' for the root), as Stage.base."""
        if not self.base_dir.is_relative_to(ROOT):
            return self.base_dir.as_posix()
        relative = self.base_dir.relative_to(ROOT).as_posix()
        return "" if relative == "." else relative


def _generator(data: dict[str, Any], key: str, where: str, generators: set[str]) -> str | None:
    """The generator named by ``key``, which must be registered in .infrahub.yml."""
    generator = data.get(key)
    if generator is not None and generator not in generators:
        raise KickError(f"{where}: {key} '{generator}' is no generator definition of .infrahub.yml")
    return generator


def _stage(value: Any, where: str, source: _Source, staged: bool, expectations: dict[str, Any]) -> Stage:
    """Parse one stage; its ``expect`` (suite only) goes into ``expectations`` by stage name."""
    allowed = _STAGE_KEYS - _CHANGE_ONLY_KEYS if staged else set(_STAGE_KEYS)
    data = _mapping(value, where, allowed | (_SUITE_ONLY_KEYS if source.suite else set()))
    name = _name(data.get("name"), where)
    where = f"{where} ({name})"
    merge = data.get("merge", True)
    if not isinstance(merge, bool):
        raise KickError(f"{where}: 'merge' must be true or false")
    if "expect" in data:
        expectations[name] = data["expect"]
    return Stage(
        name=name,
        load=_load_paths(data.get("load"), where, source.base_dir),
        generator=_generator(data, "generator", where, source.generators),
        min_artifacts=_count(data.get("min_artifacts", 0), f"{where}.min_artifacts"),
        merge=merge,
        run=_generator(data, "run", where, source.generators),
        base=source.base,
        pc_name=_pc_name(data["pc_name"], where) if "pc_name" in data else None,
    )


def _pc_name(value: Any, where: str) -> str:
    """The name of a stage's proposed change: a plain text of one line, up to ``PC_NAME_MAX`` characters."""
    if not isinstance(value, str) or not value.strip():
        raise KickError(f"{where}: 'pc_name' must be a non-empty text")
    name = value.strip()
    if "\n" in name or len(name) > PC_NAME_MAX:
        raise KickError(f"{where}: 'pc_name' must be one line of at most {PC_NAME_MAX} characters")
    return name


def _kick(name: str, value: Any, source: _Source, expectations: dict[str, Any]) -> Kick:
    """Parse one kick; its stages' ``expect`` go into ``expectations`` by stage name."""
    where = f"kick {_name(name, 'kicks')}"
    data = _mapping(value, where, _KICK_KEYS)
    branch = data.get("branch")
    if branch is not None and (_name(branch, f"{where}.branch") == "main"):
        raise KickError(f"{where}: a staged load never targets main")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        raise KickError(f"{where}: 'description' is required")
    raw_stages = data.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise KickError(f"{where}: 'stages' must be a non-empty list")
    stages = tuple(
        _stage(stage, f"{where} stage {index + 1}", source, branch is not None, expectations)
        for index, stage in enumerate(raw_stages)
    )
    names = [stage.name for stage in stages]
    if len(names) != len(set(names)):
        raise KickError(f"{where}: stage names repeat: {names}")
    idempotency = _count(data.get("idempotency", 0), f"{where}.idempotency")
    if idempotency:
        if not any(stage.generator or stage.run for stage in stages):
            raise KickError(f"{where}: 'idempotency' needs a stage that names a `generator` or a `run`")
        if branch is None and not all(stage.merge for stage in stages):
            raise KickError(f"{where}: 'idempotency' needs every stage merged: it runs on the state main holds")
    return Kick(
        name=name,
        description=data["description"].strip(),
        branch=branch,
        stages=stages,
        suite=source.suite,
        idempotency=idempotency,
    )


def _generator_names(registry: Path) -> set[str]:
    """The generator definitions registered in .infrahub.yml."""
    config = yaml.safe_load(registry.read_text()) or {}
    return {definition["name"] for definition in config.get("generator_definitions") or []}


def _kick_files(path: Path) -> list[Path]:
    """``path`` itself, or every *.yml file of the folder in name order."""
    if not path.exists():
        raise KickError(f"{path}: no such kick file or folder")
    if path.is_dir():
        files = sorted(path.glob("*.yml"))
        if not files:
            raise KickError(f"{path.name}: no kick file (*.yml) in the folder")
        return files
    return [path]


def _load(
    path: Path, source: _Source, taken: frozenset[str] = frozenset()
) -> tuple[dict[str, Kick], dict[tuple[str, str], Any]]:
    """The kicks of a file or folder and the raw ``expect`` per (kick, stage); names unique overall."""
    kicks: dict[str, Kick] = {}
    expectations: dict[tuple[str, str], Any] = {}
    for file in _kick_files(path):
        try:
            document = yaml.load(file.read_text(), Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
        except yaml.YAMLError as exc:
            raise KickError(f"{file.name}: {exc}") from exc
        data = _mapping(document, file.name, {"kicks"})
        raw = data.get("kicks")
        if not isinstance(raw, dict) or not raw:
            raise KickError(f"{file.name}: 'kicks' must map a name to each kick")
        for name, value in raw.items():
            if name in kicks or name in taken:
                raise KickError(f"{file.name}: kick name '{name}' is already used")
            stage_expect: dict[str, Any] = {}
            kicks[name] = _kick(name, value, source, stage_expect)
            expectations.update({(name, stage): expect for stage, expect in stage_expect.items()})
    return kicks, expectations


def load_kicks(path: Path = KICK_FILE, demo_dir: Path = DEMO_DIR, registry: Path = REGISTRY_FILE) -> dict[str, Kick]:
    """Every demo kick of the file (or folder), by name, in file order; KickError on the first rule broken."""
    return _load(path, _Source(demo_dir, _generator_names(registry), suite=False))[0]


def load_suite_kicks(
    path: Path = SUITE_DIR, demo_file: Path = KICK_FILE, registry: Path = REGISTRY_FILE
) -> tuple[dict[str, Kick], dict[tuple[str, str], Any]]:
    """The suite kicks (paths from the repository root, ``expect`` allowed) and their raw ``expect``."""
    taken = frozenset(load_kicks(demo_file, registry=registry))
    return _load(path, _Source(ROOT, _generator_names(registry), suite=True), taken)


def all_kicks() -> dict[str, Kick]:
    """The demo kicks, then the suite kicks."""
    return {**load_kicks(), **load_suite_kicks()[0]}


# ---------------------------------------------------------------------------
# The runner: a kick against an Infrahub instance
# ---------------------------------------------------------------------------

# Loads `paths` onto `branch`; False when the load failed.
LoadFn = Callable[[Sequence[str], str], bool]
# Called as (stage, branch, phase): "before" the load, "branch" once its generators settled, "main" after its merge.
AfterStageFn = Callable[[Stage, str, str], None]

# Settling: generator cascades (add_dc -> add_pod -> add_rack) go quiet between waves, so 20 quiet polls (100 s).
SETTLE_DELAY = 5  # seconds the load gets to schedule its triggers before the queue is read
# The artifact validator appears late: settle briefly, then poll its checks.
ARTIFACT_QUIET_SECONDS = 15
ARTIFACT_MAX_POLLS = 120  # x 10 s


class StageFailed(Exception):
    """A stage that did not complete; ``stage`` is where to resume."""

    def __init__(self, stage: str, message: str) -> None:
        """Keep the stage name with the message."""
        super().__init__(f"Stage {stage}: {message}")
        self.stage = stage


def demo_paths(stage: Stage) -> list[str]:
    """The stage's load paths, relative to the repository."""
    return stage.paths


def failed_task_ids(client: InfrahubClientSync, branch: str) -> set[str]:
    """The ids of every failed, crashed or cancelled task on the branch."""
    return set(failed_tasks(client, branch))


def failed_tasks(client: InfrahubClientSync, branch: str) -> dict[str, str]:
    """Title by id of every failed, crashed or cancelled task on the branch."""
    return {task.id: task.title for task in client.task.filter(filter=TaskFilter(state=FAILURE_STATES, branch=branch))}


def refuse_failed_tasks(client: InfrahubClientSync, stage: Stage, branch: str, known: set[str], phase: str) -> None:
    """StageFailed naming the tasks that failed on the branch since ``known`` was taken; failed tasks must be 0."""
    new = {task_id: title for task_id, title in failed_tasks(client, branch).items() if task_id not in known}
    if new:
        titles = ", ".join(sorted(set(new.values())))
        raise StageFailed(stage.name, f"{len(new)} task(s) failed on '{branch}' {phase}: {titles}")


def wait_for_dispatch(
    client: InfrahubClientSync,
    branch: str,
    generator: str,
    timeout: int = QUEUE_SETTLE.timeout,
) -> None:
    """Block until the load dispatched ``generator`` on the branch; TimeoutError after ``timeout`` seconds."""
    title = f"Run generator {generator}"
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if any(task.title == title for task in client.task.filter(filter=TaskFilter(branch=branch))):
            return
        time.sleep(QUEUE_SETTLE.interval)
    raise TimeoutError(f"'{title}' was never dispatched on '{branch}'")


def start_generator_run(client: InfrahubClientSync, branch: str, name: str) -> str:
    """Start the generator definition ``name`` on every member of its target group on ``branch``; the task id."""
    definition = client.get(kind="CoreGeneratorDefinition", name__value=name, branch=branch)
    mutation = Mutation(
        mutation="CoreGeneratorDefinitionRun",
        input_data={"data": {"id": definition.id}, "wait_until_completion": False},
        query={"ok": None, "task": {"id": None}},
    )
    response = client.execute_graphql(query=mutation.render(), branch_name=branch)
    return response["CoreGeneratorDefinitionRun"]["task"]["id"]


def wait_for_generator_run(client: InfrahubClientSync, branch: str, name: str, task_id: str) -> None:
    """Wait for the run ``task_id`` of generator ``name`` to complete; AssertionError when it ends otherwise."""
    task = client.task.wait_for_completion(
        id=task_id, interval=PARENT_GENERATOR_WAIT.interval, timeout=PARENT_GENERATOR_WAIT.timeout
    )
    if task.state != TaskState.COMPLETED:
        raise AssertionError(f"generator {name} run on '{branch}' ended {task.state}")


def run_generator_definition(client: InfrahubClientSync, branch: str, name: str) -> None:
    """Run the generator definition ``name`` on every member of its target group on ``branch``, to completion."""
    wait_for_generator_run(client, branch, name, start_generator_run(client, branch, name))


def check_idempotency(client: InfrahubClientSync, kick: Kick) -> None:
    """Run the kick's generators again, all at once, ``kick.idempotency`` times; nothing may change. No data load.

    It runs on the kick's own branch, or (a kick whose stages are merged) on a new branch from main, which holds the
    kick's result: the branch's diff against main is the fingerprint, and every round must leave it as it was.
    """
    branch = kick.branch or f"{kick.name}-idempotency"
    if not kick.branch:
        if branch in client.branch.all():
            client.branch.delete(branch_name=branch)
        client.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
    update_diff(client, branch)
    baseline = diff_fingerprint(client, branch)
    known_failures = failed_task_ids(client, branch)
    for round_number in range(1, kick.idempotency + 1):
        log.info("Idempotency %d/%d: running %s together", round_number, kick.idempotency, ", ".join(kick.components))
        runs = {name: start_generator_run(client, branch, name) for name in kick.components}
        for name, task_id in runs.items():
            wait_for_generator_run(client, branch, name, task_id)
        wait_for_quiet(client, branch, initial_delay=SETTLE_DELAY)
        new = {
            task_id: title for task_id, title in failed_tasks(client, branch).items() if task_id not in known_failures
        }
        if new:
            raise AssertionError(f"{len(new)} task(s) failed on '{branch}': {', '.join(sorted(set(new.values())))}")
        update_diff(client, branch)
        if lines := changed_objects(baseline, diff_fingerprint(client, branch)):
            raise AssertionError(idempotency_message(round_number, kick.idempotency, lines))
    if not kick.branch:
        client.branch.delete(branch_name=branch)


def _apply(
    client: InfrahubClientSync, load: LoadFn, stage: Stage, branch: str, after_stage: AfterStageFn | None = None
) -> dict[str, float]:
    """Load the stage, wait for its generators (and run its `run`), refuse new failed tasks; the seconds per phase."""
    if after_stage:
        after_stage(stage, branch, "before")
    started = time.monotonic()
    known_failures = failed_task_ids(client, branch)
    log.info("Stage %s: loading %s into '%s'", stage.name, ", ".join(stage.load), branch)
    if not load(demo_paths(stage), branch):
        raise StageFailed(stage.name, "object load failed")
    loaded = time.monotonic()
    log.info("Stage %s: waiting for generators", stage.name)
    if stage.generator:
        wait_for_dispatch(client, branch, stage.generator)
    wait_for_quiet(client, branch, initial_delay=SETTLE_DELAY)
    if stage.run:
        # A rerun once the load's own triggers settled, as a person would start it by hand.
        log.info("Stage %s: running generator %s", stage.name, stage.run)
        run_generator_definition(client, branch, stage.run)
        wait_for_quiet(client, branch, initial_delay=SETTLE_DELAY)
    refuse_failed_tasks(client, stage, branch, known_failures, "after the generators")
    timings = {"load": round(loaded - started, 1), "generators": round(time.monotonic() - loaded, 1)}
    if after_stage:
        after_stage(stage, branch, "branch")
    return timings


def _deploy(
    client: InfrahubClientSync,
    load: LoadFn,
    kick: Kick,
    stage: Stage,
    after_stage: AfterStageFn | None = None,
    change: str = "",
) -> dict[str, float]:
    """One stage as a change: fresh branch from main, apply, proposed change, artifacts, merge."""
    branch = kick.stage_branch(stage)
    if branch in client.branch.all():
        client.branch.delete(branch_name=branch)
        log.info("Deleted stale branch: %s", branch)
    client.branch.create(branch_name=branch, sync_with_git=False, wait_until_completion=True)
    # Tasks are listed by branch name: a branch deleted and created again (a --from-stage resume, a repeated kick)
    # still shows the failed tasks of the old one
    inherited = failed_task_ids(client, branch)
    timings = _apply(client, load, stage, branch, after_stage)

    started = time.monotonic()
    number = change_number(change)
    description = f"Change {number}. Kick `{kick.name}`, stage `{stage.name}`: loads {', '.join(stage.load)}."
    pc_id = create_and_validate_proposed_change(
        client=client,
        name=kick.proposed_change_name(stage),
        source_branch=branch,
        description=description,
    )["pc_id"]
    timings["proposed_change"] = round(time.monotonic() - started, 1)

    started = time.monotonic()
    wait_for_quiet(client, branch, quiet=ARTIFACT_QUIET_SECONDS, initial_delay=SETTLE_DELAY)
    artifacts = fetch_artifacts(
        client=client, branch=branch, expected_min_total=stage.min_artifacts, max_polls=ARTIFACT_MAX_POLLS
    )
    if artifacts["total"] < stage.min_artifacts:
        raise StageFailed(stage.name, f"expected >= {stage.min_artifacts} artifact(s), got {artifacts['total']}")
    if artifacts["failed"]:
        lines = [f"'{a['name']}' for {a['object']}: {a['status']}" for a in artifacts["failed"]]
        raise StageFailed(stage.name, "artifacts not generated:\n  - " + "\n  - ".join(lines))
    refuse_failed_tasks(client, stage, branch, inherited, "in the proposed change")
    timings["artifacts"] = round(time.monotonic() - started, 1)

    if stage.merge:
        started = time.monotonic()
        known_failures = failed_task_ids(client, "main")
        result = merge_proposed_change(client=client, pc_id=pc_id)
        if not result["success"]:
            checks = result.get("failed_checks") or []
            raise StageFailed(
                stage.name,
                f"merge failed: PC {result['pc_state_before']} -> {result['pc_state_after']}, task {result['task_state']}"
                + ("\n  - " + "\n  - ".join(checks) if checks else ""),
            )
        refuse_failed_tasks(client, stage, "main", known_failures, "after the merge")
        timings["merge"] = round(time.monotonic() - started, 1)
        if after_stage:
            after_stage(stage, "main", "main")
    else:
        log.info("Stage %s: proposed change %s left open on '%s'", stage.name, pc_id, branch)
    return timings


def run_kick(
    client: InfrahubClientSync,
    load: LoadFn,
    kick: Kick,
    from_stage: str = "",
    after_stage: AfterStageFn | None = None,
    change: str = "",
) -> dict[str, dict[str, float]]:
    """Run the kick's stages from ``from_stage``, calling ``after_stage`` per phase; StageFailed on a failure.

    ``change`` is the change number written on each proposed change; none given, each gets CHG and six digits.
    A stage with `merge: false` ends the run: the kick stops after it, without the idempotency check.
    """
    stages = kick.select(from_stage)
    if kick.branch and kick.branch not in client.branch.all():
        client.branch.create(branch_name=kick.branch, sync_with_git=False, wait_until_completion=True)
    timings: dict[str, dict[str, float]] = {}
    for index, stage in enumerate(stages, start=1):
        try:
            if kick.branch:
                timings[stage.name] = _apply(client, load, stage, kick.branch, after_stage)
            else:
                timings[stage.name] = _deploy(client, load, kick, stage, after_stage, change)
        except (AssertionError, TimeoutError) as exc:
            raise StageFailed(stage.name, str(exc)) from exc
        log.info("Stage %d/%d (%s) done: %s", index, len(stages), stage.name, timings[stage.name])
        if not stage.merge:
            # Each stage branches from main: the stages after it need this change merged, which is a person's to do.
            if rest := stages[index:]:
                log.info(
                    "Stage %s left its proposed change open: the kick stops here. Merge it, then continue with "
                    "--from-stage %s",
                    stage.name,
                    rest[0].name,
                )
            return timings
    if kick.idempotency:
        try:
            check_idempotency(client, kick)
        except (AssertionError, TimeoutError) as exc:
            raise StageFailed(IDEMPOTENCY, str(exc)) from exc
    return timings
