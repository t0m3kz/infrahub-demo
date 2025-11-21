"""Validate firewall."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data, validate_interfaces


class CheckEdge(InfrahubCheck):
    """Check Edge."""

    query = "edge_config"

    def validate(self, data: Any) -> None:
        """Validate Edge."""
        errors = []
        data = get_data(data)
        errors.extend(validate_interfaces(data))

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
