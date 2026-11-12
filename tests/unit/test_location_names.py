"""A suite and a rack are named in the data, not computed by the server."""

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


def _objects(node: Any, enclosing: str | None = None) -> Iterator[tuple[str, dict[str, Any], str | None]]:
    """(kind, object, parent shortname) for every suite and rack object under ``node``, wherever it is nested."""
    if isinstance(node, dict):
        data = node.get("data")
        if node.get("kind") in _LOCATION_KINDS and isinstance(data, list):
            for obj in data:
                if isinstance(obj, dict):
                    yield node["kind"], obj, obj.get("parent") or enclosing
        inner = node.get("shortname") if isinstance(node.get("shortname"), str) else enclosing
        for value in node.values():
            yield from _objects(value, inner)
    elif isinstance(node, list):
        for value in node:
            yield from _objects(value, enclosing)


def _location_objects() -> list[tuple[str, str, dict[str, Any], str | None]]:
    """(file, kind, object, parent shortname) of every suite and rack object file under ``data/``."""
    found: list[tuple[str, str, dict[str, Any], str | None]] = []
    for path in sorted((_ROOT / "data").rglob("*.y*ml")):
        for document in yaml.safe_load_all(path.read_text()):
            if isinstance(document, dict) and document.get("kind") == "Object":
                found.extend(
                    (str(path.relative_to(_ROOT)), kind, obj, parent)
                    for kind, obj, parent in _objects(document["spec"])
                )
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
    missing = [f"{path}: {kind} {obj.get('shortname')}" for path, kind, obj, _ in found if not obj.get("name")]
    assert missing == []
    assert [path for path, _, obj, _ in found if "suite_name" in obj] == []


def test_shortnames_are_unique_within_a_demo_and_names_within_their_parent() -> None:
    """`shortname` is the global identifier of a suite or a rack;"""
    shortnames: dict[tuple[str, str], list[str]] = {}
    names: dict[tuple[str, str, str | None], list[str]] = {}
    for path, kind, obj, parent in _location_objects():
        demo = "/".join(path.split("/")[:3])
        shortnames.setdefault((demo, kind), []).append(obj["shortname"])
        names.setdefault((demo, kind, parent), []).append(obj["name"])

    duplicate_shortnames = {key: sorted(n for n in v if v.count(n) > 1) for key, v in shortnames.items()}
    duplicate_names = {key: sorted(n for n in v if v.count(n) > 1) for key, v in names.items()}
    assert {key: dup for key, dup in duplicate_shortnames.items() if dup} == {}
    assert {key: dup for key, dup in duplicate_names.items() if dup} == {}


def test_a_name_may_repeat_under_different_parents() -> None:
    """The point of the rule: the same rack name under two suites is not a duplicate."""
    objects = [
        ("data/demos/x/a.yml", "LocationRack", {"shortname": "s1-r1", "name": "R1-1"}, "s1"),
        ("data/demos/x/a.yml", "LocationRack", {"shortname": "s2-r1", "name": "R1-1"}, "s2"),
    ]
    names: dict[tuple[str, str, str | None], list[str]] = {}
    for path, kind, obj, parent in objects:
        names.setdefault(("/".join(path.split("/")[:3]), kind, parent), []).append(obj["name"])

    assert all(len(v) == len(set(v)) for v in names.values())


def test_the_parent_of_a_suite_and_a_rack_is_mandatory_so_a_constraint_can_use_it() -> None:
    """The schema overrides the optional hierarchical `parent` as a mandatory Hierarchy relationship."""
    document = yaml.safe_load((_ROOT / "schemas/extensions/location/location.yml").read_text())
    nodes = _location_schema()
    overrides = {
        extension["kind"]: next(r for r in extension["relationships"] if r["name"] == "parent")
        for extension in document["extensions"]["nodes"]
        if extension["kind"] in ("LocationSuite", "LocationRack")
    }

    assert set(overrides) == {"LocationSuite", "LocationRack"}
    for kind, parent in overrides.items():
        assert (parent["kind"], parent["optional"], parent["peer"]) == ("Hierarchy", False, "LocationGeneric"), kind
        assert ["parent", "name__value"] in nodes[kind.removeprefix("Location")]["uniqueness_constraints"], kind
