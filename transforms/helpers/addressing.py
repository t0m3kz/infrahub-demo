"""IP address and sub-interface parent helpers for device transforms."""

from typing import Any


def host_ip(address: str | None) -> str | None:
    """Return `address` without its prefix length ("10.0.0.1/31" -> "10.0.0.1")."""
    return address.split("/")[0] if address else None


def sub_interface_parent(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The parent port of a VLAN sub-interface (``<parent>.<vlan>``), None for any other interface.

    ``parent_interface`` is also a VM NIC's host port (a port of another device), which no config renders as a
    parent: only a name that starts with ``<parent name>.`` is the sub-interface of that parent.
    """
    parent = iface.get("parent_interface")
    if parent and str(iface.get("name") or "").startswith(f"{parent.get('name')}."):
        return parent
    return None


def management_ip(interfaces: list[dict[str, Any]]) -> str:
    """Address (no prefix length) of the first role=management interface, "" if it has none."""
    mgmt = next((iface for iface in interfaces if iface.get("role") == "management"), None)
    return host_ip(((mgmt or {}).get("ip_address") or {}).get("address")) or ""
