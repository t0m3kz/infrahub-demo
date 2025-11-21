from typing import Any


def validate_interfaces(data: dict[str, Any]) -> list[str]:
    """
    Validates that the device has interfaces and that loopback interfaces have IP addresses.
    """
    errors: list[str] = []
    if len(data.get("interfaces", [])) == 0:
        errors.append("You're MORON !!! You removed all interfaces.")

    for interface in data.get("interfaces", []):
        if interface.get("role") == "loopback" and not interface.get("ip_addresses"):
            errors.append("You're MORON !!! You removed ip from loopback.")

    return errors
