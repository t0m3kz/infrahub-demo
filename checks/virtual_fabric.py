"""Validate Virtual Fabric deployment against design pattern constraints."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from .common import get_data


class CheckVirtualFabricCapacity(InfrahubCheck):
    """Check Virtual Fabric deployment against design pattern limits."""

    query = "virtual_fabric_validation"

    def validate(self, data: Any) -> None:
        """Validate Virtual Fabric resource allocation against design limits.

        Validates:
        - Network segment count vs design maximum
        - IP prefix count vs design maximum
        - External connection count vs design maximum
        - BGP session count vs design maximum
        - Load balancer count vs design maximum
        - Firewall rule count vs design maximum
        - Endpoint count vs design maximum
        """
        data = get_data(data)
        errors: list[str] = []

        if not data:
            self.log_error(message="No virtual fabric data found")
            return

        fabric_name = data.get("name", "Unknown")
        design_pattern = data.get("design_pattern", {})

        if not design_pattern:
            self.log_error(message=f"Virtual fabric '{fabric_name}' has no design pattern assigned")
            return

        # Get design limits
        max_segments = design_pattern.get("maximum_network_segments", 0)
        max_ip_prefixes = design_pattern.get("maximum_ip_prefixes", 0)
        max_external_conns = design_pattern.get("maximum_external_connections", 0)
        max_bgp_sessions = design_pattern.get("maximum_bgp_sessions", 0)
        max_load_balancers = design_pattern.get("maximum_load_balancers", 0)
        max_firewall_rules = design_pattern.get("maximum_firewall_rules", 0)
        max_endpoints = design_pattern.get("maximum_endpoints", 0)
        max_bandwidth = design_pattern.get("maximum_bandwidth_mbps", 0)

        # Get actual deployment counts (handle None values)
        actual_segments = data.get("network_segment_count") or 0
        actual_prefixes = data.get("ip_prefix_count") or 0
        actual_external_conns = data.get("external_connection_count") or 0
        actual_bgp_sessions = data.get("bgp_session_count") or 0
        actual_load_balancers = data.get("load_balancer_count") or 0
        actual_firewall_rules = data.get("firewall_rule_count") or 0
        actual_endpoints = data.get("endpoint_count") or 0
        actual_bandwidth = data.get("total_bandwidth_mbps") or 0

        # Validate network segments
        if actual_segments > max_segments:
            errors.append(f"Network segment count ({actual_segments}) exceeds design maximum ({max_segments})")

        # Validate IP prefixes
        if actual_prefixes > max_ip_prefixes:
            errors.append(f"IP prefix count ({actual_prefixes}) exceeds design maximum ({max_ip_prefixes})")

        # Validate external connections
        if actual_external_conns > max_external_conns:
            errors.append(
                f"External connection count ({actual_external_conns}) exceeds design maximum ({max_external_conns})"
            )

        # Validate BGP sessions
        if actual_bgp_sessions > max_bgp_sessions:
            errors.append(f"BGP session count ({actual_bgp_sessions}) exceeds design maximum ({max_bgp_sessions})")

        # Validate load balancers
        if actual_load_balancers > max_load_balancers:
            errors.append(
                f"Load balancer count ({actual_load_balancers}) exceeds design maximum ({max_load_balancers})"
            )

        # Validate firewall rules
        if actual_firewall_rules > max_firewall_rules:
            errors.append(
                f"Firewall rule count ({actual_firewall_rules}) exceeds design maximum ({max_firewall_rules})"
            )

        # Validate endpoints
        if actual_endpoints > max_endpoints:
            errors.append(f"Endpoint count ({actual_endpoints}) exceeds design maximum ({max_endpoints})")

        # Validate bandwidth
        if actual_bandwidth > max_bandwidth:
            errors.append(f"Total bandwidth ({actual_bandwidth} Mbps) exceeds design maximum ({max_bandwidth} Mbps)")

        # Display all errors
        if errors:
            for error in errors:
                self.log_error(message=error)
