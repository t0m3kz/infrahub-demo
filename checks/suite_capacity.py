"""Validate that racks and pod designs fit the suite that hosts them.

A LocationSuite is the physical capacity: ``rows`` rows of ``racks_per_row`` rack slots. A rack sits at
(``row_index``, ``index``) in that grid, ``index`` being the slot in the row (a network rack carries a slot such as 5
next to a compute rack at 1). A pod's design (TopologyTemplatePod) is the envelope its racks are built within:
``max_rows`` rows of ``max_compute_racks_per_row + max_network_racks_per_row`` slots (the same limits
generators/topology/rack.py holds a rack to). Two rules:

Rack position
    1. ``row_index`` is at most the suite's ``rows``,
    2. ``index`` is at most the suite's ``racks_per_row``.
    One finding per offending rack, naming the rack, the suite and every number out of range.

Pod envelope
    The pod's design must fit every suite that hosts a rack of the pod: ``max_rows`` at most the suite's ``rows`` and
    ``max_compute_racks_per_row + max_network_racks_per_row`` at most its ``racks_per_row``. The envelope, not the
    racks present today, is the demand: a suite whose grid is full of the racks that exist but too small for the
    design leaves the pod unable to grow into what it was designed for (and a design the grid cannot hold is
    drift between the design and the room). A pod whose racks sit in several suites is judged against each one,
    because a rack position is a coordinate of the design's grid whichever suite holds the rack. One finding per
    offending pod, naming each suite it does not fit and the numbers.

Skipped, never an error: a rack without a suite, a rack without a numeric row or slot (that coordinate is not
compared), a rack hosted by nothing or by something without a design (a colocation zone is a ``TopologyRackHosting`` with no design), a pod
without racks (it hosts no suite, so there is nothing to compare).

Known limitations (the check sees the final state of the data, nothing more):

- racks are data: no generator creates a LocationRack, so what the check sees is every rack there is,
- a pod with no rack is not tied to a suite by the model, so a design too large for the room is only found once the
  first rack of the pod is placed.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

RACK = "LocationRack"
POD = "TopologyPod"


def _is_number(value: Any) -> bool:
    """A usable coordinate: a real integer (a bool is no position)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _suite_label(suite: dict[str, Any]) -> str:
    """``'name' (R rows, P racks per row)``, for a message."""
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
    """Pod id -> (the pod, the suites hosting a rack of it, each once) for every pod that has a design and racks."""
    pods: dict[str, tuple[dict[str, Any], dict[str, dict[str, Any]]]] = {}
    for rack in racks:
        pod, suite = rack.get("pod"), rack.get("parent")
        if not pod or not pod.get("design") or not suite:
            continue
        pods.setdefault(str(pod.get("id")), (pod, {}))[1].setdefault(str(suite.get("id")), suite)
    return {pod_id: (pod, list(suites.values())) for pod_id, (pod, suites) in pods.items()}
