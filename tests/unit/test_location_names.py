"""A suite and a rack are named in the data, not computed by the server.

A computed name changed silently when the suite, the row or the slot changed, and the data cannot name an object it
cannot spell. Both names are plain, required attributes; ``suite_name`` is gone with the Jinja it fed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_LOCATION_KINDS = ("LocationSuite", "LocationRack")


def _location_schema() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((_ROOT / "schemas/extensions/location/location.yml").read_text())
    return {node["name"]: node for node in document["nodes"]}


def _objects(node: Any) -> Iterator[tuple[str, dict[str, Any]]]:
    """(kind, object) for every suite and rack object under ``node``, wherever it is nested.

    An object file nests objects as ``{kind, data: [...]}`` under ``children`` (a suite's racks), under a relationship
    such as ``racks`` (a pod's racks), or as the document's own ``spec``; walking every mapping finds all three."""
    if isinstance(node, dict):
        data = node.get("data")
        if node.get("kind") in _LOCATION_KINDS and isinstance(data, list):
            for obj in data:
                if isinstance(obj, dict):
                    yield node["kind"], obj
        for value in node.values():
            yield from _objects(value)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value)


def _location_objects() -> list[tuple[str, str, dict[str, Any]]]:
    """(file, kind, object) of every suite and rack object file under ``data/``."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for path in sorted((_ROOT / "data").rglob("*.y*ml")):
        for document in yaml.safe_load_all(path.read_text()):
            if isinstance(document, dict) and document.get("kind") == "Object":
                found.extend((str(path.relative_to(_ROOT)), kind, obj) for kind, obj in _objects(document["spec"]))
    return found


@pytest.mark.parametrize("node", ["Suite", "Rack"])
def test_a_suite_and_a_rack_have_a_plain_required_name(node: str) -> None:
    """The name is neither computed nor read-only, and a suite has no ``suite_name`` to compute it from."""
    attributes = {a["name"]: a for a in _location_schema()[node]["attributes"]}

    assert attributes["name"]["optional"] is False
    assert "computed_attribute" not in attributes["name"]
    assert not attributes["name"].get("read_only")
    assert "suite_name" not in attributes


def test_every_suite_and_rack_in_the_data_carries_its_name() -> None:
    """Nothing in the data relies on the server to name a suite or a rack."""
    found = _location_objects()

    assert found, "no suite or rack found: the data walk is broken"
    missing = [f"{path}: {kind} {obj.get('shortname')}" for path, kind, obj in found if not obj.get("name")]
    assert missing == []
    assert [path for path, _, obj in found if "suite_name" in obj] == []


def test_rack_and_suite_names_are_unique_within_a_demo() -> None:
    """Two racks (or suites) of one demo never share a name: the rack keeps its ``name`` uniqueness constraint.

    A demo is a directory under ``data/demos``; the demos are loaded one at a time, so they may reuse a name."""
    seen: dict[tuple[str, str], list[str]] = {}
    for path, kind, obj in _location_objects():
        demo = "/".join(path.split("/")[:3])
        seen.setdefault((demo, kind), []).append(obj["name"])

    duplicates = {key: sorted(n for n in names if names.count(n) > 1) for key, names in seen.items()}
    assert {key: dup for key, dup in duplicates.items() if dup} == {}
