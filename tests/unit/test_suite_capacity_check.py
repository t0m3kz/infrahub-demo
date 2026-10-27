"""Unit tests for CheckSuiteCapacity (racks and pod designs must fit the suite hosting them).

The payloads are raw GraphQL responses shaped like queries/validation/suite_capacity.gql (one ``LocationRack`` root,
each rack with its suite and the pod hosting it); ``validate`` runs them through ``clean_data``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.suite_capacity import CheckSuiteCapacity


def _suite(name: str = "SUITE-1", rows: int = 2, racks_per_row: int = 9) -> dict[str, Any]:
    """A suite node, as the query selects it."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "rows": {"value": rows},
        "racks_per_row": {"value": racks_per_row},
    }


def _design(name: str = "S_MIDDLE", rows: int = 2, compute: int = 8, network: int = 1) -> dict[str, Any]:
    """A pod design node (TopologyTemplatePod envelope), as the query selects it."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "max_rows": {"value": rows},
        "max_compute_racks_per_row": {"value": compute},
        "max_network_racks_per_row": {"value": network},
    }


def _pod(name: str = "DC1-1-POD-1", design: dict[str, Any] | None = None) -> dict[str, Any]:
    """A TopologyPod node with its design (the default S_MIDDLE: 2 rows, 8 compute + 1 network racks per row)."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "design": {"node": design if design is not None else _design()},
    }


def _zone(name: str = "FR2") -> dict[str, Any]:
    """A colocation zone hosting racks: a TopologyRackHosting without a design."""
    return {"id": f"id-{name}", "name": {"value": name}}


def _rack(
    name: str,
    row: int | None,
    index: int | None,
    suite: dict[str, Any] | None,
    pod: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A rack node at (row, index) in a suite hosted by a pod, as the query selects it."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "row_index": {"value": row},
        "index": {"value": index},
        "parent": {"node": suite},
        "deployment": {"node": pod},
    }


