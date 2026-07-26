"""Smoke tests for GraphQL fragment resolution in config queries.

These tests make the fragment behavior explicit:
- A single query file can contain unresolved spreads when checked in isolation.
- The same query resolves correctly when repository fragments are loaded.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INFRAHUB_MANIFEST = PROJECT_ROOT / ".infrahub.yml"
QUERIES_CONFIG_DIR = PROJECT_ROOT / "queries" / "config"

_FRAGMENT_DEF_RE = re.compile(r"fragment\s+([A-Za-z_][A-Za-z0-9_]*)\s+on\s+")
_FRAGMENT_SPREAD_RE = re.compile(r"\.\.\.\s*([A-Za-z_][A-Za-z0-9_]*)")


def _collect_gql_files(paths: Iterable[Path]) -> list[Path]:
    """Collect `.gql` files from file or directory paths."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.gql")))
        elif path.is_file() and path.suffix == ".gql":
            files.append(path)
    return files


def _load_global_fragment_files() -> list[Path]:
    """Load fragment files declared in `.infrahub.yml` under `graphql_fragments`."""
    manifest = yaml.safe_load(INFRAHUB_MANIFEST.read_text())
    entries = manifest.get("graphql_fragments", [])
    declared_paths = [PROJECT_ROOT / entry["file_path"] for entry in entries]
    return _collect_gql_files(declared_paths)


def _fragment_definitions(files: Iterable[Path]) -> set[str]:
    """Return all fragment names defined across the given files."""
    names: set[str] = set()
    for file_path in files:
        names.update(_FRAGMENT_DEF_RE.findall(file_path.read_text()))
    return names


def _fragment_spreads(text: str) -> set[str]:
    """Return all fragment spread names from a GraphQL document string."""
    return {name for name in _FRAGMENT_SPREAD_RE.findall(text) if name != "on"}


def _unresolved_spreads(query_file: Path, additional_defs: set[str]) -> set[str]:
    """Compute unresolved fragment spreads for a query file."""
    text = query_file.read_text()
    local_defs = set(_FRAGMENT_DEF_RE.findall(text))
    spreads = _fragment_spreads(text)
    known_defs = local_defs | additional_defs
    return {name for name in spreads if name not in known_defs}


def test_leaf_standalone_reports_external_fragment_dependency() -> None:
    """Leaf query should expose external fragment dependencies when checked alone."""
    leaf_query = QUERIES_CONFIG_DIR / "leaf.gql"
    unresolved = _unresolved_spreads(leaf_query, additional_defs=set())

    assert "InterfaceCapabilitiesWithSegmentFields" in unresolved
    assert "DeviceCapabilitiesOnDeviceFields" in unresolved


def test_config_queries_resolve_against_manifest_fragments() -> None:
    """All config queries should resolve when manifest-declared fragments are included."""
    global_fragment_defs = _fragment_definitions(_load_global_fragment_files())

    unresolved_by_file: dict[str, list[str]] = {}
    for query_file in sorted(QUERIES_CONFIG_DIR.glob("*.gql")):
        unresolved = sorted(_unresolved_spreads(query_file, additional_defs=global_fragment_defs))
        if unresolved:
            unresolved_by_file[str(query_file.relative_to(PROJECT_ROOT))] = unresolved

    assert not unresolved_by_file, f"Unresolved fragment spreads: {unresolved_by_file}"
