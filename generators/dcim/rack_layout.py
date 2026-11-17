"""Where a rack sits on its suite's grid, read once for the cable lengths (generators/helpers/cable_attributes.py)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from generators.helpers.cable_attributes import RackPosition
from generators.protocols import DcimPhysicalDevice, LocationRack, LocationSuite


def peer_id(rel: Any) -> str | None:
    """Id of a loaded relationship's peer, None when it is missing or unset."""
    return rel.id if rel is not None and rel.initialized else None


async def read_rack_positions(
    client: Any, rack_ids: Iterable[str], *, racks: dict[str, Any], suites: dict[str, Any]
) -> dict[str, RackPosition]:
    """The position of each rack whose rack and suite are visible, in at most two reads (racks, suites). ``racks`` and
    ``suites`` hold what a caller has read already and are filled with what this call reads; a rack or suite that is
    not visible yet has no entry in the result.
    """
    wanted = sorted(set(rack_ids))
    missing = [rack_id for rack_id in wanted if rack_id not in racks]
    if missing:
        racks.update(
            {rack.id: rack for rack in await client.filters(kind=LocationRack, ids=missing, include=["parent"])}
        )
    suite_ids = sorted(
        {
            suite_id
            for rack_id in wanted
            if rack_id in racks
            and (suite_id := peer_id(getattr(racks[rack_id], "parent", None)))
            and suite_id not in suites
        }
    )
    if suite_ids:
        suites.update({suite.id: suite for suite in await client.filters(kind=LocationSuite, ids=suite_ids)})

    positions: dict[str, RackPosition] = {}
    for rack_id in wanted:
        rack = racks.get(rack_id)
        suite = suites.get(peer_id(getattr(rack, "parent", None)) or "") if rack is not None else None
        if rack is None or suite is None:
            continue
        positions[rack_id] = RackPosition(
            suite_id=suite.id,
            row_index=rack.row_index.value,
            index=rack.index.value,
            rows=suite.rows.value,
            racks_per_row=suite.racks_per_row.value,
            rack_pitch_cm=suite.rack_pitch_cm.value,
            row_pitch_cm=suite.row_pitch_cm.value,
        )
    return positions


async def read_device_positions(
    client: Any, device_names: Iterable[str], *, extra_rack_ids: Iterable[str] = ()
) -> dict[str, RackPosition | None]:
    """The rack position of each existing device called ``device_names`` (None: no rack, or a rack not visible yet), for the
    cable lengths of a plan; ``extra_rack_ids`` are racks of devices the plan writes. Three reads at most.
    """
    names = sorted(set(device_names))
    rack_ids: dict[str, str | None] = {}
    if names:
        devices = await client.filters(kind=DcimPhysicalDevice, name__values=names, include=["rack"])
        rack_ids = {device.name.value: peer_id(getattr(device, "rack", None)) for device in devices}
    positions = await read_rack_positions(
        client, [*(rid for rid in rack_ids.values() if rid), *extra_rack_ids], racks={}, suites={}
    )
    return {name: positions.get(rid) if rid else None for name, rid in rack_ids.items()}
