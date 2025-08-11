from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class Spine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Extract dynamic configuration values from Infrahub data
        device_role = data.get("role", "spine")

        # Find VTEP source IP from loopback interfaces
        vtep_source_ip = None
        router_id = None
        for interface in data.get("interfaces", []):
            if (
                interface.get("typename") == "DcimVirtualInterface"
                and "loopback" in interface.get("name", "").lower()
            ):
                if interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address", "").split("/")[0]
                        if addr:
                            if interface.get("name", "").lower() == "loopback0":
                                router_id = addr
                            if not vtep_source_ip:  # Use first loopback as VTEP source
                                vtep_source_ip = addr

        # Extract OSPF area from OSPF services
        ospf_area = "0.0.0.0"  # default
        for service in data.get("device_service", []):
            if service.get("typename") == "ServiceOSPF" and service.get("area"):
                ospf_area = service.get("area", {}).get("area", "0.0.0.0")
                break

        # Add processed data to template context
        data["dynamic_config"] = {
            "device_role": device_role,
            "vtep_source_ip": vtep_source_ip or "10.255.255.1",  # fallback
            "router_id": router_id or "1.1.1.1",  # fallback
            "ospf_area": ospf_area,
            "default_tenant_vni": 999,  # could be made configurable
        }

        platform = data["device_type"]["platform"]["netmiko_device_type"]
        manufacturer = data["device_type"]["manufacturer"]["name"].lower()
        # Set up Jinja2 environment to load templates from the role subfolder
        env = Environment(
            loader=FileSystemLoader("templates/configs/spines"),
            autoescape=select_autoescape(["j2"]),
        )
        # Select the template for spine devices
        template = env.get_template(f"{manufacturer}_{platform}.j2")
        # Render the template with the provided data
        rendered_config = template.render(data=data)
        # Return the rendered result in a dict (or as needed by the framework)
        return rendered_config
