from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import get_data


class LoadBalancer(InfrahubTransform):
    query = "loadbalancer_config"

    async def transform(self, data: Any) -> Any:
        data = get_data(data)

        # Extract VIPs and process them for load balancer configuration
        vips = []

        # Process backend servers first
        backend_servers = {}
        backend_server_list = []

        # Get backend servers from the separate query
        for server_edge in data.get("backend_servers", []):
            server = server_edge.get("node", {})
            server_name = server.get("name", "unknown")
            server_status = server.get("status", "unknown")

            # Get server IP address
            server_ip = None
            for interface in server.get("interfaces", []):
                if interface.get("role") == "server" and interface.get("ip_addresses"):
                    for ip_edge in interface["ip_addresses"]:
                        ip_node = ip_edge.get("node", {})
                        server_ip = ip_node.get("address", "").split("/")[0]
                        break
                    if server_ip:
                        break

            if server_ip and server_status == "active":
                server_config = {
                    "name": server_name,
                    "ip": server_ip,
                    "port": 80,  # Default HTTP port
                    "status": server_status,
                    "weight": 1,
                    "max_fails": 3,
                    "fail_timeout": "30s",
                }
                backend_servers[server_name] = server_config
                backend_server_list.append(server_config)

        # Process VIPs from device services (new schema structure)
        device_services = data.get("device_service", [])
        for service in device_services:
            service_type = service.get("__typename") or service.get("typename")
            if service_type == "ServiceVIP":
                # ServiceVIP has a vip_ip reference to IpamIPAddress
                vip_ip = service.get("vip_ip")

                if vip_ip:
                    # Create a VIP entry using actual values from the schema
                    vip = {
                        "hostname": service.get(
                            "hostname",
                            service.get(
                                "name", f"vip-{vip_ip.get('address', 'unknown')}"
                            ),
                        ),
                        "mode": service.get("mode", "http"),
                        "status": service.get("status", "active"),
                        "balance": service.get("balance", "roundrobin"),
                        "ssl_certificate": service.get("ssl_certificate"),
                        "ip_address": vip_ip.get("address"),
                        "backend_servers": [],
                        "health_checks": [],
                        "service_name": service.get("name", "Unknown Service"),
                        "service_description": service.get("description", ""),
                    }

                    # Process backend servers
                    backend_servers_edges = service.get("backend_servers", [])
                    for server in backend_servers_edges:
                        # Handle both edge format and direct format
                        server_data = (
                            server.get("node", server) if "node" in server else server
                        )
                        server_ip = (
                            server_data.get("ip_address", {})
                            .get("address", "")
                            .split("/")[0]
                            if server_data.get("ip_address")
                            else None
                        )

                        if server_ip:
                            backend_config = {
                                "hostname": server_data.get("hostname"),
                                "ip_address": server_ip,
                                "port": 80,  # Default HTTP port
                                "status": "active",
                                "weight": 1,
                                "max_fails": 3,
                                "fail_timeout": "30s",
                            }
                            vip["backend_servers"].append(backend_config)

                    # Process health checks
                    health_checks_edges = service.get("health_checks", [])
                    for check in health_checks_edges:
                        # Handle both edge format and direct format
                        check_data = (
                            check.get("node", check) if "node" in check else check
                        )
                        health_check_config = {
                            "check": check_data.get("check"),
                            "check_type": check_data.get("check"),
                            "rise": check_data.get("rise", 3),
                            "fall": check_data.get("fall", 3),
                            "timeout": check_data.get("timeout", 1000),
                        }
                        vip["health_checks"].append(health_check_config)

                    vips.append(vip)

        # Find management IP from interfaces
        management_ip = None
        for interface in data.get("interfaces", []):
            if interface.get("role") == "management" and interface.get("ip_addresses"):
                for ip in interface["ip_addresses"]:
                    management_ip = ip.get("address", "").split("/")[0]
                    break
                if management_ip:
                    break

        # Extract device information
        device_role = data.get("role", "load_balancer")
        device_status = data.get("status", "active")

        # Add processed data to template context
        data["vips"] = vips
        data["backend_servers"] = backend_servers
        data["load_balancer_config"] = {
            "device_role": device_role,
            "device_status": device_status,
            "management_ip": management_ip or "192.168.1.100",  # fallback
            "stats_port": 8404,  # HAProxy stats port
            "stats_user": "admin",
            "stats_password": "admin123",  # Should be configurable/encrypted
            "global_maxconn": 4096,
            "default_timeout_connect": "5000ms",
            "default_timeout_client": "50000ms",
            "default_timeout_server": "50000ms",
        }

        # Determine platform and manufacturer for template selection
        platform = data["device_type"]["platform"]["netmiko_device_type"]
        manufacturer = (
            data["device_type"]["manufacturer"]["name"].lower().replace(" ", "_")
        )

        # Set up Jinja2 environment to load templates from the loadbalancers subfolder
        template_path = f"{self.root_directory}/templates/configs/loadbalancers"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )

        # Select the template for load balancer devices
        # Try manufacturer_platform.j2 first, fallback to platform.j2
        template_name = f"{manufacturer}_{platform}.j2"
        try:
            template = env.get_template(template_name)
        except Exception:
            # Fallback to just platform name
            # template_name = f"{platform}.j2"
            try:
                template = env.get_template(template_name)
            except Exception:
                # Final fallback to generic haproxy
                template = env.get_template(template_name)

        # Render the template with the processed data
        rendered_config = template.render(data=data)

        # Return the rendered result
        return rendered_config
