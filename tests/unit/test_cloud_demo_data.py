"""Guard on the YAML input: a transport must not ride on itself.

The server accepts ``rides_on`` pointing at the object itself and silently drops the edge (measured on 1.11.9), so
check R3 never sees it. The only input of this repository is YAML, so this is the one place that can refuse it.
"""

from __future__ import annotations

from pathlib import Path

from tests.traversal.walks import declared_rides_on, yaml_documents

ROOT = Path(__file__).resolve().parents[2]
KIND = "CloudDirectConnect"


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
