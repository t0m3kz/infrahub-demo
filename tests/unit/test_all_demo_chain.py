"""Unit tests for tasks.ALL_DEMO_CHAIN, the 30_all chain of reviewed changes (tests/integration/test_58_all_demo_chain.py)."""

from __future__ import annotations

from pathlib import Path

import yaml

import tasks

CHAIN = tasks.ALL_DEMO_CHAIN


def _files(root_dir: Path, path: str) -> set[Path]:
    """The object files a load path stands for: the file, or every YAML file under the directory."""
    full = root_dir / tasks.ALL_DEMO_DATA / path
    if full.is_file():
        return {full}
    return {file for pattern in ("*.yml", "*.yaml") for file in full.rglob(pattern)}


def test_step_names_are_unique() -> None:
    """A step name is its branch and its dependency name, so two steps never share one."""
    names = [step.name for step in CHAIN]
    assert len(names) == len(set(names)), names


def test_step_paths_exist(root_dir: Path) -> None:
    """Every path of every step is a file or a directory with object files."""
    for step in CHAIN:
        for path in step.paths:
            assert _files(root_dir, path), f"{step.name}: {path} holds no object file"


def test_no_file_is_loaded_by_two_steps(root_dir: Path) -> None:
    """A file belongs to one change: loaded twice, the second step would re-upsert the first one's objects."""
    owner: dict[Path, str] = {}
    for step in CHAIN:
        for path in step.paths:
            for file in _files(root_dir, path):
                assert file not in owner, f"{file} is in step '{owner[file]}' and step '{step.name}'"
                owner[file] = step.name


def test_chain_loads_only_what_the_staged_load_loads(root_dir: Path) -> None:
    """While both exist, the chain and ALL_DEMO_LOAD_STAGES load the same 30_all files (the chain a subset)."""
    staged = {file for _, paths in tasks.ALL_DEMO_LOAD_STAGES for path in paths for file in _files(root_dir, path)}
    for step in CHAIN:
        for path in step.paths:
            assert _files(root_dir, path) <= staged, f"{step.name}: {path} is not part of the staged load"


def test_step_generators_are_registered(root_dir: Path) -> None:
    """A step waits for its generator's dispatch, so the name must be a generator definition of .infrahub.yml."""
    registry = yaml.safe_load((root_dir / ".infrahub.yml").read_text())
    definitions = {definition["name"] for definition in registry["generator_definitions"]}
    for step in CHAIN:
        if step.generator is not None:
            assert step.generator in definitions, f"{step.name}: unknown generator {step.generator}"


def test_steps_order_foundation_first() -> None:
    """The foundation (customers, internet access) comes before any fabric."""
    assert CHAIN[0].name == "foundation"
