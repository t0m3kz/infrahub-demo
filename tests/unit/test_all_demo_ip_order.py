"""An IP prefix or address of 30_all is created before a file uses it, in the staged and the chain order."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import pytest
import yaml

import tasks

Position = tuple[int, int]  # (file index in the load order, document index in the file)


def _is_network(value: Any) -> bool:
    """True for an IP prefix or interface address in CIDR notation."""
    if not isinstance(value, str) or "/" not in value:
        return False
    try:
        ipaddress.ip_interface(value)
    except ValueError:
        return False
    return True


def _files(root_dir: Path, paths: tuple[str, ...]) -> list[Path]:
    """The object files of one `object load` of these paths, in the loader's (sorted) order."""
    files: list[Path] = []
    for path in paths:
        full = root_dir / tasks.ALL_DEMO_DATA / path
        files += [full] if full.is_file() else [f for pattern in ("*.yml", "*.yaml") for f in full.rglob(pattern)]
    return sorted(files)


def _staged_order(root_dir: Path) -> list[Path]:
    """Every 30_all file in the order of the staged load."""
    return [file for _, paths in tasks.ALL_DEMO_LOAD_STAGES for file in _files(root_dir, paths)]


def _chain_order(root_dir: Path) -> list[Path]:
    """The files of the chain's steps, in step order."""
    return [file for step in tasks.ALL_DEMO_CHAIN for file in _files(root_dir, step.paths)]


def _walk(node: Any, position: Position, created: dict[str, Position], used: list[tuple[str, Position]]) -> None:
    """Record every prefix/address an object creates (a `prefix` or `address` field) and every HFID reference."""
    if isinstance(node, dict):
        for key in ("prefix", "address"):
            if _is_network(node.get(key)):
                created.setdefault(str(node[key]), position)
        for value in node.values():
            _walk(value, position, created, used)
    elif isinstance(node, list):
        if len(node) == 2 and _is_network(node[0]) and isinstance(node[1], str):
            used.append((node[0], position))
            return
        for item in node:
            _walk(item, position, created, used)


def _used_before_created(files: list[Path]) -> list[str]:
    """One line per reference that loads before the object it names is created in these files."""
    created: dict[str, Position] = {}
    used: list[tuple[str, Position]] = []
    for file_index, file in enumerate(files):
        documents = [document for document in yaml.safe_load_all(file.read_text()) if document]
        for document_index, document in enumerate(documents):
            _walk(document["spec"]["data"], (file_index, document_index), created, used)
    return [
        f"{network}: used in {files[position[0]].name}, created later in {files[created[network][0]].name}"
        for network, position in used
        if network in created and created[network] > position
    ]


@pytest.mark.parametrize("order", [_staged_order, _chain_order], ids=["staged", "chain"])
def test_ip_objects_are_created_before_they_are_used(root_dir: Path, order: Any) -> None:
    """No HFID reference to a prefix or address loads before the object it names."""
    problems = _used_before_created(order(root_dir))
    assert not problems, "Create the prefix or address in an earlier file:\n" + "\n".join(f"  - {p}" for p in problems)


def test_every_referenced_ip_object_is_created_in_30_all(root_dir: Path) -> None:
    """The scan sees the references (a broken walk would pass the order test vacuously), and each names an object
    30_all creates itself, so the order test judges every one of them."""
    created: dict[str, Position] = {}
    used: list[tuple[str, Position]] = []
    for index, file in enumerate(_staged_order(root_dir)):
        for document in yaml.safe_load_all(file.read_text()):
            if document:
                _walk(document["spec"]["data"], (index, 0), created, used)
    assert len(used) > 10
    assert {network for network, _ in used} <= set(created)


def test_reference_before_creation_is_reported(tmp_path: Path) -> None:
    """A file that names a prefix before the file creating it loads is reported."""
    user = tmp_path / "01_office.yml"
    user.write_text(
        "---\nspec:\n  kind: TopologyCustomerOffice\n  data:\n    - lan_prefixes:\n        - ['10.1.0.0/24', 'default']\n"
    )
    creator = tmp_path / "02_prefixes.yml"
    creator.write_text("---\nspec:\n  kind: IpamPrefix\n  data:\n    - prefix: '10.1.0.0/24'\n")
    assert _used_before_created([user, creator]) == [
        "10.1.0.0/24: used in 01_office.yml, created later in 02_prefixes.yml"
    ]
    assert _used_before_created([creator, user]) == []
