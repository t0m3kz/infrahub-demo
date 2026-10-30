"""Unit tests for CheckPatchPanel (checks/patch_panel.py).

The payloads are raw GraphQL responses shaped like queries/validation/patch_panel.gql, run through clean_data; the query
tree pins every emitted field to a selected one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.patch_panel import CheckPatchPanel
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "patch_panel.gql"


def _port(panel: str, face: str, name: str, cable: tuple[str, str] | None = None) -> dict[str, Any]:
    """A raw port; ``cable`` is (cable id, id of the other end), the port itself being the first endpoint."""
    port_id = f"{panel}-{face[0]}{name}"
    node: dict[str, Any] = {
        "id": port_id,
        "name": {"value": name},
        "face": {"value": face},
        "cable": {"node": None},
    }
    if cable:
        cable_id, other = cable
        node["cable"] = {
            "node": {"id": cable_id, "endpoints": {"edges": [{"node": {"id": port_id}}, {"node": {"id": other}}]}}
        }
    return {"node": node}


def _panel(
    name: str,
    ports: list[dict[str, Any]],
    rack: bool = False,
) -> dict[str, Any]:
    """A raw DcimPatchPanel row."""
    return {
        "node": {
            "id": name,
            "__typename": "DcimPatchPanel",
            "name": {"value": name},
            "rack": {"node": {"id": "rack-1"} if rack else None},
            "ports": {"edges": ports},
        }
    }


def _jack(panel: str, name: str, front: tuple[str, str] | None = None, rear: tuple[str, str] | None = None) -> list:
    return [_port(panel, "front", name, front), _port(panel, "rear", name, rear)]


def _run(*panels: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """(errors, warnings) of the check over raw panels."""
    payload = {"DcimPatchPanel": {"edges": list(panels)}}
    assert unselected(payload, query_tree(QUERY)) == []
    check = cast(Any, CheckPatchPanel.__new__(CheckPatchPanel))
    errors: list[dict[str, Any]] = []
    infos: list[str] = []
    check.log_error = lambda **kwargs: errors.append(kwargs)
    check.log_info = lambda message: infos.append(message)
    check.validate(payload)
    return errors, [m for m in infos if m.startswith("WARNING: ")]


def test_a_complete_cross_connect_through_a_panel_passes() -> None:
    """A -- front 1 | rear 1 -- B: nothing to report."""
    ports = _jack("pp", "1", front=("c1", "A"), rear=("c2", "B"))

    assert _run(_panel("pp", ports)) == ([], [])


def test_a_panel_without_cables_passes() -> None:
    """An unused panel is complete."""
    assert _run(_panel("pp", _jack("pp", "1") + _jack("pp", "2"))) == ([], [])


def test_p1_a_name_with_one_face_is_an_error() -> None:
    """P1: rear 2 is missing."""
    errors, _ = _run(_panel("pp", _jack("pp", "1") + [_port("pp", "front", "2")]))

    assert len(errors) == 1
    assert "jack '2' has no rear port" in errors[0]["message"] and errors[0]["object_id"] == "pp"


def test_p2_a_cable_whose_pair_is_not_cabled_is_a_warning() -> None:
    """P2: A reaches front 1 and rear 1 has no cable."""
    errors, warnings = _run(_panel("pp", _jack("pp", "1", front=("c1", "A"))))

    assert errors == []
    assert len(warnings) == 1 and "Dangling cross-connect: pp rear 1 has no cable" in warnings[0]


def test_p2_the_dangling_end_is_reported_once_for_a_chain() -> None:
    """Two panels in series ending in an uncabled jack: one finding, however many cables lead to it."""
    first = _jack("pp1", "1", front=("c1", "A"), rear=("c2", "pp2-f1"))
    second = [_port("pp2", "front", "1", ("c2", "pp1-r1")), _port("pp2", "rear", "1")]

    errors, warnings = _run(_panel("pp1", first), _panel("pp2", second))

    assert errors == [] and len(warnings) == 1 and "pp2 rear 1" in warnings[0]


def test_p4_front_patched_to_rear_of_the_same_jack_is_a_loop_error() -> None:
    """P4: front 1 -- rear 1 of one panel."""
    ports = [_port("pp", "front", "1", ("c1", "pp-r1")), _port("pp", "rear", "1", ("c1", "pp-f1"))]

    errors, warnings = _run(_panel("pp", ports))

    assert warnings == [] and len(errors) == 1 and "loops back" in errors[0]["message"]


def test_the_absent_pair_is_p1_only_not_also_a_dangling_warning() -> None:
    """A cable on front 2 whose rear 2 does not exist: the P1 error, no second finding."""
    ports = [_port("pp", "front", "2", ("c1", "A"))]

    errors, warnings = _run(_panel("pp", ports))

    assert len(errors) == 1 and warnings == []
