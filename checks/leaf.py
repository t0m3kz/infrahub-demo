"""Validate firewall."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data, validate_interfaces


class CheckLeaf(InfrahubCheck):
    """Check Firewall."""

    query = "leaf_config"

    def validate(self, data: Any) -> None:
        """Validate Leaf."""
        errors: list[str] = []
        data = get_data(data)
        errors.extend(validate_interfaces(data))
        if not data.get("device_service"):
            errors.append("You're MORON !!! No service.")
        redundant_bgp = [
            service.get("name")
            for service in data.get("device_service", [])
            if service["typename"] == "ServiceBGPSession"
        ]
        if len(redundant_bgp) < 2:
            errors.append("No BGP redundancy set !!!")

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
