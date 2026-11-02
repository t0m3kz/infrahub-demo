"""Which front port of a patch panel belongs on which rear port, derived from the ports alone (pure).

An object template creates a panel's ports but cannot point a front port at its rear port, so the pairing is
written afterwards (generators/topology/patch_panel.py) and judged by checks/patch_panel_pairing.py. Both read the
same plan: the layout is not a field, it is what the ports say.

- jack: as many front ports as rear ports, same numbers: front N rides on rear N.
- cassette: every rear port is MPO-12 and the front ports are a multiple F (more than one) of the rear ports: rear k
  takes front ports (k-1)*F+1 .. k*F, in numeric order.
- anything else (unequal counts, a name that is no integer, a repeated number) has no plan.

Ports are dicts of cleaned query data: ``id``, ``name``, ``connector`` and, on a front port, ``rear_port`` (the rear
port it names: ``{"id": ...}`` or an id, None when unpaired).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LAYOUT_EMPTY = "empty"
LAYOUT_JACK = "jack"
LAYOUT_CASSETTE = "cassette"
LAYOUT_UNKNOWN = "unknown"

CASSETTE_CONNECTOR = "mpo-12"


@dataclass(frozen=True)
class Pairing:
    """The plan of one panel: its layout and, for a derivable layout, front port id -> rear port id."""

    layout: str
    wanted: dict[str, str] = field(default_factory=dict)
    problem: str | None = None


def _numbered(ports: list[dict[str, Any]]) -> tuple[list[tuple[int, dict[str, Any]]], str | None]:
    """The ports sorted by their integer name, or the reason they cannot be (a non-integer or a repeated number)."""
    numbered: list[tuple[int, dict[str, Any]]] = []
    for port in ports:
        name = str(port.get("name"))
        if not name.isdecimal():
            return [], f"port name {name!r} is not a number"
        numbered.append((int(name), port))
    numbered.sort(key=lambda item: item[0])
    numbers = [number for number, _ in numbered]
    if len(set(numbers)) != len(numbers):
        return [], "two ports share a number"
    return numbered, None


def rear_port_id(front: dict[str, Any]) -> str | None:
    """The id of the rear port a front port names (cleaned query data holds ``{"id": ...}``), or None."""
    current = front.get("rear_port")
    return current.get("id") if isinstance(current, dict) else current


def plan_pairing(rears: list[dict[str, Any]], fronts: list[dict[str, Any]]) -> Pairing:
    """The layout of a panel and the rear port each front port belongs on."""
    if not rears and not fronts:
        return Pairing(LAYOUT_EMPTY)
    if not rears or not fronts:
        return Pairing(LAYOUT_UNKNOWN, problem=f"{len(rears)} rear and {len(fronts)} front ports (one side is missing)")
    rear_list, problem = _numbered(rears)
    front_list, front_problem = _numbered(fronts)
    if problem or front_problem:
        return Pairing(LAYOUT_UNKNOWN, problem=problem or front_problem)
    if len(front_list) == len(rear_list):
        if [number for number, _ in front_list] != [number for number, _ in rear_list]:
            return Pairing(LAYOUT_UNKNOWN, problem="front and rear port numbers differ")
        wanted = {front["id"]: rear["id"] for (_, front), (_, rear) in zip(front_list, rear_list, strict=True)}
        return Pairing(LAYOUT_JACK, wanted)
    per_rear, remainder = divmod(len(front_list), len(rear_list))
    if remainder or per_rear < 2 or any(rear.get("connector") != CASSETTE_CONNECTOR for _, rear in rear_list):
        return Pairing(
            LAYOUT_UNKNOWN,
            problem=f"{len(front_list)} front ports on {len(rear_list)} rear ports is neither a jack panel nor "
            f"a {CASSETTE_CONNECTOR} cassette panel",
        )
    wanted = {front["id"]: rear_list[index // per_rear][1]["id"] for index, (_, front) in enumerate(front_list)}
    return Pairing(LAYOUT_CASSETTE, wanted)


def pairing_changes(pairing: Pairing, fronts: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """(to write, in conflict) as front port id -> rear port id.

    To write: an unpaired front port the plan pairs. In conflict: a front port already on another rear port, which
    is left alone (a hand edit; the check reports it).
    """
    to_write: dict[str, str] = {}
    conflicts: dict[str, str] = {}
    for front in fronts:
        wanted = pairing.wanted.get(front["id"])
        current = rear_port_id(front)
        if wanted is None or current == wanted:
            continue
        if current is None:
            to_write[front["id"]] = wanted
        else:
            conflicts[front["id"]] = wanted
    return to_write, conflicts
