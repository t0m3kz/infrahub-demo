"""Guard: every query under queries/ is covered by a smoke `syntax_check` entry in tests/smoke/test_graphql.yml."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

SMOKE_KIND = "graphql-query-smoke"
SMOKE_TEST_NAME = "syntax_check"
FRAGMENTS_DIR = "queries/fragments"
FLAT_DIR = "queries/flat"


def _flat(path: str) -> str:
    """The registered flat copy of a source query (utils/query_flatten.py)."""
    return FLAT_DIR + "/" + path.removeprefix("queries/")


@pytest.fixture(scope="module")
def registered_queries(root_dir: Path) -> dict[str, str]:
    """Return the file_path -> name map of the `queries:` list in .infrahub.yml."""
    config = yaml.safe_load((root_dir / ".infrahub.yml").read_text())
    return {entry["file_path"]: entry["name"] for entry in config["queries"]}


@pytest.fixture(scope="module")
def query_files(root_dir: Path) -> list[str]:
    """Return every source query (fragment library and flat copies excluded) as a repo-relative POSIX path."""
    return sorted(
        rel
        for rel in (path.relative_to(root_dir).as_posix() for path in (root_dir / "queries").rglob("*.gql"))
        if not rel.startswith((FRAGMENTS_DIR + "/", FLAT_DIR + "/"))
    )


@pytest.fixture(scope="module")
def smoke_entries(root_dir: Path) -> list[tuple[str, str, dict[str, Any]]]:
    """Return one (resource_name, path, spec) per `syntax_check` test of tests/smoke/test_graphql.yml."""
    document = yaml.safe_load((root_dir / "tests" / "smoke" / "test_graphql.yml").read_text())
    return [
        (resource["resource_name"], test["spec"]["path"], test["spec"])
        for resource in document["infrahub_tests"]
        for test in resource["tests"]
        if test["name"] == SMOKE_TEST_NAME
    ]


def test_every_query_file_has_a_smoke_entry(query_files: list[str], smoke_entries: list[tuple[str, str, Any]]) -> None:
    """A query whose registered copy has no `syntax_check` entry is never parsed by `invoke validate`."""
    covered = {path for _, path, _ in smoke_entries}
    assert not [path for path in query_files if _flat(path) not in covered]


def test_smoke_entries_use_the_smoke_kind(smoke_entries: list[tuple[str, str, dict[str, Any]]]) -> None:
    """Every entry runs the offline parse item, not the integration one that needs a synced repository."""
    assert not [path for _, path, spec in smoke_entries if spec.get("kind") != SMOKE_KIND]


def test_smoke_entries_point_at_existing_files(root_dir: Path, smoke_entries: list[tuple[str, str, Any]]) -> None:
    """An entry whose file is gone would fail (or rot) in the smoke run."""
    assert not [path for _, path, _ in smoke_entries if not (root_dir / path).is_file()]


def test_smoke_entries_have_no_duplicate_paths(smoke_entries: list[tuple[str, str, Any]]) -> None:
    """One query, one entry: a second entry for the same file is a stale resource_name."""
    counts = Counter(path for _, path, _ in smoke_entries)
    assert not {path: count for path, count in counts.items() if count > 1}


def test_smoke_resource_name_matches_the_registered_query_name(
    smoke_entries: list[tuple[str, str, Any]], registered_queries: dict[str, str]
) -> None:
    """resource_name is the query name registered for that file in .infrahub.yml."""
    mismatches = {
        path: (name, registered_queries.get(path))
        for name, path, _ in smoke_entries
        if registered_queries.get(path) != name
    }
    assert not mismatches


def test_every_non_fragment_query_is_registered(query_files: list[str], registered_queries: dict[str, str]) -> None:
    """A query file is registered in .infrahub.yml through its flat copy: an unregistered one has no consumer."""
    assert not [path for path in query_files if _flat(path) not in registered_queries]


def test_registered_queries_exist(root_dir: Path, registered_queries: dict[str, str]) -> None:
    """Every `queries:` entry of .infrahub.yml points at a file on disk."""
    assert not [path for path in registered_queries if not (root_dir / path).is_file()]
