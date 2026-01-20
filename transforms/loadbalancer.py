from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Environment, FileSystemLoader, select_autoescape
from netutils.utils import jinja2_convenience_function



class LoadBalancer(InfrahubTransform):
    query = "loadbalancer_config"

    async def transform(self, data: Any) -> Any:
        # Clean the data
        from utils.data_cleaning import clean_data

        cleaned_data = clean_data(data)

        # Get load balancer data
        lb_data = None
        for key, value in cleaned_data.items():
            if key == "ManagedLoadBalancer":
                if isinstance(value, list) and value:
                    lb_data = value[0]
                else:
                    lb_data = value
                break

        if not lb_data:
            raise ValueError("No ManagedLoadBalancer data found in response")

        # Extract VIPs and process them for load balancer configuration
        vips = []

        # Process VIP services (backend_pool is now nested in each VIP)
        vip_services = lb_data.get("vip_services", [])
        for vip_service in vip_services:
            # Get backend pool from nested relationship
            backend_pool = vip_service.get("backend_pool", {})

            if not backend_pool or not backend_pool.get("name"):
                continue

            # Extract values - already flattened by clean_data
            hostname = vip_service.get("hostname", "unknown")
            protocol = vip_service.get("protocol", "http")
            port = vip_service.get("port", 80)
            ssl_cert = vip_service.get("ssl_certificate")
            lb_algorithm = backend_pool.get("load_balancing_algorithm", "roundrobin")

            # Get VIP IP address
            vip_ip_node = vip_service.get("vip_ip", {})
            vip_ip = vip_ip_node.get("address", "") if vip_ip_node else ""

            # Create VIP entry with typed lists
            backend_servers: list[dict[str, Any]] = []
            health_checks_list: list[dict[str, Any]] = []

            vip = {
                "hostname": hostname,
                "protocol": protocol,
                "port": port,
                "vip_ip": vip_ip.split("/")[0] if vip_ip else "*",  # Remove CIDR, fallback to wildcard
                "mode": "tcp" if protocol == "tcp" else "http",
                "balance": lb_algorithm.replace("_", ""),
                "ssl_certificate": ssl_cert,
                "backend_servers": backend_servers,
                "health_checks": health_checks_list,
            }

            # Process onprem backend servers
            onprem_servers = backend_pool.get("onprem_servers", [])
            for server in onprem_servers:
                hostname_val = server.get("hostname", "unknown")
                server_ip = server.get("ip_address", {}).get("address", "").split("/")[0]

                if server_ip:
                    backend_config = {
                        "hostname": hostname_val,
                        "ip_address": server_ip,
                        "port": port,
                        "status": "active",
                    }
                    backend_servers.append(backend_config)

            # Process cloud backend instances
            cloud_instances = backend_pool.get("cloud_instances", [])
            for instance in cloud_instances:
                instance_name = instance.get("name", "unknown")

                # For cloud instances, use name as identifier
                backend_config = {
                    "hostname": instance_name,
                    "ip_address": "10.0.0.1",  # Placeholder - would need primary_ip from schema
                    "port": port,
                    "status": "active",
                }
                backend_servers.append(backend_config)

            # Process health checks
            health_checks = vip_service.get("health_checks", [])
            for check in health_checks:
                health_check_config = {
                    "check": check.get("check", "http"),
                    "check_type": check.get("check", "http"),
                    "rise": check.get("rise", 3),
                    "fall": check.get("fall", 3),
                    "timeout": check.get("timeout", 1000),
                }
                health_checks_list.append(health_check_config)

            vips.append(vip)

        # Set management IP placeholder
        management_ip = "192.168.1.100"  # Placeholder - ManagedLoadBalancer doesn't have interfaces

        # Extract device information from ManagedLoadBalancer
        device_role = "load-balancer"
        device_status = lb_data.get("status", "active")

        # Add processed data to template context
        lb_data["vips"] = vips
        lb_data["loadbalancer_config"] = {
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

        # Use lb_vendor to select the appropriate template for ManagedLoadBalancer
        lb_vendor = lb_data.get("lb_vendor", "haproxy")

        # Map lb_vendor to template name
        template_map = {
            "haproxy": "haproxy_technologies_linux.j2",
            "nginx": "haproxy_technologies_linux.j2",  # Use HAProxy template for Nginx (similar syntax)
            "f5_bigip": "f5_networks_linux.j2",
            "a10_networks": "a10_networks_linux.j2",
            "kemp": "haproxy_technologies_linux.j2",  # Use HAProxy template as fallback
        }

        # Get template name or default to HAProxy
        template_name = template_map.get(lb_vendor, "haproxy_technologies_linux.j2")

        # Set up Jinja2 environment to load templates from the loadbalancers subfolder
        template_path = f"{self.root_directory}/templates/configs/loadbalancers"
        env = Environment(
            loader=FileSystemLoader(template_path),
            autoescape=select_autoescape(["j2"]),
        )
        env.filters.update(jinja2_convenience_function())

        # Load the selected template
        template = env.get_template(template_name)
        # Render the template with the processed data
        rendered_config = template.render(**lb_data)

        # Return the rendered result
        return rendered_config
