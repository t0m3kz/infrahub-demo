from typing import Any

# Re-export shared utilities for Infrahub compatibility
# Infrahub loads files directly from git, so utils package may not be in path
from utils.data_cleaning import clean_data, get_data

__all__ = ["clean_data", "get_data", "validate_interfaces"]


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
