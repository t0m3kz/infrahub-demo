"""Validate physical proxy device configuration."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

from .common import validate_appliance


class CheckProxy(InfrahubCheck):
    """Check physical proxy device readiness."""

    query = "proxy_validation"

    def validate(self, data: Any) -> None:
        devices = clean_data(data).get("DcimPhysicalDevice") or []
        if not devices:
            self.log_error(message="Proxy device not found")
            return

        for error in validate_appliance(devices[0], ha_kind="ManagedProxyHA", label="proxy"):
            self.log_error(message=error)
