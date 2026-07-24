"""Validate access-leaf."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data, validate_interfaces, validate_routing_password


class CheckAccessLeaf(InfrahubCheck):
    """Check Access Leaf."""

    query = "leaf_config"

    def validate(self, data: Any) -> None:
        """Validate Access Leaf."""
        errors: list[str] = []
        data = get_data(data)
        errors.extend(validate_interfaces(data))
        errors.extend(validate_routing_password(data))

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
