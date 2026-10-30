"""Validate the passive patch panels (DcimPatchPanel, schemas/extensions/dcim/patch_panel.yml).

A panel is a set of jacks; a jack is a ``front`` and a ``rear`` DcimPatchPanelPort of the same ``name``. A cable that
lands on one continues on the cable of its pair (utils/cable_path.py).

Rules:

* P1 every port name of a panel has both faces (ERROR);
* P2 a cable that lands on a panel port continues through its pair to a cable, else the cross-connect dangles (WARNING);
* P4 the path through the panels does not come back to a port it has crossed (ERROR).

There is no P3 and no P5: a panel has no ``deployment`` and no ``position`` (its location is its optional ``rack``),
so the rule list keeps the gaps.

A pair that is absent is the P1 defect, so P2 does not repeat it.
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
from utils.cable_path import DANGLING, LOOP, trace_through_panels
from utils.data_cleaning import clean_data

_KIND = "DcimPatchPanel"
_FACES = frozenset({"front", "rear"})


def _label(node: dict[str, Any]) -> str:
    return str(node.get("name") or node.get("id") or "<unnamed>")


def _port_label(panel: dict[str, Any], port: dict[str, Any]) -> str:
    return f"{_label(panel)} {port.get('face')} {port.get('name')}"


def face_errors(panel: dict[str, Any]) -> list[str]:
    """P1: the port names of one cleaned panel that lack a face."""
    faces: dict[str, set[str]] = {}
    for port in panel.get("ports") or []:
        faces.setdefault(str(port.get("name")), set()).add(str(port.get("face")))
    return [
        f"{_label(panel)}: jack '{name}' has no {', '.join(sorted(_FACES - present))} port: a front and a rear port "
        "of one name are a pair."
        for name, present in sorted(faces.items())
        if present != _FACES
    ]


def _maps(
    panels: list[dict[str, Any]],
) -> tuple[
    dict[str, tuple[str, str, str]], dict[str, dict[str, str]], list[tuple[str, dict[str, Any], dict[str, Any]]]
]:
    """The panel ports by id, the cable on each end of every panel cable, and (cable id, panel, port) per cable end."""
    panel_ports: dict[str, tuple[str, str, str]] = {}
    cables: dict[str, dict[str, str]] = {}
    landings: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for panel in panels:
        for port in panel.get("ports") or []:
            panel_ports[port["id"]] = (panel["id"], str(port.get("face")), str(port.get("name")))
    for panel in panels:
        for port in panel.get("ports") or []:
            cable = port.get("cable")
            ends = [
                end["id"] for end in (cable or {}).get("endpoints") or [] if isinstance(end, dict) and end.get("id")
            ]
            if len(ends) != 2 or port["id"] not in ends:
                continue
            other = next(end for end in ends if end != port["id"])
            cables[port["id"]] = {"cable_id": cable["id"], "far_id": other}
            cables.setdefault(other, {"cable_id": cable["id"], "far_id": port["id"]})
            landings.append((other, panel, port))
    return panel_ports, cables, landings


def path_findings(panels: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """P2 and P4 over all cleaned panels, as (errors, warnings); one finding per stopping port."""
    panel_ports, cables, landings = _maps(panels)
    by_id = {port["id"]: (panel, port) for panel in panels for port in panel.get("ports") or []}
    errors: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, str | frozenset[str]]] = set()
    for start, _, _ in landings:
        result = trace_through_panels(start, cables, panel_ports)
        key = (result.status, frozenset(result.hops) if result.status == LOOP else result.last_port)
        if result.status not in (DANGLING, LOOP) or key in seen or result.last_port not in by_id:
            continue
        seen.add(key)
        panel, port = by_id[result.last_port]
        if result.status == LOOP:
            errors.append(f"The cross-connect loops back to {_port_label(panel, port)}.")
        else:
            warnings.append(
                f"Dangling cross-connect: {_port_label(panel, port)} has no cable, but its pair carries one."
            )
    return errors, warnings


class CheckPatchPanel(WarningCheck):
    """Panels are complete, placed, and the cross-connects through them continue and do not loop."""

    query = "patch_panel"

    def validate(self, data: Any) -> None:
        panels = clean_data(data).get(_KIND) or []
        errors: list[tuple[str, str | None]] = []
        for panel in panels:
            errors += [(message, panel.get("id")) for message in face_errors(panel)]
        path_errors, path_warnings = path_findings(panels)
        errors += [(message, None) for message in path_errors]
        for message, object_id in errors:
            self.log_error(message=message, object_id=object_id, object_type=_KIND)
        for message in path_warnings:
            self.log_warning(message)
