"""Validate border leaf."""

from typing import Any

from transforms.config.border_leaf import BorderLeaf
from transforms.helpers.firewall import get_exchange_transits

from .common import BaseDeviceCheck, validate_interfaces, validate_routing_password


def validate_transit_vlans(device: dict[str, Any]) -> list[str]:
    """No exchange transit VLAN may equal a segment VLAN on this border leaf.

    The border leaf holds no legs itself: it derives one transit VLAN per leg of
    every firewall context tagged on its service ports
    (transforms.helpers.firewall.get_exchange_transits) and renders them next to
    its segment VLANs. get_vlans de-duplicates by VLAN id, so a collision would
    silently drop one of the two. Segment VLANs are collected the same way the
    transform collects its activations.
    """
    transits = get_exchange_transits(device.get("interfaces"))
    if not transits:
        return []

    transform = BorderLeaf.__new__(BorderLeaf)
    interfaces = device.get("interfaces") or []
    capabilities = device.get("capabilities") or []
    activations = transform._collect_activations_from_interfaces(
        interfaces, device_id=device.get("id"), device_capabilities=capabilities
    )
    if any(iface.get("role") == "multisite-vip" for iface in interfaces):
        activations.extend(
            transform._collect_border_gateway_activations(
                device.get("deployment"),
                device_id=device.get("id"),
                device_capabilities=capabilities,
                seen={(act.get("segment") or {}).get("id") for act in activations},
            )
        )
    segment_by_vlan = {
        act["vlan_id"]: (act.get("segment") or {}).get("name", "<unnamed>") for act in activations if act.get("vlan_id")
    }

    device_name = device.get("name", "unknown")
    return [
        f"Exchange transit VLAN {transit['vlan_id']} ('{transit['segment']['name']}') on '{device_name}' "
        f"collides with segment '{segment_by_vlan[transit['vlan_id']]}' — the config would silently keep only one."
        for transit in transits
        if transit["vlan_id"] in segment_by_vlan
    ]


class CheckBorderLeaf(BaseDeviceCheck):
    """Check Border Leaf."""

    query = "border_leaf_config"
    validators = [validate_interfaces, validate_routing_password, validate_transit_vlans]
