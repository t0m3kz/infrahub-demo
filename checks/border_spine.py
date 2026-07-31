"""Validate border spine."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data, validate_exchange_gateways, validate_interfaces, validate_routing_password


class CheckBorderSpine(InfrahubCheck):
    """Check Border Spine."""

    query = "border_spine_config"

    def validate(self, data: Any) -> None:
        """Validate Border Spine."""
        errors: list[str] = []
        data = get_data(data)
        errors.extend(validate_interfaces(data))
        errors.extend(validate_routing_password(data))
        errors.extend(validate_exchange_gateways(data))

        if errors:
            for error in errors:
                self.log_error(message=error)
