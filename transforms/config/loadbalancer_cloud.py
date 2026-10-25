from typing import Any

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template

from transforms.cloud.terraform_common import tf_id
from transforms.helpers.loadbalancer import pool_member_ips
from transforms.helpers.templates import load_template
from utils.data_cleaning import clean_data

AWS_LB_TYPES = {"l4_cloud": "network", "l7_cloud": "application", "gateway": "gateway", "classic": "classic"}
"""ManagedLoadBalancer.lb_type -> aws_lb load_balancer_type (an unset lb_type renders AWS's default, application)."""

PUBLIC_SCHEMES = frozenset({"internet_facing", "external"})
"""ManagedLoadBalancer.scheme choices whose frontend is a public address."""

_HTTP_CHECKS = frozenset({"http", "https"})


def _pool_name(vip: dict) -> str:
    """The backend pool / target group / backend service of one VIP: hostname-protocol-port."""
    return f"{vip.get('hostname')}-{vip.get('protocol')}-{vip.get('port')}"


def _aws_lb_type(lb: dict) -> str:
    """The aws_lb load_balancer_type of the load balancer; a type AWS has no equivalent for raises ValueError."""
    lb_type = lb.get("lb_type")
    if lb_type is None:
        return "application"
    if lb_type not in AWS_LB_TYPES:
        raise ValueError(f"CloudLoadBalancer {lb.get('name')}: lb_type {lb_type} has no AWS load_balancer_type")
    return AWS_LB_TYPES[lb_type]


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

        pool_data: dict = {
            "name": _pool_name(vip),
            "port": vip.get("port"),
            "protocol": (vip.get("protocol") or "").upper(),
            "targets": pool_member_ips(vip),
        }

        health_checks = vip.get("health_checks", [])
        if health_checks:
            hc = health_checks[0]
            check = hc.get("check", "http")
            pool_data["health_check"] = {
                "protocol": check,
                "port": "traffic-port",
                "interval": 30,
                "timeout": hc.get("timeout", 1000) // 1000,
                "healthy_threshold": hc.get("rise", 3),
                "unhealthy_threshold": hc.get("fall", 3),
            }
            if check in _HTTP_CHECKS:
                pool_data["health_check"]["path"] = "/"
        pools.append(pool_data)

    return {
        "lb_name": lb.get("name"),
        "lb_type": _aws_lb_type(lb),
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
        pool_name = _pool_name(vip)
        lb_rules.append(
            {
                "name": f"{pool_name}-rule",
                "protocol": vip.get("protocol"),
                "frontend_port": vip.get("port"),
                "backend_port": vip.get("port"),
                "backend_pool_name": pool_name,
            }
        )

        pool_data: dict = {"name": pool_name, "addresses": pool_member_ips(vip)}

        health_checks = vip.get("health_checks", [])
        if health_checks:
            hc = health_checks[0]
            probe: dict = {
                "protocol": hc.get("check", "http").upper(),
                "port": vip.get("port"),
                "interval_in_seconds": 30,
                "number_of_probes": hc.get("fall", 3),
            }
            if hc.get("check", "") in _HTTP_CHECKS:
                probe["request_path"] = "/"
            pool_data["health_probe"] = probe
        pools.append(pool_data)

    return {
        "lb_name": lb.get("name"),
        "sku": "Standard",
        "type": "Public" if lb.get("scheme") in PUBLIC_SCHEMES else "Private",
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
    l7 = lb.get("lb_type") == "l7_cloud"

    for vip in vips:
        pool_name = _pool_name(vip)
        protocol = (vip.get("protocol") or "").upper()
        forwarding_rules.append(
            {
                "name": f"{vip.get('hostname')}-{protocol}-{vip.get('port')}",
                "protocol": protocol,
                "port_range": str(vip.get("port")),
                "is_global": l7 and protocol in ("HTTP", "HTTPS"),
                "backend_service": pool_name,
            }
        )

        backend_services.append(
            {
                "name": pool_name,
                "protocol": "HTTP" if l7 else ("UDP" if protocol == "UDP" else "TCP"),
                "session_affinity": "NONE",
                "health_check": f"{pool_name}-health-check",
                "port": vip.get("port"),
                "endpoints": pool_member_ips(vip),
            }
        )

        hcs = vip.get("health_checks", [])
        if hcs:
            hc = hcs[0]
            hc_data: dict = {
                "name": f"{pool_name}-health-check",
                "protocol": hc.get("check", "http").upper(),
                "port": vip.get("port"),
                "check_interval_sec": 30,
                "timeout_sec": hc.get("timeout", 1000) // 1000,
                "healthy_threshold": hc.get("rise", 3),
                "unhealthy_threshold": hc.get("fall", 3),
            }
            if hc.get("check", "") in _HTTP_CHECKS:
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

_PROVIDER_KEYS = {"aws": "aws", "azure": "azure", "gcp": "gcp", "google cloud": "gcp"}
"""Lower-cased OrganizationProvider name -> preparer/template key (data/bootstrap/02_providers.yml: AWS, Azure, Google Cloud)."""


class LoadBalancerCloud(InfrahubTransform):
    """
    Transform to generate Terraform HCL (.tf) for cloud load balancers.

    Same "one template per platform" pattern as BaseDeviceTransform._load_template (leaf/spine/tor
    CLI configs), keyed on cloud provider instead of the device platform: each cloud gets its own
    template under templates/configs/loadbalancers_cloud/ shaped like that provider's actual
    Terraform resources (aws_lb/aws_lb_target_group/attachments, azurerm_lb/backend pool
    addresses/azurerm_lb_rule, google_compute_backend_service/network endpoints/forwarding_rule).
    prepare_aws_data/prepare_azure_data/prepare_gcp_data are pure data-shaping functions: one
    listener/rule/forwarding rule per LoadbalancerVIP, its pool members (the cloud instances'
    addresses, VIP.pool_members) as IP targets on the VIP's own port and protocol.

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
        cloud_provider = _PROVIDER_KEYS.get(provider_name, "aws")

        config = _PREPARERS[cloud_provider](lb, vips)
        return self._load_template(cloud_provider).render(**config)

    def _load_template(self, name: str) -> Template:
        """Load the Jinja2 HCL template for the given cloud provider (aws/azure/gcp)."""
        return load_template(
            f"{self.root_directory}/templates/configs",
            f"loadbalancers_cloud/{name}.j2",
            filters={"tf_id": lambda value: tf_id(str(value))},
        )
