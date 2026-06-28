import json
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from utils.data_cleaning import clean_data


def _members_to_targets(vip: dict) -> list[dict]:
    targets = []
    for m in vip.get("members", []):
        for pi in m.get("pool_interfaces", []):
            ip_obj = pi.get("ip_address") or {}
            ip = (ip_obj.get("address") or "").split("/")[0]
            if ip:
                targets.append(
                    {"name": m.get("name", ""), "ip": ip, "port": pi.get("port"), "weight": m.get("weight", 1)}
                )
    return targets


def prepare_aws_data(lb: dict, vips: list[dict]) -> dict:
    """Prepare load balancer data for AWS Terraform module."""
    pools = []
    listeners = []

    for vip in vips:
        listener = {
            "hostname": vip.get("hostname"),
            "port": vip.get("port"),
            "protocol": vip.get("protocol"),
            "load_balancing_algorithm": vip.get("load_balancing_algorithm"),
            "session_persistence": vip.get("session_persistence"),
        }
        if vip.get("ssl_certificate"):
            listener["certificate_arn"] = vip["ssl_certificate"]
        listeners.append(listener)

        targets = _members_to_targets(vip)
        pool_name = f"{vip.get('hostname')}-{vip.get('protocol')}-{vip.get('port')}"
        pool_data: dict = {"name": pool_name, "targets": targets}

        health_checks = vip.get("health_checks", [])
        if health_checks:
            hc = health_checks[0]
            pool_data["health_check"] = {
                "protocol": hc.get("check", "http"),
                "port": "traffic-port",
                "path": "/",
                "interval": 30,
                "timeout": hc.get("timeout", 1000) // 1000,
                "healthy_threshold": hc.get("rise", 3),
                "unhealthy_threshold": hc.get("fall", 3),
            }
        pools.append(pool_data)

    return {
        "lb_name": lb.get("name"),
        "lb_type": lb.get("lb_type"),
        "internal": lb.get("scheme") == "internal",
        "vpc_name": (lb.get("virtual_network") or {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_pools": pools,
        "listeners": listeners,
        "tags": {"Name": lb.get("name"), "ManagedBy": "Terraform", "Infrahub": "true"},
    }


def prepare_azure_data(lb: dict, vips: list[dict]) -> dict:
    """Prepare load balancer data for Azure Terraform module."""
    pools = []
    lb_rules = []

    for vip in vips:
        pool_name = f"{vip.get('hostname')}-{vip.get('protocol')}-{vip.get('port')}"
        lb_rules.append(
            {
                "name": f"{vip.get('hostname')}-rule",
                "protocol": vip.get("protocol"),
                "frontend_port": vip.get("port"),
                "backend_port": vip.get("port"),
                "backend_pool_name": pool_name,
            }
        )

        backend_addresses = []
        for m in vip.get("members", []):
            for pi in m.get("pool_interfaces", []):
                ip_obj = pi.get("ip_address") or {}
                ip = (ip_obj.get("address") or "").split("/")[0]
                if ip:
                    backend_addresses.append({"name": m.get("name", ""), "ip_address": ip})

        pool_data: dict = {"name": pool_name, "backend_addresses": backend_addresses}

        health_checks = vip.get("health_checks", [])
        if health_checks:
            hc = health_checks[0]
            probe: dict = {
                "protocol": hc.get("check", "http").upper(),
                "port": 80,
                "interval_in_seconds": 30,
                "number_of_probes": hc.get("fall", 3),
            }
            if hc.get("check", "").upper() in ["HTTP", "HTTPS"]:
                probe["request_path"] = "/"
            pool_data["health_probe"] = probe
        pools.append(pool_data)

    return {
        "lb_name": lb.get("name"),
        "sku": "Standard",
        "type": "Public" if lb.get("scheme") == "internet-facing" else "Private",
        "vnet_name": (lb.get("virtual_network") or {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_pools": pools,
        "lb_rules": lb_rules,
        "tags": {"Name": lb.get("name"), "ManagedBy": "Terraform", "Infrahub": "true"},
    }


def prepare_gcp_data(lb: dict, vips: list[dict]) -> dict:
    """Prepare load balancer data for GCP Terraform module."""
    backend_services = []
    health_checks_out = []
    forwarding_rules = []

    for vip in vips:
        pool_name = f"{vip.get('hostname')}-{vip.get('protocol')}-{vip.get('port')}"
        forwarding_rules.append(
            {
                "name": f"{vip.get('hostname')}-{vip.get('protocol', '').upper()}-{vip.get('port')}",
                "protocol": vip.get("protocol", "").upper(),
                "port_range": str(vip.get("port")),
                "is_global": lb.get("lb_type") == "application"
                and vip.get("protocol", "").upper() in ["HTTP", "HTTPS"],
                "backend_service": pool_name,
            }
        )

        targets = []
        for m in vip.get("members", []):
            for pi in m.get("pool_interfaces", []):
                ip_obj = pi.get("ip_address") or {}
                ip = (ip_obj.get("address") or "").split("/")[0]
                if ip:
                    targets.append({"name": m.get("name", ""), "ip": ip})

        backend_services.append(
            {
                "name": pool_name,
                "protocol": "HTTP" if lb.get("lb_type") == "application" else "TCP",
                "session_affinity": "NONE",
                "health_check": f"{pool_name}-health-check",
                "targets": targets,
            }
        )

        hcs = vip.get("health_checks", [])
        if hcs:
            hc = hcs[0]
            hc_data: dict = {
                "name": f"{pool_name}-health-check",
                "protocol": hc.get("check", "http").upper(),
                "port": 80,
                "check_interval_sec": 30,
                "timeout_sec": hc.get("timeout", 1000) // 1000,
                "healthy_threshold": hc.get("rise", 3),
                "unhealthy_threshold": hc.get("fall", 3),
            }
            if hc.get("check", "").upper() in ["HTTP", "HTTPS"]:
                hc_data["request_path"] = "/"
            health_checks_out.append(hc_data)

    return {
        "lb_name": lb.get("name"),
        "lb_type": lb.get("lb_type"),
        "network_name": (lb.get("virtual_network") or {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_services": backend_services,
        "health_checks": health_checks_out,
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
        cleaned = clean_data(data)

        lbs = cleaned.get("CloudLoadBalancer") or []
        if not lbs:
            raise ValueError("No CloudLoadBalancer found in query result")
        lb = lbs[0]

        vips = cleaned.get("LoadbalancerVIP") or []

        # Determine cloud provider from load balancer's virtual network account/provider
        cloud_provider = "aws"  # default
        vnet = lb.get("virtual_network") or {}
        account = vnet.get("account") or {}
        if account:
            provider_name = (account.get("provider") or {}).get("name", "").lower()
            if provider_name in ["aws", "azure", "gcp"]:
                cloud_provider = provider_name

        # Validate cloud provider
        if cloud_provider not in ["aws", "azure", "gcp"]:
            raise ValueError(f"Unsupported cloud provider: {cloud_provider}. Must be aws, azure, or gcp")

        # Prepare data for the specific cloud provider
        if cloud_provider == "aws":
            config = prepare_aws_data(lb, vips)
        elif cloud_provider == "azure":
            config = prepare_azure_data(lb, vips)
        else:  # gcp
            config = prepare_gcp_data(lb, vips)

        # Return JSON directly
        return json.dumps(config, indent=2)
