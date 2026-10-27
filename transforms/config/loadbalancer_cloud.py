import ipaddress
from typing import Any
from urllib.parse import urlparse

from infrahub_sdk.transforms import InfrahubTransform
from jinja2 import Template

from transforms.cloud.terraform_common import tf_id
from transforms.helpers.addressing import host_ip
from transforms.helpers.templates import load_template
from utils.data_cleaning import clean_data

AWS_LB_TYPES = {"l4_cloud": "network", "l7_cloud": "application", "gateway": "gateway", "classic": "classic"}
"""ManagedLoadBalancer.lb_type -> aws_lb load_balancer_type (an unset lb_type renders AWS's default, application)."""

PUBLIC_SCHEMES = frozenset({"internet_facing", "external"})
"""ManagedLoadBalancer.scheme choices whose frontend is a public address."""

_HTTP_CHECKS = frozenset({"http", "https"})

L7_PROTOCOLS = frozenset({"http", "https"})
"""The VIP protocols a layer 7 load balancer carries; a layer 4 (l4_cloud) one carries the others."""

HC_INTERVAL_S = 30
"""Probe interval of every cloud health check (the model has none)."""

L7_HEALTH_PATH = "/"
L7_HEALTH_STATUS = "200-399"
"""The request path and the success status range of a layer 7 http probe (the model has neither)."""

L7_HEALTH_MIN_TIMEOUT_S = 2
"""The shortest probe timeout the layer 7 gateways accept (an ALB refuses 1 s)."""

AWS_SSL_POLICY = "ELBSecurityPolicy-TLS13-1-2-2021-06"
"""The ssl_policy of an ALB https listener."""

AZURE_GATEWAY_SKU = "Standard_v2"
"""The Application Gateway sku (name and tier) of a layer 7 load balancer."""


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


def _instance_ips(lb: dict, vip: dict) -> list[str]:
    """The distinct private IPs of the VIP's cloud instances, in address order (Azure and GCP targets).

    Unlike the on-prem pool_member_ips, an instance without a private_ip raises: a listener must not silently lose
    a target.
    """
    ips = set()
    for member in vip.get("pool_members") or []:
        if not (ip := host_ip(member.get("private_ip"))):
            raise ValueError(f"CloudLoadBalancer {lb.get('name')}: instance {member.get('name')} has no private_ip")
        ips.add(ip)
    return sorted(ips, key=ipaddress.ip_address)


def _instance_ids(lb: dict, vip: dict) -> list[str]:
    """The distinct cloud ids (``i-...``) of the VIP's cloud instances, sorted (AWS targets, registered by id).

    An instance without a cloud_id raises.
    """
    ids = set()
    for member in vip.get("pool_members") or []:
        if not (cloud_id := member.get("cloud_id")):
            raise ValueError(f"CloudLoadBalancer {lb.get('name')}: instance {member.get('name')} has no cloud_id")
        ids.add(cloud_id)
    return sorted(ids)


def _layer(lb: dict, vips: list[dict]) -> str:
    """The layer ("l4" or "l7") the load balancer renders, after checking that its VIPs fit it.

    Only an l7_cloud load balancer renders as layer 7: its VIPs are http, or https with an ssl_certificate. An
    l4_cloud one refuses http and https. Any other lb_type (or none) keeps the layer 4 rendering without a check.
    """
    lb_type = lb.get("lb_type")
    for vip in vips:
        where = f"CloudLoadBalancer {lb.get('name')}: VIP {vip.get('hostname')} {vip.get('protocol')}/{vip.get('port')}"
        protocol = vip.get("protocol")
        if lb_type == "l7_cloud":
            if protocol not in L7_PROTOCOLS:
                raise ValueError(f"{where}: a layer 7 (l7_cloud) load balancer carries http or https")
            if protocol == "https" and not vip.get("ssl_certificate"):
                raise ValueError(f"{where}: a layer 7 load balancer terminates TLS and needs an ssl_certificate")
        elif lb_type == "l4_cloud" and protocol in L7_PROTOCOLS:
            raise ValueError(f"{where}: a layer 4 (l4_cloud) load balancer carries tcp or udp")
    return "l7" if lb_type == "l7_cloud" else "l4"


