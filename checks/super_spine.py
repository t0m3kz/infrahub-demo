"""Validate super spine."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from checks.common import validate_interfaces
from utils.data_cleaning import get_data


class CheckSuperSpine(InfrahubCheck):
    """Check Super Spine."""

    query = "super_spine_config"

    def validate(self, data: Any) -> None:
        """Validate Super Spine."""
        errors: list[str] = []
        data = get_data(data)
        errors.extend(validate_interfaces(data))
        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
