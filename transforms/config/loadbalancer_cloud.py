from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template

from transforms.helpers.addressing import host_ip
from transforms.helpers.loadbalancer_pbr import pool_interfaces
from transforms.helpers.templates import load_template
from utils.data_cleaning import clean_data


def _members_to_targets(vip: dict) -> list[dict]:
    """Pool members with an address: name, ip (no prefix length), port, weight."""
    return [
        {"name": m.get("name", ""), "ip": ip, "port": pi.get("port"), "weight": m.get("weight", 1)}
        for m, pi in pool_interfaces(vip)
        for ip in [host_ip((pi.get("ip_address") or {}).get("address"))]
        if ip
    ]


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

        backend_addresses = [{"name": t["name"], "ip_address": t["ip"]} for t in _members_to_targets(vip)]

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

        targets = [{"name": t["name"], "ip": t["ip"]} for t in _members_to_targets(vip)]

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


_PREPARERS = {"aws": prepare_aws_data, "azure": prepare_azure_data, "gcp": prepare_gcp_data}


class LoadBalancerCloud(InfrahubTransform):
    """
    Transform to generate Terraform HCL (.tf) for cloud load balancers.

    Same "one template per platform" pattern as BaseDeviceTransform._load_template (leaf/spine/tor
    CLI configs), keyed on cloud provider instead of netmiko_device_type: each cloud gets its own
    template under templates/configs/loadbalancers_cloud/ shaped like that provider's actual
    Terraform resources (aws_lb/aws_lb_target_group, azurerm_lb/azurerm_lb_rule,
    google_compute_backend_service/forwarding_rule). prepare_aws_data/prepare_azure_data/
    prepare_gcp_data stay pure data-shaping functions (unchanged) — only the final rendering step
    moved from json.dumps to per-provider Jinja2 templates.

    Output is stored as an Infrahub artifact that can be pulled by CI/CD pipelines.

    Usage:
        Cloud provider is auto-detected from the load balancer's account provider
    """

    query = "loadbalancer_cloud"

    async def transform(self, data: Any) -> str:
        """Generate Terraform HCL content from CloudLoadBalancer data."""
        cleaned = clean_data(data)

        lbs = cleaned.get("CloudLoadBalancer") or []
        if not lbs:
            raise ValueError("No CloudLoadBalancer found in query result")
        lb = lbs[0]

        vips = cleaned.get("LoadbalancerVIP") or []

        # Cloud provider from the load balancer's virtual network account/provider; AWS by default.
        account = (lb.get("virtual_network") or {}).get("account") or {}
        provider_name = (account.get("provider") or {}).get("name", "").lower() if account else ""
        cloud_provider = provider_name if provider_name in _PREPARERS else "aws"

        config = _PREPARERS[cloud_provider](lb, vips)
        return self._load_template(cloud_provider).render(**config)

    def _load_template(self, name: str) -> Template:
        """Load the Jinja2 HCL template for the given cloud provider (aws/azure/gcp)."""
        return load_template(
            f"{self.root_directory}/templates/configs",
            f"loadbalancers_cloud/{name}.j2",
            filters={"tf_id": lambda value: str(value).replace(".", "_").replace("-", "_")},
        )
