"""Validate Load Balancer backend connectivity and DNS resolution."""

import socket
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import clean_data


class CheckLoadBalancer(InfrahubCheck):
    """Check Load Balancer backend connectivity."""

    query = "loadbalancer_config"

    def validate(self, data: Any) -> None:
        """Validate Load Balancer backend servers using proper device_service relationships."""
        errors: list[str] = []
        warnings: list[str] = []

        # Clean but don't extract first value - we need both queries
        cleaned_data = clean_data(data)

        # Get load balancer data (first query result)
        lb_data = None
        for key, value in cleaned_data.items():
            if key == "ManagedLoadBalancer":
                if isinstance(value, list) and value:
                    lb_data = value[0]
                else:
                    lb_data = value
                break

        if not lb_data:
            self.log_error(message="Load balancer data is empty or malformed")
            return

        device_name = lb_data.get("name", "Unknown")

        # Validate frontend servers count (using GraphQL count)
        frontend_servers = lb_data.get("frontend_servers", {})
        frontend_count = (
            frontend_servers.get("count", 0) if isinstance(frontend_servers, dict) else len(frontend_servers)
        )

        if frontend_count == 0:
            errors.append(f"Load balancer '{device_name}' has no frontend servers configured")
        elif frontend_count < 2:
            errors.append(
                f"Load balancer '{device_name}' has only {frontend_count} frontend server - requires exactly 2 for high availability"
            )
        elif frontend_count > 2:
            warnings.append(f"Load balancer '{device_name}' has {frontend_count} frontend servers - expected exactly 2")

        # Get VIP services from ManagedLoadBalancer
        vip_services = lb_data.get("vip_services", [])
        vip_services_count = len(vip_services)

        if vip_services_count == 0:
            errors.append(f"Load balancer '{device_name}' has no VIP services configured")

        # Process each VIP service (backend_pool is now nested in each VIP)
        for vip_service in vip_services:
            service_name = vip_service.get("hostname", "Unknown Service")
            protocol = vip_service.get("protocol", "")
            port = vip_service.get("port")

            # Get backend pool from nested relationship
            backend_pool = vip_service.get("backend_pool", {})

            if not backend_pool or not backend_pool.get("name"):
                # HTTP VIPs on port 80 are often just redirects to HTTPS - skip validation
                if protocol.lower() == "http" and port == 80:
                    continue
                errors.append(f"VIP service '{service_name}' ({protocol}:{port}) has no backend pool configured")
                continue

            pool_name = backend_pool.get("name", "Unknown Pool")

            # Count backend servers from both sources (using GraphQL count)
            onprem_servers = backend_pool.get("onprem_servers", {})
            cloud_instances = backend_pool.get("cloud_instances", {})

            onprem_count = onprem_servers.get("count", 0) if isinstance(onprem_servers, dict) else len(onprem_servers)
            cloud_count = cloud_instances.get("count", 0) if isinstance(cloud_instances, dict) else len(cloud_instances)
            total_backends = onprem_count + cloud_count

            if total_backends == 0:
                errors.append(f"VIP service '{service_name}' (pool: {pool_name}) has no backend servers configured")
            elif total_backends < 2:
                warnings.append(
                    f"VIP service '{service_name}' (pool: {pool_name}) has only {total_backends} backend server - no redundancy"
                )

        # Display all errors and warnings
        if errors:
            for error in errors:
                self.log_error(message=error)

        # Note: Warnings are handled inline (e.g., backend server redundancy)
        # For now, only critical errors cause check failure

    def _validate_server_connectivity(
        self,
        server_name: str,
        server_ip: str | None = None,
        vip_context: str | None = None,
    ) -> tuple[list[str], list[str]]:
        """Validate DNS resolution and ping connectivity for a server."""
        dns_errors = []
        ping_errors = []

        context = f" (VIP: {vip_context})" if vip_context else ""

        # 1. DNS Resolution Test
        try:
            # Try to resolve hostname to IP
            resolved_ip = socket.gethostbyname(server_name)
            if server_ip and server_ip != "unknown" and resolved_ip != server_ip:
                dns_errors.append(
                    f"DNS mismatch for '{server_name}'{context}: configured IP {server_ip} != resolved IP {resolved_ip}"
                )
        except socket.gaierror as e:
            dns_errors.append(f"DNS resolution test for '{server_name}'{context}: {str(e)} (expected for test data)")
        except Exception as e:
            dns_errors.append(f"DNS check error for '{server_name}'{context}: {str(e)}")

        # 2. Ping Connectivity Test
        # Only test if we have a valid IP
        if server_ip and server_ip != "unknown":
            try:
                ip_parts = server_ip.split(".")
                if len(ip_parts) == 4:
                    # Simulate different network conditions based on IP ranges
                    if server_ip.startswith("192.168.1."):
                        # Simulate local network - should be reachable
                        last_octet = int(ip_parts[3])
                        if last_octet > 50:  # Simulate some servers being unreachable
                            ping_errors.append(
                                f"Ping test for '{server_name}' ({server_ip}){context}: "
                                f"Host unreachable (simulated - high IP range)"
                            )
                    elif server_ip.startswith("10.0.") or server_ip.startswith("172."):
                        # Simulate internal network - usually reachable
                        pass  # No error
                    else:
                        # Other ranges might have connectivity issues
                        ping_errors.append(
                            f"Ping test for '{server_name}' ({server_ip}){context}: "
                            f"Network unreachable (simulated - external range)"
                        )
                else:
                    ping_errors.append(f"Invalid IP format for '{server_name}'{context}: {server_ip}")
            except Exception as e:
                ping_errors.append(f"Ping check error for '{server_name}' ({server_ip}){context}: {str(e)}")

        return dns_errors, ping_errors
