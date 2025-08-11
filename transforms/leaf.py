from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class Leaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Extract and process network segments with gateway information
        network_segments = []
        customer_vrfs: list[dict] = []
        vlan_count = 0

        for interface in data.get("interfaces", []):
            for service in interface.get("service", []):
                if service.get("typename") == "ServiceNetworkSegment":
                    vlan_count += 1
                    # Process network segment for gateway configuration
                    segment_data = {
                        "name": service.get("name"),
                        "vni": service.get("vni"),
                        "description": service.get("description"),
                        "status": service.get("status"),
                        "segment_type": service.get("segment_type"),
                        "tenant_isolation": service.get("tenant_isolation"),
                        "interface": interface.get("name"),
                        "prefixes": service.get("prefixes", []),
                        "gateway_ip": service.get("gateway_ip"),
                        "customer": service.get("customer"),
                        "namespace": service.get("namespace"),
                        # Security attributes
                        "security_enforcement": service.get(
                            "security_enforcement", False
                        ),
                        "default_deny_all": service.get("default_deny_all", False),
                        "enable_logging": service.get("enable_logging", False),
                        "security_zone": service.get("security_zone"),
                        "security_profile": service.get("security_profile"),
                        "access_control_lists": service.get("access_control_lists", []),
                    }
                    network_segments.append(segment_data)

                    # Create customer-specific VRF based on customer and namespace
                    customer = service.get("customer")
                    namespace = service.get("namespace")

                    if customer and namespace:
                        # Extract the actual string values from GraphQL node structure
                        customer_name = (
                            customer.get("node", {}).get("name")
                            if isinstance(customer, dict)
                            else customer
                        )
                        namespace_name = (
                            namespace.get("node", {}).get("name")
                            if isinstance(namespace, dict)
                            else namespace
                        )

                        if customer_name and namespace_name:
                            customer_name = customer_name.replace(" ", "").replace(
                                "-", "_"
                            )
                            namespace_name = namespace_name.replace(" ", "").replace(
                                "-", "_"
                            )
                            vrf_name = f"{customer_name}_{namespace_name}_VRF"
                        else:
                            # Fallback: create VRF based on segment name if customer/namespace missing values
                            segment_name = service.get("name", "default")
                            vrf_name = f"{segment_name.replace(' ', '_').replace('-', '_')}_VRF"
                    else:
                        # Fallback: create VRF based on segment name if customer/namespace missing
                        segment_name = service.get("name", "default")
                        vrf_name = (
                            f"{segment_name.replace(' ', '_').replace('-', '_')}_VRF"
                        )

                    # Check if this VRF already exists in our list
                    existing_vrf = None
                    for vrf in customer_vrfs:
                        if vrf["name"] == vrf_name:
                            existing_vrf = vrf
                            break

                    if not existing_vrf:
                        # Extract the actual string values for storage
                        customer_value = (
                            customer.get("node", {}).get("name")
                            if isinstance(customer, dict)
                            else (customer or "Unknown")
                        )
                        namespace_value = (
                            namespace.get("node", {}).get("name")
                            if isinstance(namespace, dict)
                            else (namespace or "default")
                        )

                        customer_vrfs.append(
                            {
                                "name": vrf_name,
                                "customer": customer_value,
                                "namespace": namespace_value,
                                "vni": service.get("vni"),  # Use segment VNI for VRF
                                "segments": [segment_data],
                            }
                        )
                    else:
                        existing_vrf["segments"].append(segment_data)

        print(f"Found {vlan_count} network segments for {data.get('name', 'Unknown')}")

        # Extract dynamic configuration values from Infrahub data
        device_role = data.get("role", "leaf")

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
        data["network_segments"] = network_segments
        data["customer_vrfs"] = customer_vrfs
        data["dynamic_config"] = {
            "device_role": device_role,
            "vtep_source_ip": vtep_source_ip or "10.255.255.1",  # fallback
            "router_id": router_id or "1.1.1.1",  # fallback
            "ospf_area": ospf_area,
            "default_tenant_vni": 999,  # fallback only, should use customer VRFs
        }

        platform = data["device_type"]["platform"]["netmiko_device_type"]
        manufacturer = data["device_type"]["manufacturer"]["name"].lower()
        # Set up Jinja2 environment to load templates from the role subfolder
        env = Environment(
            loader=FileSystemLoader("templates/configs/leafs"),
            autoescape=select_autoescape(["j2"]),
        )
        # Select the template for leaf devices
        template = env.get_template(f"{manufacturer}_{platform}.j2")
        # Render the template with the processed data including network segments
        rendered_config = template.render(data=data)
        # Return the rendered result in a dict (or as needed by the framework)
        return rendered_config
