"""Segment port, VLAN row and MLAG pair helpers shared by generators, transforms and checks.

A switch port (physical, LAG) carries a segment as a member of the segment's DEPLOYMENT in the port's topology: the
``ManagedSegmentDeployment`` among the port's interface ``capabilities`` (``port_deployments``, ``port_segments``).

There is no VLAN domain object: a segment's VLAN ID is a row on ONE device (``ManagedSegmentVlan.device``,
unique on [device, vlan_id]). The two peers of an MLAG pair must carry the same VLAN ID for a segment (a dual-homed
port-channel is one L2 broadcast domain): the allocator (generators/segment_vlan.py) gives them one id and
checks/mlag_domains.py reports a pair whose rows differ. The generator reads the capabilities as SDK peers and the
checks as cleaned query data; both pass ``(typename, id)`` pairs here.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from utils.limits import GLOBAL_L2_VNI_START, SHARED_VLAN_COUNT, SHARED_VLAN_ID_MIN

MLAG_KIND = "ManagedMLAG"
DEPLOYMENT_KIND = "ManagedSegmentDeployment"


def port_deployments(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The segment deployments a cleaned port is a member of: its ``capabilities`` of kind ManagedSegmentDeployment."""
    return [
        cap
        for cap in port.get("capabilities") or []
        if isinstance(cap, dict) and cap.get("typename") == DEPLOYMENT_KIND
    ]


def port_segments(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The segments a cleaned port carries: the ``segment`` of each of its deployments (a query that selects it)."""
    return [segment for dep in port_deployments(port) if (segment := dep.get("segment"))]


def mlag_id(capabilities: Iterable[tuple[str | None, str | None]]) -> str | None:
    """The id of the MLAG a device is a member of (its ``(typename, id)`` capabilities), None without one.

    An MLAG without an id (a query that did not select it) is ignored.
    """
    return next(
        (capability_id for typename, capability_id in capabilities if typename == MLAG_KIND and capability_id), None
    )


def mlag_peer_devices(names: Iterable[str]) -> str:
    """The value of ``ManagedMLAG.peer_devices``: the member device names, sorted and joined with a comma.

    The attribute mirrors the many ``capabilities`` edge into one unique Text, because a uniqueness constraint cannot
    use a many relationship. The generator writes it and ``checks/mlag_domains.py`` compares against it.
    """
    return ",".join(sorted(names))


def shared_vlan_id(vni: int | None) -> int | None:
    """The VLAN id a SHARED segment (vlan_scope shared) has on every switch, derived from its VNI.

    VLAN = SHARED_VLAN_ID_MIN + (VNI - GLOBAL_L2_VNI_START): the segment's one VNI from the global pool maps onto the
    shared band, and only when it falls in the first SHARED_VLAN_COUNT numbers of that pool; None for any other VNI (or
    none), which the generator and the check report. One rule for both, so they cannot drift.
    """
    if vni is None or not 0 <= vni - GLOBAL_L2_VNI_START < SHARED_VLAN_COUNT:
        return None
    return SHARED_VLAN_ID_MIN + (vni - GLOBAL_L2_VNI_START)


def shared_vni_band() -> str:
    """The VNIs a shared segment may have, for messages."""
    return f"{GLOBAL_L2_VNI_START}-{GLOBAL_L2_VNI_START + SHARED_VLAN_COUNT - 1}"
