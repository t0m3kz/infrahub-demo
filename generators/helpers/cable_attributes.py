"""Derived attributes of a DcimCable: printed ID, jacket colour and length.

Pure functions, no SDK: every cable-creating generator calls ``cable_attributes`` and merges the result into the
cable's create data, so one rule decides all three values and a rerun writes the same ones.

``cable_id``
    ``CBL-`` plus the first ``CABLE_ID_HEX_LENGTH`` (10) upper-case hex digits of the SHA-1 of the cable's two
    endpoint labels (``<device>-<interface>``), sorted and joined with ``__`` (the same text a cable is named by).
    It is short enough to print on a label, depends only on the two ends (never a counter, a random value or the
    deployment, which is not known at every creation site and may be renamed), so it is identical on every rerun.
    Two different cables collide with probability n^2 / 2^41 (about 5e-5 for 10 000 cables); the schema declares
    ``cable_id`` unique, so a collision fails the save instead of passing silently.

``color``
    The TIA-598 jacket colour of the cable type, see ``CABLE_COLOR_BY_TYPE``; ``None`` for a type with no entry.

``length_cm``
    Integer centimetres (Infrahub's Number is integer-only) derived from the suite layout, rounded UP to the next
    ``LENGTH_ROUNDING_CM`` (50 cm). Racks sit on a grid: ``row_index`` 1..rows, ``index`` 1..racks_per_row, spaced
    ``rack_pitch_cm`` along a row and ``row_pitch_cm`` between rows. Every end adds ``SERVICE_LOOP_CM`` (50 cm) of
    slack; the cable is routed along the rows and across at an aisle end, never diagonally:

    * same rack: ``INTRA_RACK_CM`` (100 cm), no slack added;
    * same row, different racks: ``|index_a - index_b| * rack_pitch + 2 * SERVICE_LOOP_CM``;
    * different rows: ``|row_a - row_b| * row_pitch + (walk to the aisle end) + 2 * SERVICE_LOOP_CM``. The aisle
      lies one slot beyond each end of a row, so a rack at ``index`` is ``index`` slots from the low end and
      ``racks_per_row + 1 - index`` from the high end. The cable crosses at ONE end for both racks, the cheaper of
      the two: ``min(index_a + index_b, (rpr + 1 - index_a) + (rpr + 1 - index_b)) * rack_pitch``. (Each rack going
      to its own nearest end would need an unrouted run along the aisle whenever those are opposite ends.)

    ``None`` (the attribute stays unset) whenever a figure would be a guess: an end with no rack (a device with no
    rack, a virtual device), racks of different suites, a missing pitch, or a rack coordinate missing or outside
    the suite grid.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

# TIA-598 jacket colours by DcimCable.type. A test pins this to the schema's `type` and `color` choices, so a new
# cable type fails there until it gets a colour.
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

    Every figure but ``suite_id`` may be None (an unset schema attribute); cable_length_cm() then returns None
    rather than guess.
    """

    suite_id: str
    row_index: int | None
    index: int | None
    rows: int | None
    racks_per_row: int | None
    rack_pitch_cm: int | None
    row_pitch_cm: int | None


def endpoint_label(device_name: str, interface_name: str) -> str:
    """The label of one cable end, ``<device>-<interface>``: the text cables are named and their ID derived from."""
    return f"{device_name}-{interface_name}"


def cable_id(endpoint_labels: list[str]) -> str:
    """Short, deterministic printed ID of the cable joining the given endpoint labels (order does not matter)."""
    digest = hashlib.sha1("__".join(sorted(endpoint_labels)).encode(), usedforsecurity=False).hexdigest()
    return f"{CABLE_ID_PREFIX}{digest[:CABLE_ID_HEX_LENGTH].upper()}"


def cable_color(cable_type: str | None) -> str | None:
    """Jacket colour for a cable type, None when the type has none."""
    return CABLE_COLOR_BY_TYPE.get(cable_type) if cable_type else None


def _round_up(centimetres: int) -> int:
    """Round up to the next multiple of LENGTH_ROUNDING_CM (a multiple stays as it is)."""
    return -(-centimetres // LENGTH_ROUNDING_CM) * LENGTH_ROUNDING_CM


def _grid_slot(end: RackPosition) -> tuple[int, int] | None:
    """The end's (row, index) when both lie inside the suite's rows x racks_per_row grid, else None."""
    row, index, rows, per_row = end.row_index, end.index, end.rows, end.racks_per_row
    if row is None or index is None or rows is None or per_row is None:
        return None
    return (row, index) if 1 <= row <= rows and 1 <= index <= per_row else None


def _shared(value_a: int | None, value_b: int | None) -> int | None:
    """A suite property both ends report: the value when they agree, None when either is missing or they differ."""
    return value_a if value_a == value_b else None


def cable_length_cm(end_a: RackPosition | None, end_b: RackPosition | None) -> int | None:
    """Length in cm of a cable between two rack positions, or None when it cannot be derived (module docstring)."""
    if end_a is None or end_b is None or end_a.suite_id != end_b.suite_id:
        return None
    slot_a, slot_b = _grid_slot(end_a), _grid_slot(end_b)
    if slot_a is None or slot_b is None:
        return None
    if slot_a == slot_b:
        return INTRA_RACK_CM

    # The pitches and the grid are suite properties, so both ends must agree on them; if not, do not guess.
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
    """The derived DcimCable attributes to merge into the create data: ``cable_id`` always, ``color`` and
    ``length_cm`` only when known (a key is omitted rather than sent as None)."""
    attributes: dict[str, Any] = {"cable_id": cable_id(endpoint_labels)}
    color = cable_color(cable_type)
    if color is not None:
        attributes["color"] = color
    length = cable_length_cm(end_a, end_b)
    if length is not None:
        attributes["length_cm"] = length
    return attributes
