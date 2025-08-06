"""Validate firewall."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data


class InfrahubValidateFirewall(InfrahubCheck):
    """Check Firewall."""

    query = "firewall_config"

    def validate(self, data: Any) -> None:
        """Validate firewall."""
        errors: list[str] = []
        data = get_data(data)
        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
