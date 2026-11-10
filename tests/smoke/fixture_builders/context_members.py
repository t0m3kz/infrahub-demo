"""Give every firewall context in a raw fixture the members the model holds: the HA pair of the device it sits on.

A ``ManagedFirewallContext`` is a shared capability of the firewalls that enforce it, and the firewall query selects
its ``shared_devices`` (checks/common.py validate_context_members holds them to one HA pair). The builders place a
context on its device's ``shared_capabilities``; this hook fills the members a generator would write: the members of
the device's ``ManagedFirewallHA``, or the device alone when it is in none. A context that already lists its members is
left as it is, so the hook is idempotent.
"""

from __future__ import annotations

from typing import Any

FIREWALL_CONTEXT = "ManagedFirewallContext"
FIREWALL_HA = "ManagedFirewallHA"


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else field


def _edge_nodes(relationship: Any) -> list[dict[str, Any]]:
    edges = relationship.get("edges") if isinstance(relationship, dict) else None
    return [edge["node"] for edge in edges or [] if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]


def _pair(device: dict[str, Any]) -> list[str]:
    """The member names of the device's firewall HA domain, else the device's own name."""
    for capability in _edge_nodes(device.get("capabilities")):
        if capability.get("__typename") == FIREWALL_HA:
            members = sorted(str(_value(m.get("name"))) for m in _edge_nodes(capability.get("capabilities")))
            if members:
                return members
    name = _value(device.get("name"))
    return [str(name)] if name else []


def _fill(node: Any) -> None:
    if isinstance(node, dict):
        if "shared_capabilities" in node:
            pair = _pair(node)
            for capability in _edge_nodes(node["shared_capabilities"]):
                if capability.get("__typename") == FIREWALL_CONTEXT and "shared_devices" not in capability and pair:
                    capability["shared_devices"] = {"edges": [{"node": {"name": {"value": m}}} for m in pair]}
        for value in node.values():
            _fill(value)
    elif isinstance(node, list):
        for item in node:
            _fill(item)


def with_context_members(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` with every firewall context on a device given that device's HA pair as its members (in place)."""
    _fill(data)
    return data
