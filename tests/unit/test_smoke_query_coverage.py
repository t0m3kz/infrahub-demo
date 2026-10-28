"""Guard: every query under queries/ is covered by a smoke `syntax_check` entry in tests/smoke/test_graphql.yml.

The smoke entry is an OFFLINE parse (`GraphQLQueryAnalyzer`, no schema); the live schema check is the
repository sync of `uv run invoke test-integration-fast`. Without this guard a query added to `.infrahub.yml` but not to the smoke file is
not even parsed by `invoke validate`.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from infrahub_sdk.analyzer import GraphQLQueryAnalyzer

SMOKE_KIND = "graphql-query-smoke"
SMOKE_TEST_NAME = "syntax_check"
FRAGMENTS_DIR = "queries/fragments"


@pytest.fixture(scope="module")
def registered_queries(root_dir: Path) -> dict[str, str]:
    """Return the file_path -> name map of the `queries:` list in .infrahub.yml."""
    config = yaml.safe_load((root_dir / ".infrahub.yml").read_text())
    return {entry["file_path"]: entry["name"] for entry in config["queries"]}


@pytest.fixture(scope="module")
def query_files(root_dir: Path) -> list[str]:
    """Return every queries/**/*.gql (fragment library excluded) as a repo-relative POSIX path."""
    return sorted(
        path.relative_to(root_dir).as_posix()
        for path in (root_dir / "queries").rglob("*.gql")
        if FRAGMENTS_DIR not in path.relative_to(root_dir).as_posix()
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
    """A query file without a `syntax_check` entry is never parsed by `invoke validate`."""
    covered = {path for _, path, _ in smoke_entries}
    assert not [path for path in query_files if path not in covered]


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
    """A query file is registered in .infrahub.yml: an unregistered one has no consumer."""
    assert not [path for path in query_files if path not in registered_queries]


def test_registered_queries_exist(root_dir: Path, registered_queries: dict[str, str]) -> None:
    """Every `queries:` entry of .infrahub.yml points at a file on disk."""
    assert not [path for path in registered_queries if not (root_dir / path).is_file()]


def test_every_query_file_parses_offline(root_dir: Path, query_files: list[str]) -> None:
    """The analyzer the smoke item uses accepts each query on its own (fragment spreads need not be defined)."""
    failures: dict[str, str] = {}
    for path in query_files:
        try:
            GraphQLQueryAnalyzer((root_dir / path).read_text())
        except Exception as error:  # noqa: BLE001 - report every failing file at once
            failures[path] = str(error)
    assert not failures
