"""Validate firewall."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data, validate_interfaces


class CheckToR(InfrahubCheck):
    """Check Firewall."""

    query = "leaf_config"

    def validate(self, data: Any) -> None:
        """Validate Leaf."""
        errors: list[str] = []
        data = get_data(data)
        errors.extend(validate_interfaces(data))
        # if not data.get("device_services", []):
        #     errors.append("No overlay/ underlay services.")
        # else:
        #     redundant_bgp = [
        #         service.get("name")
        #         for service in data.get("device_services", [])
        #         if service["typename"] == "ServiceBGP"
        #     ]
        #     if redundant_bgp and len(redundant_bgp) < 2:
        #         errors.append("No BGP redundancy set !!!")

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
