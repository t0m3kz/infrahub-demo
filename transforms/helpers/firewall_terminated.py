"""The segments a firewall context terminates: the sub-interface each member renders for one.

A ``ManagedVxlanSegment`` with ``terminated_on`` set is gatewayed by the firewall pair of that context: its ``gateway``
is the pair's floating VIP (no per-member address) and the fabric only bridges it (VLAN row, L2 VNI, no SVI). The
sub-interface is not an object. It is derived here, per member, from three facts of the query data:

- the trunk: the ``parent_interface`` of the context's transit legs (every leg of a context rides on the same
  ``uplink`` trunk, utils/exchange_transit.py);
- the facing leaf: the device at the far end of the trunk's cable (or its physical host port's cable for a VM NIC);
- the VLAN: the segment's VLAN row on that leaf, the tag both ends of the cable use.

The result is shaped like the firewall interfaces ``transforms/config/firewall.py`` builds, with the VIP as
``virtual_ip``, so every vendor template renders it through its existing interface path.
"""

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
    """One firewall interface per segment a context terminates, in the shape of ``_build_fw_interfaces``.

    Each is ``<trunk>.<vlan>`` on the trunk, addressed with the segment's gateway as ``virtual_ip`` (a connected
    network: no static route, see ``get_firewall_static_routes``) and in the segment's zone. A segment is skipped, with
    a warning, when the context has no leg on this device (no trunk), the trunk has no facing device, the segment has
    no gateway, or no VLAN row on the facing leaf yet (the segment reconcile has not converged): the next run renders it.

    ``contexts`` come from ``get_firewall_contexts`` (``terminated_segments`` and ``legs``), ``interfaces`` are the
    device's own cleaned interfaces, ``ha`` its HA pair (``get_ha``; without one there is no VIP to own) and
    ``device_id`` its id.
    """
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
