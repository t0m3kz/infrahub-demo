"""The segments a firewall context terminates: the sub-interface each member renders for one."""

from __future__ import annotations

import logging
from typing import Any

from utils.circuit_underlay import cabled_port

_log = logging.getLogger(__name__)


def _trunk_name(context: dict[str, Any]) -> str | None:
    """The name of the uplink trunk the context's legs ride on, None when it has no leg on this device."""
    return next(
        (name for leg in context.get("legs") or [] if (name := (leg.get("parent_interface") or {}).get("name"))), None
    )


def _facing_device_ids(interfaces: list[dict[str, Any]], trunk_name: str, device_id: str | None) -> set[str]:
    """The facing devices of a physical trunk or the cabled host port a virtual trunk rides on."""
    trunk = next((iface for iface in interfaces if iface.get("name") == trunk_name), {})
    port = cabled_port(trunk)
    if port is None:
        return set()
    own_device_id = (port.get("device") or {}).get("id")
    if port is not trunk and not own_device_id:
        return set()
    endpoints = (port.get("cable") or {}).get("endpoints") or []
    device_ids = {(endpoint.get("device") or {}).get("id") for endpoint in endpoints}
    return {identifier for identifier in device_ids if identifier and identifier != (own_device_id or device_id)}


def _row_vlan(segment: dict[str, Any], device_ids: set[str]) -> int | None:
    """The VLAN id of the segment's row on one of the devices, None when it has none there."""
    for deployment in segment.get("segment_deployments") or []:
        for row in deployment.get("vlans") or []:
            if (row.get("device") or {}).get("id") in device_ids and isinstance(row.get("vlan_id"), int):
                return row["vlan_id"]
    return None


def terminated_interfaces(
    contexts: list[dict[str, Any]],
    interfaces: list[dict[str, Any]],
    ha: dict[str, Any] | None,
    device_id: str | None,
    device_name: str,
) -> list[dict[str, Any]]:
    """One firewall interface per segment a context terminates, in the shape of ``_build_fw_interfaces``."""
    found: list[dict[str, Any]] = []
    for context in contexts:
        segments = context.get("terminated_segments") or []
        if not segments:
            continue
        trunk = _trunk_name(context)
        facing = _facing_device_ids(interfaces, trunk, device_id) if trunk else set()
        if not facing:
            _log.warning(
                "%s: context %s terminates %d segment(s) but has no uplink trunk cabled to a facing leaf "
                "on this device: none rendered",
                device_name,
                context.get("name"),
                len(segments),
            )
            continue
        for segment in segments:
            gateway = (segment.get("gateway") or {}).get("address")
            vlan = _row_vlan(segment, facing)
            if not gateway or vlan is None:
                _log.warning(
                    "%s: terminated segment %s of context %s not rendered: %s",
                    device_name,
                    segment.get("name"),
                    context.get("name"),
                    "no gateway" if not gateway else "no VLAN row on the facing leaf yet",
                )
                continue
            found.append(
                {
                    "name": f"{trunk}.{vlan}",
                    "description": segment.get("name"),
                    "status": None,
                    "vlan_id": vlan,
                    "parent_interface": {"name": trunk},
                    "ip_address": {"address": gateway},
                    "security_zone": segment.get("security_zone"),
                    "virtual_ip": gateway if ha else None,
                }
            )
    return sorted(found, key=lambda iface: (iface["parent_interface"]["name"], iface["vlan_id"]))
