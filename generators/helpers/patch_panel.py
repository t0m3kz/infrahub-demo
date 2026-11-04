"""What a patch panel of a type holds, and how its ports differ from it (pure).

A panel names its type (`DcimPatchPanel.panel_type`, a Dropdown whose choices are the keys of PANEL_TYPES). The type
is the whole layout: how many rear ports of which connector, and how many front ports ride on each. add_patch_panel
(generators/topology/patch_panel.py) builds the ports the type asks for, each front port on its rear port, and
checks/patch_panel_pairing.py reports what still differs. Both read audit().

- jack (one front port per rear port): front N rides on rear N.
- cassette (F front ports per MPO rear port): rear k takes the front ports (k-1)*F+1 .. k*F.

Ports are dicts of cleaned query data: ``id``, ``name``, ``connector`` and, on a front port, ``rear_port`` (the rear
port it names: ``{"id": ...}`` or an id, None when unpaired). Port names are their numbers, from 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LAYOUT_JACK = "jack"
LAYOUT_CASSETTE = "cassette"


@dataclass(frozen=True)
class PanelType:
    """The ports of one panel type: ``rears`` rear ports, each carrying ``fronts_per_rear`` front ports."""

    rears: int
    rear_connector: str
    fronts_per_rear: int
    front_connector: str

    @property
    def fronts(self) -> int:
        return self.rears * self.fronts_per_rear

    @property
    def layout(self) -> str:
        return LAYOUT_JACK if self.fronts_per_rear == 1 else LAYOUT_CASSETTE

    def rear_of(self, front: int) -> int:
        """The number of the rear port front port ``front`` rides on."""
        return (front - 1) // self.fronts_per_rear + 1


# The choices of DcimPatchPanel.panel_type (schemas/extensions/dcim/patch_panel.yml; tests/unit/test_patch_panel_schema.py
# holds the two equal).
PANEL_TYPES: dict[str, PanelType] = {
    "lc-24": PanelType(24, "lc-upc", 1, "lc-upc"),
    "lc-48": PanelType(48, "lc-upc", 1, "lc-upc"),
    "lc-96": PanelType(96, "lc-upc", 1, "lc-upc"),
    "sc-12": PanelType(12, "sc-upc", 1, "sc-upc"),
    "rj45-24": PanelType(24, "rj45", 1, "rj45"),
    "rj45-48": PanelType(48, "rj45", 1, "rj45"),
    "mpo12-cassette-24": PanelType(2, "mpo-12", 12, "lc-upc"),
    "mpo12-cassette-48": PanelType(4, "mpo-12", 12, "lc-upc"),
    "mpo12-cassette-96": PanelType(8, "mpo-12", 12, "lc-upc"),
}


@dataclass
class PanelAudit:
    """How the ports of one panel differ from its type.

    ``missing_rears`` / ``missing_fronts`` hold the port numbers to create; ``to_pair`` an unpaired front port id ->
    the number of its rear port; ``misplaced`` front port names on another rear port; ``extra`` port names the type
    does not have ("rear 25", "front 25"); ``wrong_connector`` the same for a connector that differs. ``problem`` is
    set when there is no type to compare with (unknown type, a port name that is no number, a repeated number).
    """

    panel_type: PanelType | None = None
    problem: str | None = None
    missing_rears: list[int] = field(default_factory=list)
    missing_fronts: list[int] = field(default_factory=list)
    to_pair: dict[str, int] = field(default_factory=dict)
    misplaced: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    wrong_connector: list[str] = field(default_factory=list)


def _by_number(ports: list[dict[str, Any]], side: str) -> tuple[dict[int, dict[str, Any]], str | None]:
    """The ports by their integer name, or the reason they cannot be keyed so."""
    numbered: dict[int, dict[str, Any]] = {}
    for port in ports:
        name = str(port.get("name"))
        if not name.isdecimal():
            return {}, f"{side} port name {name!r} is not a number"
        if int(name) in numbered:
            return {}, f"two {side} ports share the number {name}"
        numbered[int(name)] = port
    return numbered, None


def rear_port_id(front: dict[str, Any]) -> str | None:
    """The id of the rear port a front port names (cleaned query data holds ``{"id": ...}``), or None."""
    current = front.get("rear_port")
    return current.get("id") if isinstance(current, dict) else current


def audit(type_name: str | None, rears: list[dict[str, Any]], fronts: list[dict[str, Any]]) -> PanelAudit:
    """Compare the ports of a panel with what its type ``type_name`` holds."""
    panel_type = PANEL_TYPES.get(type_name or "")
    if panel_type is None:
        return PanelAudit(problem=f"panel_type {type_name!r} is not one of {', '.join(PANEL_TYPES)}")
    rear_by, problem = _by_number(rears, "rear")
    front_by, front_problem = _by_number(fronts, "front")
    if problem or front_problem:
        return PanelAudit(panel_type, problem=problem or front_problem)
    result = PanelAudit(panel_type)
    result.missing_rears = [n for n in range(1, panel_type.rears + 1) if n not in rear_by]
    result.missing_fronts = [n for n in range(1, panel_type.fronts + 1) if n not in front_by]
    result.extra = [f"rear {n}" for n in sorted(rear_by) if n > panel_type.rears] + [
        f"front {n}" for n in sorted(front_by) if n > panel_type.fronts
    ]
    result.wrong_connector = [
        f"rear {n}" for n, port in sorted(rear_by.items()) if port.get("connector") != panel_type.rear_connector
    ] + [f"front {n}" for n, port in sorted(front_by.items()) if port.get("connector") != panel_type.front_connector]
    rear_ids = {port["id"]: n for n, port in rear_by.items()}
    for n, front in sorted(front_by.items()):
        if n > panel_type.fronts:
            continue
        current = rear_port_id(front)
        if current is None:
            result.to_pair[front["id"]] = panel_type.rear_of(n)
        elif rear_ids.get(current) != panel_type.rear_of(n):
            result.misplaced.append(str(n))
    return result
