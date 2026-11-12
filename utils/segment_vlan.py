"""Segment port, VLAN row and MLAG pair helpers shared by generators, transforms and checks.

A port carries a segment as a member of its switch's ``ManagedSegmentVlan`` row; see AGENTS.md "VLAN rows".
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from utils.limits import GLOBAL_L2_VNI_START, SHARED_VLAN_COUNT, SHARED_VLAN_ID_MIN
from utils.routing_domain import INTERFACE_CAPABILITY_KEYS

MLAG_KIND = "ManagedMLAG"
ROW_KIND = "ManagedSegmentVlan"
DEPLOYMENT_KIND = "ManagedSegmentDeployment"


def port_rows(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The VLAN rows a cleaned port is a member of: its ``capabilities`` of kind ManagedSegmentVlan."""
    return [cap for cap in port.get("capabilities") or [] if isinstance(cap, dict) and cap.get("typename") == ROW_KIND]


def svi_rows(svi: dict[str, Any]) -> list[dict[str, Any]]:
    """The VLAN rows a cleaned role-``svi`` interface is a member of, found under either capability key, by id."""
    rows = {
        str(cap.get("id") or id(cap)): cap
        for key in INTERFACE_CAPABILITY_KEYS
        for cap in svi.get(key) or []
        if isinstance(cap, dict) and cap.get("typename") == ROW_KIND
    }
    return list(rows.values())


def port_deployments(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The segment deployments a cleaned port carries: the ``segment_deployment`` of each of its rows."""
    return [dep for row in port_rows(port) if (dep := row.get("segment_deployment"))]


def port_segments(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The segments a cleaned port carries: the ``segment`` of each row's deployment."""
    return [segment for dep in port_deployments(port) if (segment := dep.get("segment"))]


def mlag_id(capabilities: Iterable[tuple[str | None, str | None]]) -> str | None:
    """The id of the MLAG a device is a member of (from ``(typename, id)`` capabilities), None without one."""
    return next(
        (capability_id for typename, capability_id in capabilities if typename == MLAG_KIND and capability_id), None
    )


def mlag_peer_devices(names: Iterable[str]) -> str:
    """The value of ``ManagedMLAG.peer_devices``: the sorted member names joined with a comma (unique Text mirror)."""
    return ",".join(sorted(names))


def shared_vlan_id(vni: int | None) -> int | None:
    """The VLAN id of a SHARED segment, SHARED_VLAN_ID_MIN + (VNI - GLOBAL_L2_VNI_START); None for a VNI outside the
    band.
    """
    if vni is None or not 0 <= vni - GLOBAL_L2_VNI_START < SHARED_VLAN_COUNT:
        return None
    return SHARED_VLAN_ID_MIN + (vni - GLOBAL_L2_VNI_START)


def shared_vni_band() -> str:
    """The VNIs a shared segment may have, for messages."""
    return f"{GLOBAL_L2_VNI_START}-{GLOBAL_L2_VNI_START + SHARED_VLAN_COUNT - 1}"