def _l7_health(vip: dict, *, tcp_probe: bool) -> dict | None:
    """The one probe of a layer 7 VIP, None without a health check.

    An http check is the probe (``HTTP`` on ``L7_HEALTH_PATH``, success ``L7_HEALTH_STATUS``). A tcp/ssl check is a
    TCP probe where the provider has one (``tcp_probe``); where it has not (an ALB and an Application Gateway probe
    over HTTP only) the check supplies the timers and the probe stays HTTP.
    """
    checks = vip.get("health_checks") or []
    if not checks:
        return None
    check = next((c for c in checks if c.get("check") == "http"), checks[0])
    tcp = tcp_probe and check.get("check") != "http"
    return {
        "protocol": "TCP" if tcp else "HTTP",
        "path": None if tcp else L7_HEALTH_PATH,
        "status": None if tcp else L7_HEALTH_STATUS,
        "interval": HC_INTERVAL_S,
        "timeout": max(L7_HEALTH_MIN_TIMEOUT_S, (check.get("timeout") or 1000) // 1000),
        "rise": check.get("rise") or 3,
        "fall": check.get("fall") or 3,
    }


def _azure_certificate(reference: str) -> dict:
    """The Application Gateway ssl_certificate of a VIP's ssl_certificate: ``name`` and, for a key vault secret id
    (``https://<vault>.vault.azure.net/secrets/<name>[/<version>]``), that ``secret_id``; a plain certificate name
    has none (the template looks the secret up by name)."""
    if not reference.startswith("https://"):
        return {"name": reference, "secret_id": None}
    parts = [part for part in urlparse(reference).path.split("/") if part]
    if len(parts) < 2 or parts[0] != "secrets":
        raise ValueError(f"ssl_certificate {reference} is neither a certificate name nor a key vault secret id")
    return {"name": parts[1], "secret_id": reference}


def prepare_aws_data(lb: dict, vips: list[dict]) -> dict:
    """Prepare load balancer data for AWS Terraform module."""
    layer = _layer(lb, vips)
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
        if vip.get("ssl_certificate") and (layer == "l4" or vip.get("protocol") == "https"):
            listener["certificate_arn"] = vip["ssl_certificate"]
            if layer == "l7":
                listener["ssl_policy"] = AWS_SSL_POLICY
        listeners.append(listener)

        pool_data: dict = {
            "name": _pool_name(vip),
            "port": vip.get("port"),
            # a layer 7 listener terminates TLS: the targets are served plain HTTP
            "protocol": "HTTP" if layer == "l7" else (vip.get("protocol") or "").upper(),
            "target_type": "instance",
            "targets": _instance_ids(lb, vip),
        }

        if layer == "l7":
            probe = _l7_health(vip, tcp_probe=False)
            if probe:
                pool_data["health_check"] = {
                    "protocol": probe["protocol"],
                    "port": "traffic-port",
                    "path": probe["path"],
                    "matcher": probe["status"],
                    "interval": probe["interval"],
                    "timeout": probe["timeout"],
                    "healthy_threshold": probe["rise"],
                    "unhealthy_threshold": probe["fall"],
                }
        elif health_checks := vip.get("health_checks", []):
            hc = health_checks[0]
            check = hc.get("check", "http")
            pool_data["health_check"] = {
                "protocol": check,
                "port": "traffic-port",
                "interval": HC_INTERVAL_S,
                "timeout": hc.get("timeout", 1000) // 1000,
                "healthy_threshold": hc.get("rise", 3),
                "unhealthy_threshold": hc.get("fall", 3),
            }
            if check in _HTTP_CHECKS:
                pool_data["health_check"]["path"] = "/"
        pools.append(pool_data)

    return {
        "layer": layer,
        "lb_name": lb.get("name"),
        "dns_names": lb.get("dns_names") or [],
        "lb_type": _aws_lb_type(lb),
        "internal": lb.get("scheme") == "internal",
        "vpc_name": (lb.get("virtual_network") or {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_pools": pools,
        "listeners": listeners,
        "tags": {"Name": lb.get("name"), "ManagedBy": "Terraform", "Infrahub": "true"},
    }


def prepare_azure_data(lb: dict, vips: list[dict]) -> dict:
    """Prepare load balancer data for Azure Terraform module.

    A layer 4 VIP is an ``azurerm_lb_rule``; a layer 7 one is a listener and routing rule of an Application Gateway
    (``protocol`` http/https, the https certificate name, the probe name), both kept in ``lb_rules``; the certificates
    the listeners name are ``ssl_certificates``.
    """
    layer = _layer(lb, vips)
    pools = []
    lb_rules = []
    certificates: dict[str, dict] = {}

    for priority, vip in enumerate(vips, start=1):
        pool_name = _pool_name(vip)
        rule: dict = {
            "name": f"{pool_name}-rule",
            "protocol": vip.get("protocol"),
            "frontend_port": vip.get("port"),
            "backend_port": vip.get("port"),
            "backend_pool_name": pool_name,
        }
        pool_data: dict = {"name": pool_name, "addresses": _instance_ips(lb, vip)}

        if layer == "l7":
            rule["priority"] = priority
            if vip.get("protocol") == "https":
                certificate = _azure_certificate(vip["ssl_certificate"])
                certificates[certificate["name"]] = certificate
                rule["ssl_certificate"] = certificate["name"]
            probe = _l7_health(vip, tcp_probe=False)
            if probe:
                rule["probe_name"] = f"{pool_name}-probe"
                pool_data["health_probe"] = {
                    "protocol": "Http",
                    "interval_in_seconds": probe["interval"],
                    "timeout_in_seconds": probe["timeout"],
                    "number_of_probes": probe["fall"],
                    "request_path": probe["path"],
                    "status_codes": probe["status"],
                }
        elif health_checks := vip.get("health_checks", []):
            hc = health_checks[0]
            probe_data: dict = {
                "protocol": hc.get("check", "http").upper(),
                "port": vip.get("port"),
                "interval_in_seconds": HC_INTERVAL_S,
                "number_of_probes": hc.get("fall", 3),
            }
            if hc.get("check", "") in _HTTP_CHECKS:
                probe_data["request_path"] = "/"
            pool_data["health_probe"] = probe_data
        lb_rules.append(rule)
        pools.append(pool_data)

    return {
        "layer": layer,
        "lb_name": lb.get("name"),
        "dns_names": lb.get("dns_names") or [],
        "sku": AZURE_GATEWAY_SKU if layer == "l7" else "Standard",
        "type": "Public" if lb.get("scheme") in PUBLIC_SCHEMES else "Private",
        "vnet_name": (lb.get("virtual_network") or {}).get("name"),
        "subnet_names": [s.get("name") for s in lb.get("network_segments", [])],
        "backend_pools": pools,
        "lb_rules": lb_rules,
        "ssl_certificates": [certificates[name] for name in sorted(certificates)],
        "tags": {"Name": lb.get("name"), "ManagedBy": "Terraform", "Infrahub": "true"},
    }


def prepare_gcp_data(lb: dict, vips: list[dict]) -> dict:
    """Prepare load balancer data for GCP Terraform module.

    A layer 7 forwarding rule is global and fronts a URL map and a target proxy (``url_map``, ``proxy``,
    ``proxy_type``, the https ``ssl_certificate``); a layer 4 one targets the backend service directly.
    """
    layer = _layer(lb, vips)
    backend_services = []
    health_checks_out = []
    forwarding_rules = []

    for vip in vips:
        pool_name = _pool_name(vip)
        protocol = (vip.get("protocol") or "").upper()
        rule: dict = {
            "name": f"{vip.get('hostname')}-{protocol}-{vip.get('port')}",
            "protocol": protocol,
            "port_range": str(vip.get("port")),
            "backend_service": pool_name,
        }
        if layer == "l7":
            rule.update(
                url_map=f"{pool_name}-url-map",
                proxy=f"{pool_name}-proxy",
                proxy_type=protocol.lower(),
                ssl_certificate=vip.get("ssl_certificate") if protocol == "HTTPS" else None,
            )
        forwarding_rules.append(rule)

        backend_services.append(
            {
                "name": pool_name,
                "protocol": "HTTP" if layer == "l7" else ("UDP" if protocol == "UDP" else "TCP"),
                "session_affinity": "NONE",
                "health_check": f"{pool_name}-health-check",
                "port": vip.get("port"),
                "endpoints": _instance_ips(lb, vip),
            }
        )

        hc_data: dict | None = None
        if layer == "l7":
            if probe := _l7_health(vip, tcp_probe=True):
                hc_data = {
                    "protocol": probe["protocol"],
                    "timeout_sec": probe["timeout"],
                    "healthy_threshold": probe["rise"],
                    "unhealthy_threshold": probe["fall"],
                }
                if probe["path"]:
                    hc_data["request_path"] = probe["path"]
        elif hcs := vip.get("health_checks", []):
            hc = hcs[0]
            hc_data = {
                "protocol": hc.get("check", "http").upper(),
                "timeout_sec": hc.get("timeout", 1000) // 1000,
                "healthy_threshold": hc.get("rise", 3),
                "unhealthy_threshold": hc.get("fall", 3),
            }
            if hc.get("check", "") in _HTTP_CHECKS:
                hc_data["request_path"] = "/"
        if hc_data:
            health_checks_out.append(
                {
                    "name": f"{pool_name}-health-check",
                    "port": vip.get("port"),
                    "check_interval_sec": HC_INTERVAL_S,
                    **hc_data,
                }
            )

    return {
        "layer": layer,
        "lb_name": lb.get("name"),
        "dns_names": lb.get("dns_names") or [],
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
    listener/rule/forwarding rule per LoadbalancerVIP, its pool members (the cloud instances, VIP.pool_members) as
    targets on the VIP's own port and protocol: AWS registers the instances by cloud_id (target_type instance),
    Azure and GCP their private_ip. An instance missing what its provider needs raises ValueError.

    The layer follows the load balancer's lb_type: l4_cloud (tcp/udp VIPs) renders the layer 4 resources above,
    l7_cloud (http/https VIPs, https with an ssl_certificate) renders an ALB, an Application Gateway or a global
    external HTTP(S) load balancer from the same template, selected by the ``layer`` the preparer returns. Each
    ``dns_names`` entry is an ``# endpoint:`` comment.

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
