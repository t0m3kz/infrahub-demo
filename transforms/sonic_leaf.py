from infrahub_sdk.transforms import InfrahubTransform

from .common import clean_data


class SonicLeaf(InfrahubTransform):
    query = "leaf_config"

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
            "overlay": {},
            "bgp": [],
            "vlans": [],
        }

        # Process device services once
        for service in device.get("device_service") or []:
            if not service:
                continue

            if service["__typename"] == "ServiceOspfPeering":
                result["underlay"] = {"name": service["name"], "area": service["area"]}
            elif service["__typename"] == "ServiceBgpPeering":
                result["overlay"] = {"name": service["name"], "asn": service["asn"]}

        # Process interfaces and collect VLANs in one pass
        vlans_set = set()

        for interface in device["interfaces"]:
            vlans = []

            for service in interface.get("service") or []:
                if service and service["__typename"] == "ServiceLayer2Network":
                    vlan = service["vlan"]
                    vlans.append(vlan)
                    vlans_set.add(vlan)

            result["interfaces"].append(
                {
                    interface["name"]: {
                        "description": interface["description"],
                        "vlans": vlans,
                    }
                }
            )

        result["vlans"] = list(vlans_set)
        return result
