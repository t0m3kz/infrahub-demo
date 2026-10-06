"""IP address string helpers for device transforms."""

from typing import Any


def host_ip(address: str | None) -> str | None:
    """Return `address` without its prefix length ("10.0.0.1/31" -> "10.0.0.1")."""
    return address.split("/")[0] if address else None


def management_ip(interfaces: list[dict[str, Any]]) -> str:
    """Address (no prefix length) of the first role=management interface, "" if it has none."""
    mgmt = next((iface for iface in interfaces if iface.get("role") == "management"), None)
    return host_ip(((mgmt or {}).get("ip_address") or {}).get("address")) or ""
