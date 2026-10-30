"""Follow a cable through passive patch panels (pure, no SDK).

A DcimPatchPanel is not a device. A jack has two faces, each a DcimPatchPanelPort carrying one cable, and the pair is the
only rule of v1: the ``front`` port and the ``rear`` port of the SAME ``name`` in one panel. A cable that lands on one of
them continues on the cable of its pair (schemas/extensions/dcim/patch_panel.yml).

``trace_through_panels`` walks from a start port along its cable, jumps through every panel it meets and stops at the
first endpoint that is not a panel port. It reads two maps and nothing else, so a caller feeds it from any source:

* ``cables``: port id -> ``{"cable_id": ..., "far_id": ...}``, the cable on the port and the port at its other end;
* ``panel_ports``: panel port id -> ``(panel_id, face, name)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

OK = "ok"
DANGLING = "dangling"
LOOP = "loop"
MISSING_PAIR = "missing_pair"

_OPPOSITE = {"front": "rear", "rear": "front"}


@dataclass(frozen=True)
class PathResult:
    """The outcome of one walk.

    ``hops`` are the cable ids crossed, in order. ``terminal`` is the first non-panel endpoint (status ``ok``, else
    None). ``last_port`` is the port the walk stopped at: the panel port whose pair is uncabled (``dangling``), the panel
    port with no pair (``missing_pair``), the port reached twice (``loop``), or the terminal.
    """

    hops: tuple[str, ...]
    terminal: str | None
    status: str
    last_port: str


def trace_through_panels(
    start_port_id: str,
    cables: Mapping[str, Mapping[str, str]],
    panel_ports: Mapping[str, tuple[str, str, str]],
) -> PathResult:
    """Walk from ``start_port_id`` along its cable, through the panels, to the first non-panel endpoint."""
    by_jack = {(panel, face, name): port_id for port_id, (panel, face, name) in panel_ports.items()}
    hops: list[str] = []
    visited = {start_port_id}
    current = start_port_id
    while True:
        cable = cables.get(current)
        if cable is None:
            return PathResult(tuple(hops), None, DANGLING, current)
        hops.append(cable["cable_id"])
        far = cable["far_id"]
        if far in visited:
            return PathResult(tuple(hops), None, LOOP, far)
        visited.add(far)
        if far not in panel_ports:
            return PathResult(tuple(hops), far, OK, far)
        panel, face, name = panel_ports[far]
        pair = by_jack.get((panel, _OPPOSITE.get(face, ""), name))
        if pair is None:
            return PathResult(tuple(hops), None, MISSING_PAIR, far)
        if pair in visited:
            return PathResult(tuple(hops), None, LOOP, pair)
        visited.add(pair)
        current = pair
