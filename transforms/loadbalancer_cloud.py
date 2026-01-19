import json
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


def prepare_aws_data(lb: dict) -> dict:
    """Prepare load balancer data for AWS Terraform module."""
    backend_pools = []
    listeners = []

    # Process VIP services and their backend pools
    for vip in lb.get("vip_services", []):
        # Add listener
        listener = {
            "hostname": vip.get("hostname"),
            "port": vip.get("port"),
            "protocol": vip.get("protocol"),
        }
        if vip.get("ssl_certificate"):
            listener["certificate_arn"] = vip["ssl_certificate"]
        listeners.append(listener)

        # Add backend pool if exists and not already added
        backend_pool = vip.get("backend_pool")
        if backend_pool:
            pool_name = backend_pool.get("name")
            if not any(p["name"] == pool_name for p in backend_pools):
                targets = []

                # Add onprem servers
                for server in backend_pool.get("onprem_servers", []):
                    if server.get("primary_ip"):
                        targets.append({"name": server["name"], "ip": server["primary_ip"]["address"].split("/")[0]})

                # Add cloud instances
                for instance in backend_pool.get("cloud_instances", []):
                    targets.append({"name": instance["name"], "id": instance.get("cloud_id", "")})

                pool_data = {"name": pool_name, "targets": targets}

                # Get health check from VIP service (not backend pool)
                health_checks = vip.get("health_checks", [])
                if health_checks:
                    health_check = health_checks[0]  # Use first health check
                    pool_data["health_check"] = {
                        "protocol": health_check.get("check", "http"),  # check → protocol (http/tcp/ssl)
                        "port": "traffic-port",  # Not in schema, use traffic port
                        "path": "/",  # Not in schema, default for HTTP
                        "interval": 30,  # Not in schema, use default (30 seconds)
                        "timeout": health_check.get("timeout", 1000) // 1000,  # Convert ms to seconds
                        "healthy_threshold": health_check.get("rise", 3),
                        "unhealthy_threshold": health_check.get("fall", 3),
                    }

                backend_pools.append(pool_data)

    return {
        "lb_name": lb.get("name"),
        "lb_type": lb.get("lb_type"),
        "internal": lb.get("scheme") == "internal",
        "vpc_name": lb.get("virtual_network", {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_pools": backend_pools,
        "listeners": listeners,
        "tags": {"Name": lb.get("name"), "ManagedBy": "Terraform", "Infrahub": "true"},
    }


def prepare_azure_data(lb: dict) -> dict:
    """Prepare load balancer data for Azure Terraform module."""
    backend_pools = []
    lb_rules = []

    # Process VIP services and their backend pools
    for vip in lb.get("vip_services", []):
        # Add load balancing rule
        lb_rules.append(
            {
                "name": f"{vip.get('hostname')}-rule",
                "protocol": vip.get("protocol"),
                "frontend_port": vip.get("port"),
                "backend_port": vip.get("port"),
                "backend_pool_name": vip.get("backend_pool", {}).get("name") if vip.get("backend_pool") else None,
            }
        )

        # Add backend pool if exists and not already added
        backend_pool = vip.get("backend_pool")
        if backend_pool:
            pool_name = backend_pool.get("name")
            if not any(p["name"] == pool_name for p in backend_pools):
                backend_addresses = []

                # Add onprem servers
                for server in backend_pool.get("onprem_servers", []):
                    if server.get("ip_address"):
                        backend_addresses.append(
                            {"name": server["hostname"], "ip_address": server["ip_address"]["address"].split("/")[0]}
                        )

                # Add cloud instances
                for instance in backend_pool.get("cloud_instances", []):
                    backend_addresses.append(
                        {
                            "name": instance.get("hostname") or instance.get("name", ""),
                            "id": instance.get("cloud_id", ""),
                        }
                    )

                pool_data = {"name": pool_name, "backend_addresses": backend_addresses}

                # Get health probe from VIP service (not backend pool)
                health_checks = vip.get("health_checks", [])
                if health_checks:
                    health_check = health_checks[0]  # Use first health check
                    probe = {
                        "protocol": health_check.get("check", "http").upper(),  # check → protocol
                        "port": 80,  # Default port, not in schema
                        "interval_in_seconds": 30,  # Not in schema, use default
                        "number_of_probes": health_check.get("fall", 3),  # fall → number_of_probes
                    }
                    if health_check.get("check", "").upper() in ["HTTP", "HTTPS"]:
                        probe["request_path"] = "/"  # Default path, not in schema
                    pool_data["health_probe"] = probe

                backend_pools.append(pool_data)

    return {
        "lb_name": lb.get("name"),
        "sku": "Standard",
        "type": "Public" if lb.get("scheme") == "internet-facing" else "Private",
        "vnet_name": lb.get("virtual_network", {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_pools": backend_pools,
        "lb_rules": lb_rules,
        "tags": {"Name": lb.get("name"), "ManagedBy": "Terraform", "Infrahub": "true"},
    }


def prepare_gcp_data(lb: dict) -> dict:
    """Prepare load balancer data for GCP Terraform module."""
    backend_services = []
    health_checks = []
    forwarding_rules = []

    # Process VIP services and their backend pools
    for vip in lb.get("vip_services", []):
        # Add forwarding rule
        forwarding_rules.append(
            {
                "name": f"{vip.get('hostname')}-{vip.get('protocol', '').upper()}-{vip.get('port')}",
                "protocol": vip.get("protocol", "").upper(),
                "port_range": str(vip.get("port")),
                "is_global": lb.get("lb_type") == "application"
                and vip.get("protocol", "").upper() in ["HTTP", "HTTPS"],
                "backend_service": vip.get("backend_pool", {}).get("name") if vip.get("backend_pool") else None,
            }
        )

        # Add backend service if exists and not already added
        backend_pool = vip.get("backend_pool")
        if backend_pool:
            pool_name = backend_pool.get("name")
            if not any(s["name"] == pool_name for s in backend_services):
                targets = []

                # Add onprem servers
                for server in backend_pool.get("onprem_servers", []):
                    if server.get("ip_address"):
                        targets.append(
                            {"name": server["hostname"], "ip": server["ip_address"]["address"].split("/")[0]}
                        )

                # Add cloud instances
                for instance in backend_pool.get("cloud_instances", []):
                    targets.append({"name": instance.get("hostname") or instance.get("name", "")})

                # Map load balancing algorithm
                algorithm = backend_pool.get("load_balancing_algorithm", "")
                session_affinity = {"source_ip": "CLIENT_IP", "source_ip_proto": "CLIENT_IP_PROTO"}.get(
                    algorithm, "NONE"
                )

                backend_services.append(
                    {
                        "name": pool_name,
                        "protocol": "HTTP" if lb.get("lb_type") == "application" else "TCP",
                        "session_affinity": session_affinity,
                        "health_check": f"{pool_name}-health-check",
                        "targets": targets,
                    }
                )

                # Get health check from VIP service (not backend pool)
                health_checks = vip.get("health_checks", [])
                if health_checks:
                    health_check = health_checks[0]  # Use first health check
                    hc_data = {
                        "name": f"{pool_name}-health-check",
                        "protocol": health_check.get("check", "http").upper(),  # check → protocol
                        "port": 80,  # Default port, not in schema
                        "check_interval_sec": 30,  # Not in schema, use default
                        "timeout_sec": health_check.get("timeout", 1000) // 1000,  # Convert ms to seconds
                        "healthy_threshold": health_check.get("rise", 3),
                        "unhealthy_threshold": health_check.get("fall", 3),
                    }
                    if health_check.get("check", "").upper() in ["HTTP", "HTTPS"]:
                        hc_data["request_path"] = "/"  # Default path, not in schema
                    health_checks.append(hc_data)

    return {
        "lb_name": lb.get("name"),
        "lb_type": lb.get("lb_type"),
        "network_name": lb.get("virtual_network", {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_services": backend_services,
        "health_checks": health_checks,
        "forwarding_rules": forwarding_rules,
        "labels": {"name": lb.get("name", "").replace("-", "_"), "managed_by": "terraform", "infrahub": "true"},
    }


class LoadBalancerCloud(InfrahubTransform):
    """
    Transform to generate terraform.tfvars.json for cloud load balancers.

    Supports multiple cloud providers (AWS, Azure, GCP) using Jinja2 templates.
    Output is stored as an Infrahub artifact that can be pulled by CI/CD pipelines.

    Usage:
        Cloud provider is auto-detected from the load balancer's account provider
    """

    query = "loadbalancer_cloud"

    async def transform(self, data: Any) -> str:
        """Generate terraform.tfvars.json content from CloudLoadBalancer data."""
        # get_data() already extracts and cleans the first LB from edges
        lb = get_data(data)

        if not lb:
            raise ValueError("No CloudLoadBalancer found in query result")

        # Determine cloud provider from load balancer's virtual network account/provider
        cloud_provider = "aws"  # default
        if lb.get("virtual_network") and lb["virtual_network"].get("account"):
            provider_name = lb["virtual_network"]["account"].get("provider", {}).get("name", "").lower()
            if provider_name in ["aws", "azure", "gcp"]:
                cloud_provider = provider_name

        # Validate cloud provider
        if cloud_provider not in ["aws", "azure", "gcp"]:
            raise ValueError(f"Unsupported cloud provider: {cloud_provider}. Must be aws, azure, or gcp")

        # Prepare data for the specific cloud provider
        if cloud_provider == "aws":
            config = prepare_aws_data(lb)
        elif cloud_provider == "azure":
            config = prepare_azure_data(lb)
        else:  # gcp
            config = prepare_gcp_data(lb)

        # Return JSON directly
        return json.dumps(config, indent=2)
