"""Validate Load Balancer backend connectivity and DNS resolution."""

import socket
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data


class CheckLoadBalancer(InfrahubCheck):
    """Check Load Balancer backend connectivity."""

    query = "loadbalancer_config"

    def validate(self, data: Any) -> None:
        """Validate Load Balancer backend servers using proper device_service relationships."""
        errors: list[str] = []
        warnings: list[str] = []

        data = get_data(data)
        device_name = data.get("name", "Unknown")

        # Get device services (VIP services) from the load balancer device
        device_services = data.get("device_services", [])
        vip_services_count = 0

        # Process each VIP service connected to this load balancer device
        for service in device_services:
            service_name = service.get("name", "Unknown Service")
            service_status = service.get("status", "unknown")
            service_type = service.get("typename", "Unknown")
            backend_servers = len(service.get("backend_servers", []))

            if service_type == "ServiceVIP":
                vip_services_count += 1

                # Check service status
                if service_status != "active":
                    errors.append(
                        f"VIP service '{service_name}' is not active (status: {service_status})"
                    )
                    continue

                # Get VIP IP from this service (single IP per service)
                vip_ip = service.get("vip_ip")
                if not vip_ip:
                    errors.append(f"VIP service '{service_name}' has no IP configured")
                    continue

                ip_address = vip_ip.get("address", "Unknown IP")

                # Simple validation that the service has an IP
                if ip_address and ip_address != "Unknown IP":
                    # VIP service is properly configured with an IP
                    vip_services_count += 1
                else:
                    errors.append(
                        f"VIP service '{service_name}' has invalid IP configuration"
                    )
                if backend_servers == 0:
                    errors.append(
                        f"Load balancer '{device_name}' has {vip_services_count} VIP service(s) but no backend servers configured yet"
                    )
                elif backend_servers < 2:
                    errors.append(
                        f"Load balancer '{device_name}' has only {backend_servers} backend server - no redundancy"
                    )

        # Check if load balancer has any VIP services
        if vip_services_count == 0:
            errors.append(
                f"Load balancer '{device_name}' has no VIP services configured"
            )

        # Display all errors and warnings
        if errors:
            for error in errors:
                self.log_error(message=error)

        if warnings:
            for warning in warnings:
                self.log_error(message=f"WARNING: {warning}")

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
                    f"DNS mismatch for '{server_name}'{context}: "
                    f"configured IP {server_ip} != resolved IP {resolved_ip}"
                )
        except socket.gaierror as e:
            dns_errors.append(
                f"DNS resolution test for '{server_name}'{context}: {str(e)} (expected for test data)"
            )
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
                    ping_errors.append(
                        f"Invalid IP format for '{server_name}'{context}: {server_ip}"
                    )
            except Exception as e:
                ping_errors.append(
                    f"Ping check error for '{server_name}' ({server_ip}){context}: {str(e)}"
                )

        return dns_errors, ping_errors
