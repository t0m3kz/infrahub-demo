"""One security-zone assignment for every segment kind (VLAN, VXLAN, cloud)."""

from __future__ import annotations

from typing import Any
from weakref import WeakKeyDictionary

from utils.exchange_transit import namespace_type_for_environment, zone_name_for_namespace_type

from ..protocols import SecurityZone

# A segment whose zone is authoritative data (an exchange zone): never written here.
EXTERNAL_SEGMENT_KIND = "ManagedExternalSegment"

# Per-run memo, keyed by the run's client (a tracking client lives for one run):
# zone name -> zone id, and segment id -> zone name already in place.
_ZONE_IDS: WeakKeyDictionary[Any, dict[str, str]] = WeakKeyDictionary()
_ZONED_SEGMENTS: WeakKeyDictionary[Any, dict[str, str]] = WeakKeyDictionary()


async def ensure_security_zone(
    client: Any,
    *,
    kind: str | None,
    segment_id: str | None,
    environment: str | None,
    current_zone: str | None = None,
) -> str | None:
    """Put a segment in the macro trust zone of its own environment; return the zone name.

    ``kind`` is the segment's real typename and has no default: a segment of an
    unknown kind cannot be written. A missing environment fails closed
    (ValueError) rather than guessing production. The zones are bootstrap data
    (data/bootstrap/24_security_zones.yml): looked up by name once per run,
    never created; a missing one raises LookupError.

    The segment is written untracked (``update_group_context=False``): it is the
    run's own TARGET, so a tracked save would put it in the run's group and a
    later run that skips the save would delete the segment. ``current_zone`` (the
    zone name the caller read from the segment) makes a second pass write
    nothing. An External segment is never touched (returns None).
    """
    if not kind:
        raise ValueError("ensure_security_zone needs the segment's typename (no default kind)")
    if not segment_id:
        raise ValueError("ensure_security_zone needs the segment id")
    if kind == EXTERNAL_SEGMENT_KIND:
        return None
    if not environment:
        raise ValueError(f"Segment {segment_id} has no environment: its security zone cannot be derived")

    zone_name = zone_name_for_namespace_type(namespace_type_for_environment(environment))
    zoned = _ZONED_SEGMENTS.setdefault(client, {})
    if zoned.get(segment_id) == zone_name:
        return zone_name

    if current_zone != zone_name:
        zone_ids = _ZONE_IDS.setdefault(client, {})
        if zone_name not in zone_ids:
            zones = await client.filters(kind=SecurityZone, name__value=zone_name)
            if not zones:
                raise LookupError(f"Security zone {zone_name} not found - load data/bootstrap/24_security_zones.yml")
            zone_ids[zone_name] = zones[0].id
        segment_obj = await client.create(
            kind=kind,
            data={"id": segment_id, "security_zone": {"id": zone_ids[zone_name]}},
        )
        await segment_obj.save(allow_upsert=True, update_group_context=False)

    zoned[segment_id] = zone_name
    return zone_name
