"""Validate firewall."""

from infrahub_sdk.checks import InfrahubCheck
from .common import clean_data


class CheckSonicSpine(InfrahubCheck):
    """Check Firewall."""

    query = "spine_config"

    def validate(self, data):
        """Validate Sonic Spine."""
        # device = clean_data(data)["DcimPhysicalDevice"][0]
        
        # # Initialize with default values
        # result = {
        #     "underlay": {},
        # }


        for service in data["DcimPhysicalDevice"]["edges"][0]["node"]["device_service"][
            "edges"
        ]:
        # Process device services once
            if not service:
                self.log_error(
                    message="You're MORON !!! No service."
                )
                
        #     if service["__typename"] == "ServiceOspfUnderlay":
        #         result["underlay"] = {"name": service["name"], "area": service["area"]}

        # if not result["underlay"]:
        #     self.log_error(
        #             message="You're MORON !!! You removed underlay."
        #         )


        