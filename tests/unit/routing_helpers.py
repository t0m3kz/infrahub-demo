"""Shared routing planner mocks and raw-query-derived VRF fixtures."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from unittest.mock import MagicMock

from utils.data_cleaning import clean_data


def make_vrf(zone: Mapping[str, Any], deployment: str = "fixture-deployment") -> dict[str, Any]:
    """Build a scoped routing owner through the same raw GraphQL shape as production."""
    return clean_data(
        {
            "__typename": "ManagedVRF",
            "id": f"vrf-{deployment}-{zone.get('id') or zone.get('name') or 'unclassified'}",
            "deployment": {"node": {"id": deployment, "name": {"value": deployment}}},
            "security_zone": {"node": {key: value if key == "id" else {"value": value} for key, value in zone.items()}},
        }
    )


def make_loopback(name: str, device_id: str, role: str, ip: str, lb_id: str | None = None) -> MagicMock:
    """A loopback interface of ``name`` (id ``lb-<device_id>`` unless ``lb_id``) carrying ``<ip>/32``."""
    lb = MagicMock()
    lb.id = lb_id or f"lb-{device_id}"
    lb.device.peer.name.value = name
    lb.device.peer.id = device_id
    lb.device.peer.role.value = role
    lb.ip_address.id = f"ip-{device_id}"
    lb.ip_address.display_label = f"{ip}/32"
    return lb


def make_p2p_interface(iface_id: str, iface_name: str, device_id: str, cable_id: str) -> MagicMock:
    """A cabled point-to-point interface of a device."""
    iface = MagicMock()
    iface.id = iface_id
    iface.name.value = iface_name
    iface.cable.id = cable_id
    iface.device.id = device_id
    return iface
