"""Guard on the YAML input: a transport must not ride on itself."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.unit.demo_yaml_helpers import walk_objects

ROOT = Path(__file__).resolve().parents[2]
KIND = "CloudDirectConnect"


def yaml_documents(directory: Path) -> list[Any]:
    """The `spec` of every document of the object files under a directory."""
    specs: list[Any] = []
    for path in sorted([*directory.rglob("*.yml"), *directory.rglob("*.yaml")]):
        specs += [doc.get("spec", {}) for doc in yaml.safe_load_all(path.read_text()) if isinstance(doc, dict)]
    return specs


def _target(rides_on: Any) -> str | None:
    """The name a ``rides_on`` value points at: an HFID string, a one-item HFID list or a nested object."""
    if isinstance(rides_on, str):
        return rides_on
    if isinstance(rides_on, list) and len(rides_on) == 1:
        return str(rides_on[0])
    if isinstance(rides_on, dict) and isinstance(rides_on.get("data"), dict) and "name" in rides_on["data"]:
        return str(rides_on["data"]["name"])
    return None


def declared_rides_on(documents: list[Any]) -> dict[str, str]:
    """{transport name: name it rides on} for every transport of the object-file specs that declares one."""
    found: list[tuple[str, dict[str, Any]]] = []
    for document in documents:
        walk_objects(document, "", found)
    return {
        str(entry["name"]): target
        for kind, entry in found
        if kind == KIND and "name" in entry and (target := _target(entry.get("rides_on")))
    }


def _self_riding(documents: list) -> list[str]:
    return sorted(name for name, target in declared_rides_on(documents).items() if name == target)


def test_no_transport_in_the_data_rides_on_itself() -> None:
    """Every object file under data/ and tests/integration/data/ passes."""
    offenders = {}
    for base in (ROOT / "data", ROOT / "tests" / "integration" / "data"):
        for directory in sorted({p.parent for p in [*base.rglob("*.yml"), *base.rglob("*.yaml")]}):
            bad = _self_riding(yaml_documents(directory))
            if bad:
                offenders[str(directory.relative_to(ROOT))] = bad
    assert not offenders, f"rides_on points at the transport itself: {offenders}"


def test_a_transport_riding_on_itself_is_found() -> None:
    """The string form of the reference."""
    spec = {"kind": KIND, "data": [{"name": "er-1", "rides_on": "er-1"}, {"name": "er-2", "rides_on": "er-1"}]}
    assert _self_riding([spec]) == ["er-1"]


def test_a_nested_or_list_reference_to_itself_is_found() -> None:
    """The nested object and the HFID list forms."""
    spec = {
        "kind": KIND,
        "data": [
            {"name": "a", "rides_on": {"kind": KIND, "data": {"name": "a"}}},
            {"name": "b", "rides_on": ["b"]},
            {"name": "c", "rides_on": ["a"]},
        ],
    }
    assert _self_riding([spec]) == ["a", "b"]


def test_a_transport_with_no_rides_on_declares_nothing() -> None:
    """No key, an empty value."""
    spec = {"kind": KIND, "data": [{"name": "x"}, {"name": "y", "rides_on": None}]}
    assert declared_rides_on([spec]) == {}
