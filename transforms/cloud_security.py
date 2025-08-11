"""Cloud Security Configuration Transform for Zscaler and other cloud security providers."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class CloudSecurity(InfrahubTransform):
    """
    Transform to generate cloud security configurations for Zscaler and other cloud security providers.
    """

    query = "cloud_security_config"

    async def transform(self, data: Any) -> Any:
        """
        Transform cloud security data into configuration.

        Args:
            data: Dictionary containing cloud security device and service data

        Returns:
            Rendered configuration string
        """
        data = get_data(data)

        # Extract device information
        device_info = {
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "role": data.get("role", ""),
            "status": data.get("status", ""),
            "cpu": data.get("cpu", ""),
            "memory": data.get("memory", ""),
            "location": data.get("location", {}).get("name", ""),
            "device_type": data.get("device_type", {}).get("name", ""),
            "platform": data.get("platform", {}).get("name", ""),
        }

        # Extract interfaces
        interfaces = []
        for interface in data.get("interfaces", []):
            interfaces.append(
                {
                    "name": interface.get("name", ""),
                    "role": interface.get("role", ""),
                    "status": interface.get("status", ""),
                    "description": interface.get("description", ""),
                }
            )

        # Extract cloud security services if available
        cloud_services = []
        for service in data.get("cloud_services", []):
            cloud_services.append(
                {
                    "name": service.get("name", ""),
                    "provider": service.get("provider", ""),
                    "service_type": service.get("service_type", ""),
                    "tenant_id": service.get("tenant_id", ""),
                    "api_endpoint": service.get("api_endpoint", ""),
                    "cloud_region": service.get("cloud_region", ""),
                }
            )

        # Extract cloud gateways if available
        gateways = []
        for gateway in data.get("gateways", []):
            gateways.append(
                {
                    "name": gateway.get("name", ""),
                    "gateway_type": gateway.get("gateway_type", ""),
                    "city": gateway.get("city", ""),
                    "country": gateway.get("country", ""),
                    "endpoint_address": gateway.get("endpoint_address", ""),
                    "capacity_mbps": gateway.get("capacity_mbps", ""),
                }
            )

        # Add processed data to template context
        data["device"] = device_info
        data["interfaces"] = interfaces
        data["cloud_services"] = cloud_services
        data["gateways"] = gateways

        # Determine platform and manufacturer for template selection
        platform = data["device_type"]["platform"]["netmiko_device_type"]
        manufacturer = (
            data["device_type"]["manufacturer"]["name"].lower().replace(" ", "_")
        )

        # Set up Jinja2 environment to load templates from the cloud_security subfolder
        env = Environment(
            loader=FileSystemLoader("templates/configs/cloud_security"),
            autoescape=select_autoescape(["j2"]),
        )

        # Select the template for cloud security devices
        template = env.get_template(f"{manufacturer}_{platform}.j2")
        # Render the template with the processed data
        rendered_config = template.render(data=data)

        # Return the rendered result
        return rendered_config
