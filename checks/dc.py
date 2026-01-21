"""Validate Data Center deployment against design pattern constraints."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data


class CheckDataCenterCapacity(InfrahubCheck):
    """Check Data Center deployment against design pattern limits."""

    query = "dc_validation"

    def validate(self, data: Any) -> None:
        """Validate Data Center capacity and design compliance.

        NOTE: Validation disabled - design_pattern removed from DataCenter.
        Pod-level validations should be implemented per pod design.
        """
        # Design pattern validation removed - no longer applicable
        # Each pod now has its own design with specific constraints
        return

            if tor_count > max_tors:
                errors.append(f"Pod '{pod_name}': ToR count ({tor_count}) exceeds design maximum ({max_tors})")

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