def _findings(*racks: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the check over the racks and return what it logged (message, object id and kind)."""
    check = cast(Any, CheckSuiteCapacity.__new__(CheckSuiteCapacity))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate({"LocationRack": {"edges": [{"node": rack} for rack in racks]}})
    return findings


def test_no_racks_gives_no_findings() -> None:
    """An empty inventory is clean."""
    assert _findings() == []


def test_rack_inside_the_grid_passes() -> None:
    """A rack well inside its suite's rows and racks per row is not reported."""
    assert _findings(_rack("R-1-1", 1, 1, _suite(rows=2, racks_per_row=9))) == []


def test_rack_on_the_last_row_and_last_slot_passes() -> None:
    """Boundary: row_index == rows and index == racks_per_row both fit."""
    assert _findings(_rack("R-2-9", 2, 9, _suite(rows=2, racks_per_row=9))) == []


def test_rack_one_row_beyond_the_suite_is_reported() -> None:
    """Boundary: row_index == rows + 1 is outside the grid; the message names rack, suite and the numbers."""
    findings = _findings(_rack("R-3-1", 3, 1, _suite("MUC-1-Suite-1", rows=2, racks_per_row=9)))

    assert len(findings) == 1
    message = findings[0]["message"]
    assert "R-3-1" in message and "MUC-1-Suite-1" in message
    assert "row 3 is beyond the suite's 2 rows" in message
    assert "position" not in message
    assert findings[0]["object_id"] == "id-R-3-1"
    assert findings[0]["object_type"] == "LocationRack"


def test_rack_one_slot_beyond_the_suite_is_reported() -> None:
    """Boundary: index == racks_per_row + 1 is outside the grid; the message names rack, suite and the numbers."""
    findings = _findings(_rack("R-1-10", 1, 10, _suite("MUC-1-Suite-1", rows=2, racks_per_row=9)))

    assert len(findings) == 1
    message = findings[0]["message"]
    assert "R-1-10" in message and "MUC-1-Suite-1" in message
    assert "position 10 is beyond the suite's 9 racks per row" in message
    assert "row 1" not in message


def test_rack_beyond_both_dimensions_gets_one_finding_with_both_reasons() -> None:
    """One finding per rack, however many numbers are out of range."""
    findings = _findings(_rack("R-5-5", 5, 5, _suite(rows=1, racks_per_row=2)))

    assert len(findings) == 1
    assert "row 5 is beyond the suite's 1 rows" in findings[0]["message"]
    assert "position 5 is beyond the suite's 2 racks per row" in findings[0]["message"]


def test_every_offending_rack_is_reported_once() -> None:
    """Two bad racks and a good one: two findings, one per bad rack."""
    suite = _suite(rows=1, racks_per_row=2)
    findings = _findings(_rack("A", 1, 3, suite), _rack("B", 1, 2, suite), _rack("C", 2, 1, suite))

    assert [f["object_id"] for f in findings] == ["id-A", "id-C"]


def test_rack_without_a_suite_is_skipped() -> None:
    """A rack with no parent suite has no grid to leave."""
    assert _findings(_rack("R-9-9", 9, 9, None)) == []


@pytest.mark.parametrize(("row", "index"), [(None, 99), (99, None), (None, None)])
def test_a_missing_coordinate_is_not_compared(row: int | None, index: int | None) -> None:
    """A rack without a row_index or an index (both optional in the schema) is compared on the one it has."""
    findings = _findings(_rack("R-X", row, index, _suite(rows=2, racks_per_row=9)))

    assert len(findings) == (row is not None) + (index is not None)


def test_pod_whose_design_fits_the_suite_passes() -> None:
    """S_MIDDLE (2 rows, 8 + 1 racks per row) in a suite of exactly 2 rows of 9 is the boundary that fits."""
    suite = _suite(rows=2, racks_per_row=9)
    assert _findings(_rack("R-1-5", 1, 5, suite, _pod())) == []


def test_pod_needing_one_row_more_than_the_suite_is_reported() -> None:
    """Boundary: design.max_rows == suite.rows + 1; the finding names pod, design, suite and the numbers."""
    suite = _suite("MUC-1-Suite-1", rows=1, racks_per_row=9)
    findings = _findings(_rack("R-1-5", 1, 5, suite, _pod("DC1-1-POD-1", _design("S_MIDDLE", rows=2))))

    assert len(findings) == 1
    message = findings[0]["message"]
    assert "DC1-1-POD-1" in message and "S_MIDDLE" in message and "MUC-1-Suite-1" in message
    assert "needs 2 rows of 9 racks per row (8 compute + 1 network)" in message
    assert "(1 rows, 9 racks per row) cannot hold 2 rows" in message
    assert "rows and" not in message
    assert findings[0]["object_id"] == "id-DC1-1-POD-1"
    assert findings[0]["object_type"] == "TopologyPod"


def test_pod_needing_one_slot_more_than_the_suite_is_reported() -> None:
    """Boundary: compute + network == racks_per_row + 1."""
    suite = _suite("SUITE-A", rows=2, racks_per_row=8)
    findings = _findings(_rack("R-1-5", 1, 5, suite, _pod()))

    assert len(findings) == 1
    assert "cannot hold 9 racks per row" in findings[0]["message"]
    assert "rows and" not in findings[0]["message"]


def test_pod_too_big_in_both_dimensions_is_one_finding_naming_both() -> None:
    """One finding per pod, both numbers in the reason."""
    suite = _suite("SUITE-A", rows=1, racks_per_row=2)
    findings = _findings(_rack("R-1-1", 1, 1, suite, _pod()))

    assert len(findings) == 1
    assert "cannot hold 2 rows and 9 racks per row" in findings[0]["message"]


def test_pod_with_many_racks_in_a_small_suite_is_reported_once() -> None:
    """The pod is the offender, not each of its racks: five racks give one pod finding (and none by position)."""
    suite = _suite("SUITE-A", rows=2, racks_per_row=2)
    racks = [_rack(f"R-1-{i}", 1, i, suite, _pod()) for i in (1, 2)] + [_rack("R-2-1", 2, 1, suite, _pod())]
    findings = _findings(*racks)

    assert [f["object_type"] for f in findings] == ["TopologyPod"]


def test_pod_spanning_suites_is_judged_against_each_suite() -> None:
    """A rack position is a coordinate of the design's grid: every hosting suite must hold the envelope."""
    big, small = _suite("BIG", rows=2, racks_per_row=9), _suite("SMALL", rows=2, racks_per_row=4)
    pod = _pod()
    findings = _findings(
        _rack("R-1-5", 1, 5, big, pod), _rack("R-2-1", 2, 1, small, pod), _rack("R-1-1", 1, 1, small, pod)
    )

    assert len(findings) == 1
    message = findings[0]["message"]
    assert "SMALL" in message and "BIG" not in message
    assert "cannot hold 9 racks per row" in message


def test_pod_spanning_two_small_suites_names_both() -> None:
    """Both suites too small: still one finding for the pod, listing each suite."""
    pod = _pod()
    findings = _findings(
        _rack("A", 1, 1, _suite("S1", rows=1, racks_per_row=9), pod),
        _rack("B", 1, 1, _suite("S2", rows=2, racks_per_row=3), pod),
    )

    assert len(findings) == 1
    assert "'S1'" in findings[0]["message"] and "'S2'" in findings[0]["message"]


def test_two_pods_in_one_suite_are_judged_separately() -> None:
    """Each pod is held to its own design: only the oversized one is reported."""
    suite = _suite("SUITE-A", rows=2, racks_per_row=9)
    small = _pod("DC1-1-POD-1", _design("S_MIDDLE"))
    large = _pod("DC1-1-POD-2", _design("M_MIDDLE", rows=4))
    findings = _findings(_rack("A", 1, 1, suite, small), _rack("B", 1, 1, suite, large))

    assert [f["object_id"] for f in findings] == ["id-DC1-1-POD-2"]
    assert "M_MIDDLE" in findings[0]["message"]


def test_rack_without_a_pod_gets_no_envelope_check() -> None:
    """Hosted by nothing: only the position is judged."""
    assert _findings(_rack("R-1-1", 1, 1, _suite(rows=1, racks_per_row=1), None)) == []


def test_rack_hosted_by_a_zone_without_a_design_gets_no_envelope_check() -> None:
    """A colocation zone has no design: its rack is judged on its position alone."""
    assert _findings(_rack("R-1-2", 1, 2, _suite("FR2", rows=1, racks_per_row=2), _zone())) == []


def test_rack_hosted_by_a_zone_is_still_judged_on_its_position() -> None:
    """No design does not exempt the position."""
    findings = _findings(_rack("R-1-3", 1, 3, _suite("FR2", rows=1, racks_per_row=2), _zone()))

    assert len(findings) == 1 and findings[0]["object_type"] == "LocationRack"


def test_pod_whose_racks_have_no_suite_is_skipped() -> None:
    """Without a suite there is nothing to compare the envelope with."""
    assert _findings(_rack("R-1-1", 1, 1, None, _pod())) == []


def test_a_design_that_fits_exactly_with_no_compute_racks_passes() -> None:
    """S_BORDER_SPINE_POD (1 row, 0 compute + 1 network) fits a 1x1 suite."""
    design = _design("S_BORDER_SPINE_POD", rows=1, compute=0, network=1)
    assert _findings(_rack("R-1-1", 1, 1, _suite(rows=1, racks_per_row=1), _pod("DC7-7-POD-1", design))) == []
