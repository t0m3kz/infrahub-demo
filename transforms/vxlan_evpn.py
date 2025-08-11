#!/usr/bin/env python3
"""
VXLAN/EVPN Configuration Transform Script
Generates device configurations from Infrahub overlay services
"""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class VXLANEVPNTransform(InfrahubTransform):
    """Transform for VXLAN/EVPN device configurations"""

    query = "vxlan_evpn_config"

    async def transform(self, data: Any) -> str:
        """Transform VXLAN/EVPN configuration for device"""

        # Clean and extract data using common pattern
        data = get_data(data)

        # Extract device information
        device_role = data.get("role", {}).get("name", "leaf").lower()
        platform_obj = data.get("device_type", {}).get("platform", {})
        platform = platform_obj.get("netmiko_device_type", "unknown").lower()
        manufacturer = (
            data.get("device_type", {})
            .get("manufacturer", {})
            .get("name", "unknown")
            .lower()
        )

        # Determine template based on device role and platform/manufacturer
        template_filename = self._get_template_filename(
            device_role, platform, manufacturer
        )

        # Set up Jinja2 environment
        template_dir = f"templates/configs/{device_role}s"
        env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(["j2"]),
        )

        # Load and render template
        template = env.get_template(template_filename)

        # Prepare template data
        template_data = self._prepare_template_data(data)

        # Render configuration
        return template.render(**template_data)

    def _get_template_filename(
        self, device_role: str, platform: str, manufacturer: str
    ) -> str:
        """Determine template filename based on device characteristics"""

        # Normalize platform and manufacturer for template selection
        if "nxos" in platform or "nexus" in platform or manufacturer == "cisco":
            return "nxos_vxlan_evpn.j2"
        elif "eos" in platform or manufacturer == "arista":
            return "eos_vxlan_evpn.j2"
        elif "sonic" in platform or manufacturer == "sonic":
            return "sonic_vxlan_evpn.j2"
        else:
            # Default to Cisco NX-OS
            return "nxos_vxlan_evpn.j2"

    def _prepare_template_data(self, data: dict) -> dict:
        """Prepare data for template rendering"""

        # Extract basic device info
        template_data = {
            "name": data.get("name", "unknown"),
            "role": data.get("role", {}),
            "device": data,
            "local_asn": 65000,  # Default ASN
            "loopback0_ip": None,
        }

        # Extract overlay services from device services
        vxlan_services = []
        evpn_services = []
        bgp_services = []

        for service in data.get("device_service", []):
            if not service:
                continue

            service_type = service.get("typename", "")

            if service_type == "ServiceVXLAN":
                vxlan_services.append(service)
            elif service_type == "ServiceEVPN":
                evpn_services.append(service)
            elif service_type == "ServiceBGP" or service_type == "ServiceBGPSession":
                bgp_services.append(service)
                # Extract local ASN
                if service.get("local_as", {}).get("asn"):
                    template_data["local_asn"] = service["local_as"]["asn"]

        template_data.update(
            {
                "vxlan_services": vxlan_services,
                "evpn_services": evpn_services,
                "bgp_services": bgp_services,
            }
        )

        # Extract interfaces
        interfaces = data.get("interfaces", [])
        template_data["interfaces"] = interfaces

        # Find loopback0 IP
        for interface in interfaces:
            if interface.get("name") == "Loopback0" and interface.get("ip_addresses"):
                for ip in interface["ip_addresses"]:
                    addr = ip.get("address")
                    if addr:
                        template_data["loopback0_ip"] = addr.split("/")[0]
                        break
                if template_data["loopback0_ip"]:
                    break

        return template_data
