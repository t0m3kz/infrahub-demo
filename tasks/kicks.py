"""Deployment kicks: the dataclasses and the strict loader of tasks/kicks.yml and the suite folder."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.resolver import BaseResolver

ROOT = Path(__file__).resolve().parents[1]
KICK_FILE = Path(__file__).with_name("kicks.yml")
DEMO_DIR = ROOT / "data" / "demos"
# The integration suite's kicks, one file each; their load paths are relative to the repository root.
SUITE_DIR = ROOT / "tests" / "integration" / "kicks"
REGISTRY_FILE = ROOT / ".infrahub.yml"
# The staged 30_all load.
ALL_DEMO_KICK = "all-demo"

# Names become branch names (<kick>-<stage>).
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_KICK_KEYS = {"description", "branch", "stages"}
_STAGE_KEYS = {"name", "load", "generator", "run", "min_artifacts", "merge"}
_CHANGE_ONLY_KEYS = {"min_artifacts", "merge"}
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

    @property
    def staged(self) -> bool:
        """True when every stage loads onto the one branch, with no proposed change."""
        return self.branch is not None

    def stage_branch(self, stage: Stage) -> str:
        """The branch a stage loads onto."""
        return self.branch or f"{self.name}-{stage.name}"

    def select(self, from_stage: str = "") -> tuple[Stage, ...]:
        """The stages from ``from_stage`` on (all when empty); KickError for an unknown name."""
        if not from_stage:
            return self.stages
        names = [stage.name for stage in self.stages]
        if from_stage not in names:
            raise KickError(f"{self.name}: unknown stage '{from_stage}'. Stages: {', '.join(names)}")
        return self.stages[names.index(from_stage) :]


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
    )


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
    # Each stage branches from main, so a merged stage after an open one would not see the open one's objects.
    for before, after in zip(stages, stages[1:]):
        if not before.merge and after.merge:
            raise KickError(f"{where}: stage '{after.name}' merges after the open stage '{before.name}'")
    return Kick(name=name, description=data["description"].strip(), branch=branch, stages=stages, suite=source.suite)


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


def load_kick(name: str, path: Path = KICK_FILE) -> Kick:
    """One demo kick by name; KickError for an unknown one."""
    kicks = load_kicks(path)
    if name not in kicks:
        raise KickError(f"Unknown kick '{name}'. Kicks: {', '.join(kicks)}")
    return kicks[name]
