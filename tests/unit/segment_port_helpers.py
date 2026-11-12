"""A switch port's segment membership in test fixtures: its switch's VLAN row."""

from __future__ import annotations

from typing import Any

ROW_KIND = "ManagedSegmentVlan"


def deployment_caps(*segments: dict[str, Any]) -> list[dict[str, Any]]:
    """Cleaned ``capabilities`` entries of a port carrying ``segments`` (one row each)."""
    return [
        {
            "typename": ROW_KIND,
            "id": f"sv-{seg.get('id')}",
            "segment_deployment": {"id": f"sd-{seg.get('id')}", "segment": seg},
        }
        for seg in segments
    ]


def raw_deployment_edges(*segments: dict[str, Any]) -> list[dict[str, Any]]:
    """Raw ``capabilities`` edges of a port carrying the raw segment nodes ``segments``."""
    return [
        {
            "node": {
                "__typename": ROW_KIND,
                "id": f"sv-{seg.get('id')}",
                "segment_deployment": {"node": {"id": f"sd-{seg.get('id')}", "segment": {"node": seg}}},
            }
        }
        for seg in segments
    ]


def raw_port_segments(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The raw segment nodes a raw port carries (through its row capabilities)."""
    return [
        edge["node"]["segment_deployment"]["node"]["segment"]["node"]
        for edge in (port.get("capabilities") or {}).get("edges") or []
        if edge["node"].get("__typename") == ROW_KIND
    ]
