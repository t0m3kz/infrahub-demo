"""A switch port's segment membership in test fixtures: the segment's ManagedSegmentDeployment among the port's
``capabilities`` (utils/segment_vlan.py ``port_segments``), cleaned or raw."""

from __future__ import annotations

from typing import Any

DEPLOYMENT_KIND = "ManagedSegmentDeployment"


def deployment_caps(*segments: dict[str, Any]) -> list[dict[str, Any]]:
    """Cleaned ``capabilities`` entries of a port carrying ``segments`` (one deployment each)."""
    return [{"typename": DEPLOYMENT_KIND, "id": f"sd-{seg.get('id')}", "segment": seg} for seg in segments]


def raw_deployment_edges(*segments: dict[str, Any]) -> list[dict[str, Any]]:
    """Raw ``capabilities`` edges of a port carrying the raw segment nodes ``segments``."""
    return [
        {"node": {"__typename": DEPLOYMENT_KIND, "id": f"sd-{seg.get('id')}", "segment": {"node": seg}}}
        for seg in segments
    ]


def raw_port_segments(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The raw segment nodes a raw port carries (its deployment capabilities)."""
    return [
        edge["node"]["segment"]["node"]
        for edge in (port.get("capabilities") or {}).get("edges") or []
        if edge["node"].get("__typename") == DEPLOYMENT_KIND
    ]
