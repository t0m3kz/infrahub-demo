"""Derived attributes of a DcimCable: printed ID, jacket colour and length (pure, no SDK). ``cable_id`` is CBL- plus a
SHA-1 prefix of the sorted endpoint labels; ``length_cm`` comes from the suite grid, None whenever it would be a guess.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# TIA-598 colours by DcimCable.type; a test pins it to the schema choices
CABLE_COLOR_BY_TYPE: dict[str, str] = {
    "smf": "yellow",
    "mmf": "aqua",
    "copper": "blue",
    "dac": "black",
}

CABLE_ID_PREFIX = "CBL-"
CABLE_ID_HEX_LENGTH = 10
INTRA_RACK_CM = 100
SERVICE_LOOP_CM = 50
LENGTH_ROUNDING_CM = 50


@dataclass(frozen=True)
class RackPosition:
    """Where one cable end sits: its rack on the suite grid plus the suite's grid and pitches.
    Any figure but ``suite_id`` may be None; cable_length_cm() then returns None."""

    suite_id: str
    row_index: int | None
    index: int | None
    rows: int | None
    racks_per_row: int | None
    rack_pitch_cm: int | None
    row_pitch_cm: int | None


def endpoint_label(device_name: str, interface_name: str) -> str:
    """Label of one cable end, ``<device>-<interface>``."""
    return f"{device_name}-{interface_name}"


def cable_id(endpoint_labels: list[str]) -> str:
    """Short, deterministic printed ID of the cable joining the endpoint labels (order does not matter)."""
    digest = hashlib.sha1("__".join(sorted(endpoint_labels)).encode(), usedforsecurity=False).hexdigest()
    return f"{CABLE_ID_PREFIX}{digest[:CABLE_ID_HEX_LENGTH].upper()}"


def cable_color(cable_type: str | None) -> str | None:
    """Jacket colour for a cable type, None when the type has none."""
    return CABLE_COLOR_BY_TYPE.get(cable_type) if cable_type else None


def _round_up(centimetres: int) -> int:
    """Round up to the next multiple of LENGTH_ROUNDING_CM."""
    return -(-centimetres // LENGTH_ROUNDING_CM) * LENGTH_ROUNDING_CM


def _grid_slot(end: RackPosition) -> tuple[int, int] | None:
    """The end's (row, index) when inside the suite grid, else None."""
    row, index, rows, per_row = end.row_index, end.index, end.rows, end.racks_per_row
    if row is None or index is None or rows is None or per_row is None:
        return None
    return (row, index) if 1 <= row <= rows and 1 <= index <= per_row else None


def _shared(value_a: int | None, value_b: int | None) -> int | None:
    """A suite property both ends report: the value when they agree, else None."""
    return value_a if value_a == value_b else None


def cable_length_cm(end_a: RackPosition | None, end_b: RackPosition | None) -> int | None:
    """Length in cm of a cable between two rack positions, or None when it cannot be derived."""
    if end_a is None or end_b is None or end_a.suite_id != end_b.suite_id:
        return None
    slot_a, slot_b = _grid_slot(end_a), _grid_slot(end_b)
    if slot_a is None or slot_b is None:
        return None
    if slot_a == slot_b:
        return INTRA_RACK_CM

    # Pitches and grid are suite properties: both ends must agree, else do not guess
    rack_pitch = _shared(end_a.rack_pitch_cm, end_b.rack_pitch_cm)
    per_row = _shared(end_a.racks_per_row, end_b.racks_per_row)
    (row_a, index_a), (row_b, index_b) = slot_a, slot_b
    slack = 2 * SERVICE_LOOP_CM
    if rack_pitch is None or per_row is None:
        return None
    if row_a == row_b:
        return _round_up(abs(index_a - index_b) * rack_pitch + slack)
    row_pitch = _shared(end_a.row_pitch_cm, end_b.row_pitch_cm)
    if row_pitch is None:
        return None
    high_end = per_row + 1
    slots_to_aisle = min(index_a + index_b, (high_end - index_a) + (high_end - index_b))
    return _round_up(abs(row_a - row_b) * row_pitch + slots_to_aisle * rack_pitch + slack)


def cable_attributes(
    cable_type: str | None,
    endpoint_labels: list[str],
    end_a: RackPosition | None,
    end_b: RackPosition | None,
) -> dict[str, Any]:
    """Derived DcimCable attributes for the create data: ``cable_id`` always, ``color`` and ``length_cm`` only when
    known.
    """
    attributes: dict[str, Any] = {"cable_id": cable_id(endpoint_labels)}
    color = cable_color(cable_type)
    if color is not None:
        attributes["color"] = color
    length = cable_length_cm(end_a, end_b)
    if length is not None:
        attributes["length_cm"] = length
    return attributes
