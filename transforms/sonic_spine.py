from infrahub_sdk.transforms import InfrahubTransform

from .common import clean_data


class SonicSpine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data):
        cleaned_data = clean_data(data)
        if isinstance(cleaned_data, dict):
            device = cleaned_data["DcimPhysicalDevice"][0]
        else:
            raise ValueError("clean_data() did not return a dictionary")

        # Initialize with default values
        result = {
            "name": device["name"],
            "interfaces": [],
            "underlay": {},
        }

        # Process device services once
        for service in device.get("device_service") or []:
            if not service:
                continue

            if service["__typename"] == "ServiceOspfPeering":
                result["underlay"] = {"name": service["name"], "area": service["area"]}

        for interface in device["interfaces"]:
            result["interfaces"].append(
                {interface["name"]: {"description": interface["description"]}}
            )
        return result
