"""Check that racks and pod designs fit the suite that hosts them.

Racks lie inside the suite's rows and racks_per_row; the pod design's envelope fits every hosting suite.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

RACK = "LocationRack"
POD = "TopologyPod"


def _is_number(value: Any) -> bool:
    """A real integer (a bool is no position)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _suite_label(suite: dict[str, Any]) -> str:
    """``'name' (R rows, P racks per row)``."""
    name = suite.get("name") or suite.get("id") or "<unnamed-suite>"
    return f"'{name}' ({suite['rows']} rows, {suite['racks_per_row']} racks per row)"


class CheckSuiteCapacity(InfrahubCheck):
    """Every rack lies inside its suite's grid and every pod's design fits the suites hosting its racks (ERRORs)."""

    query = "suite_capacity"

    def validate(self, data: Any) -> None:
        racks = clean_data(data).get(RACK) or []
        for rack in racks:
            self._validate_position(rack)
        for pod, suites in _suites_by_pod(racks).values():
            self._validate_envelope(pod, suites)

    def _validate_position(self, rack: dict[str, Any]) -> None:
        """Rule 1 and 2: the rack's row and slot are within its suite's rows and racks per row."""
        suite = rack.get("parent")
        if not suite:
            return
        problems = []
        row, slot = rack.get("row_index"), rack.get("index")
        if _is_number(row) and row > suite["rows"]:
            problems.append(f"row {row} is beyond the suite's {suite['rows']} rows")
        if _is_number(slot) and slot > suite["racks_per_row"]:
            problems.append(f"position {slot} is beyond the suite's {suite['racks_per_row']} racks per row")
        if problems:
            self.log_error(
                message=f"Rack '{rack.get('name') or rack.get('id')}' does not fit suite {_suite_label(suite)}: "
                + "; ".join(problems),
                object_id=rack.get("id"),
                object_type=RACK,
            )

    def _validate_envelope(self, pod: dict[str, Any], suites: list[dict[str, Any]]) -> None:
        """The pod's design envelope fits every suite hosting a rack of the pod."""
        design = pod["design"]
        rows = design["max_rows"]
        slots = design["max_compute_racks_per_row"] + design["max_network_racks_per_row"]
        misfits = []
        for suite in suites:
            problems = []
            if rows > suite["rows"]:
                problems.append(f"{rows} rows")
            if slots > suite["racks_per_row"]:
                problems.append(f"{slots} racks per row")
            if problems:
                misfits.append(f"{_suite_label(suite)} cannot hold {' and '.join(problems)}")
        if misfits:
            self.log_error(
                message=(
                    f"Pod '{pod.get('name') or pod.get('id')}' design '{design.get('name') or '<unnamed-design>'}' "
                    f"needs {rows} rows of {slots} racks per row "
                    f"({design['max_compute_racks_per_row']} compute + {design['max_network_racks_per_row']} network), "
                    f"which does not fit: " + "; ".join(misfits)
                ),
                object_id=pod.get("id"),
                object_type=POD,
            )


def _suites_by_pod(racks: list[dict[str, Any]]) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Pod id -> (pod, suites hosting its racks) for every pod with a design and racks."""
    pods: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
    for rack in racks:
        pod, suite = rack.get("deployment"), rack.get("parent")
        if not pod or not pod.get("design") or not suite:
            continue
        pods.setdefault(str(pod.get("id")), (pod, {}))[1].setdefault(str(suite.get("id")), suite)
    return {pod_id: (pod, list(suites.values())) for pod_id, (pod, suites) in pods.items()}
