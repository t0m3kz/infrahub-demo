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


def _row_and_suite(rack: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve optional LocationRackRow and parent LocationSuite from ``rack['parent']``."""
    parent = rack.get("parent") if isinstance(rack.get("parent"), dict) else None
    if not parent:
        return None, None
    if "row_type" in parent or ("rows" not in parent and isinstance(parent.get("parent"), dict)):
        suite = parent.get("parent") if isinstance(parent.get("parent"), dict) else None
        return parent, suite
    return None, parent


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
        """Rule 1 and 2: the rack's row and slot are within its suite/row grid and match row_type."""
        row_node, suite = _row_and_suite(rack)
        if not suite:
            return
        problems = []
        row = (
            rack.get("row_index")
            if rack.get("row_index") is not None
            else (row_node.get("index") if row_node else None)
        )
        slot = rack.get("index")
        max_slots = (
            row_node["racks_per_row"]
            if row_node is not None and _is_number(row_node.get("racks_per_row"))
            else suite["racks_per_row"]
        )
        if _is_number(row) and row > suite["rows"]:
            problems.append(f"row {row} is beyond the suite's {suite['rows']} rows")
        scope_label = "row's" if (row_node is not None and _is_number(row_node.get("racks_per_row"))) else "suite's"
        if _is_number(slot) and slot > max_slots:
            problems.append(f"position {slot} is beyond the {scope_label} {max_slots} racks per row")
        if row_node is not None:
            row_type = str(row_node.get("row_type") or "mixed")
            rack_type = str(rack.get("rack_type") or "")
            if row_type != "mixed" and rack_type and rack_type != row_type:
                problems.append(
                    f"rack_type '{rack_type}' does not match row '{row_node.get('name') or row}' row_type '{row_type}'"
                )
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
        _, suite = _row_and_suite(rack)
        pod = rack.get("deployment")
        if not pod or not pod.get("design") or not suite:
            continue
        pods.setdefault(str(pod.get("id")), (pod, {}))[1].setdefault(str(suite.get("id")), suite)
    return {pod_id: (pod, list(suites.values())) for pod_id, (pod, suites) in pods.items()}
