"""The PBR bindings of a switch, derived from its own nested data (pure, no I/O); rules in utils/vrf_demand.py."""

from __future__ import annotations

from typing import Any

from utils.vrf_demand import PBR_KIND, device_vrf_facts


def has_pbr(device: dict[str, Any]) -> bool:
    """Whether the device has a ``ManagedPBR`` among its ``capabilities``."""
    return any(cap.get("typename") == PBR_KIND for cap in device.get("capabilities") or [])


def pbr_bindings(device: dict[str, Any]) -> dict[str, str]:
    """``Vlan<id> -> footprint id`` of the segment SVIs the device steers to a firewall."""
    facts = device_vrf_facts(device)
    return {name: footprint for name, footprint in facts.pbr_footprints_by_name().items() if footprint}


def overlay_pbr_bindings(device: dict[str, Any]) -> dict[str, frozenset[str]]:
    """``Vlan<slot> -> footprint ids`` of the L3 VNI SVIs of a border device that steer overlay traffic to a firewall."""
    return device_vrf_facts(device).pbr_overlay_footprints_by_name()
