"""Validate Data Center deployment against design pattern constraints."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck


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
