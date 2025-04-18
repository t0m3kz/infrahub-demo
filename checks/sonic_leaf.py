"""Validate firewall."""

from infrahub_sdk.checks import InfrahubCheck
from .common import clean_data


class CheckSonicLeaf(InfrahubCheck):
    """Check Firewall."""

    query = "leaf_config"

    def validate(self, data):
        """Validate Sonic Spine."""
        device = clean_data(data)["DcimPhysicalDevice"][0]

        # Initialize with default values
        result = {
            "underlay": {},
        }

        # Process device services once
        for service in device.get("device_service") or []:
            if not service:
                self.log_error(message="You're MORON !!! No service.")

            if service["__typename"] == "ServiceOspfPeering":
                result["underlay"] = {"name": service["name"], "area": service["area"]}

        if not result["underlay"]:
            self.log_error(message="You're MORON !!! You removed underlay.")
        return device
