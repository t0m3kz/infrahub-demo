from infrahub_sdk.transforms import InfrahubTransform
from .common import clean_data

class SonicSpine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data):
        device = clean_data(data)["DcimPhysicalDevice"][0]
        
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
                
            if service["__typename"] == "ServiceOspfUnderlay":
                result["underlay"] = {"name": service["name"], "area": service["area"]}

        for interface in device["interfaces"]:            
            result["interfaces"].append({interface["name"]: {"description": interface["description"]}})
        return result