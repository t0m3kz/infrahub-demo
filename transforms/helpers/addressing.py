"""IP address and sub-interface parent helpers for device transforms."""

from typing import Any

from utils.interface_encapsulation import parent_of


def host_ip(address: str | None) -> str | None:
    """Return `address` without its prefix length ("10.0.0.1/31" -> "10.0.0.1")."""
    return address.split("/")[0] if address else None


def sub_interface_parent(iface: dict[str, Any]) -> dict[str, Any] | None:
    """The parent port of a sub-interface (an interface riding on a port of its own device), None for any other.

    The encapsulation ``parent`` is also a VM NIC's host port (a port of another device), which no config renders as
    a parent: only a parent on the interface's own device (both ``device`` ids selected by the query) counts. The
    name is a convention and plays no part; the VLAN tag is ``vlan_of`` (None: untagged).
    """
    parent = parent_of(iface)
    own_device = (iface.get("device") or {}).get("id")
    if parent and own_device and (parent.get("device") or {}).get("id") == own_device:
        return parent
    return None


def management_ip(interfaces: list[dict[str, Any]]) -> str:
    """Address (no prefix length) of the first role=management interface, "" if it has none."""
    mgmt = next((iface for iface in interfaces if iface.get("role") == "management"), None)
    return host_ip(((mgmt or {}).get("ip_address") or {}).get("address")) or ""
