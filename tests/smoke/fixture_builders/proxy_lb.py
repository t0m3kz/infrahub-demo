"""Proxy, load-balancer and cloud load-balancer fixtures."""

from __future__ import annotations

from typing import Any

from tests.smoke.fixture_builders.common import _edges, _node, _v, write_data_fixtures
from transforms.config.loadbalancer import LoadBalancer
from transforms.config.loadbalancer_cloud import LoadBalancerCloud
from transforms.config.proxy import Proxy

_PROXY_PLATFORM_VENDOR: dict[str, str] = {
    "haproxy_technologies": "haproxy",
    "squid_cache": "squid",
    "bluecoat_sgos": "bluecoat",
    "cisco_wsa": "cisco_wsa",
}


def build_proxy_data(
    *,
    device_name: str,
    platform: str,
    proxy_type: str = "explicit",
) -> dict:
    """Build a raw GQL-shaped dict for a proxy device config query.

    The response mirrors what clean_data() will receive (raw ``{"value": ...}`` wrappers).
    """
    vendor = _PROXY_PLATFORM_VENDOR.get(platform, "haproxy")

    proxy_cap = {
        "__typename": "ManagedProxyHA",
        "name": _v(f"{device_name}-HA"),
        "group_id": _v(1),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "proxy_type": _v(proxy_type),
        "proxy_vendor": _v(vendor),
        "capabilities": _edges(
            [
                {"name": _v(device_name)},
                {"name": _v(device_name.replace("01", "02"))},
            ]
        ),
    }

    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "description": _v("OOB management"),
            "status": _v("active"),
            "role": _v("management"),
            "ip_address": _node({"address": _v("192.168.1.1/24")}),
        },
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("eth0"),
            "description": _v("Ingress uplink"),
            "status": _v("active"),
            "role": _v("uplink"),
            "ip_address": _node(None),
        },
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("eth1"),
            "description": _v("Egress uplink"),
            "status": _v("active"),
            "role": _v("uplink"),
            "ip_address": _node(None),
        },
    ]

    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v("proxy"),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
            }
        ),
        "primary_address": _node(None),
        "tags": _edges([]),
        "capabilities": _edges([proxy_cap]),
        "interfaces": _edges(interfaces),
    }

    return {"DcimPhysicalDevice": _edges([device_node])}


def _inline_segment_cap(*, ha_name: str, gateway: str, members: dict[str, str]) -> dict:
    """A terminate_inline segment capability gatewayed by the HA pair ``ha_name``;
    ``members`` maps each member device to its own address on the segment."""
    return {
        "__typename": "ManagedVxlanSegment",
        "id": "seg-inline",
        "name": _v("c001-web-p"),
        "terminate_inline": _v(True),
        "inline_service": _node({"name": _v(ha_name)}),
        "gateway": _node({"address": _v(gateway)}),
        "capabilities": _edges(
            [
                {"device": _node({"name": _v(device)}), "ip_address": _node({"address": _v(address)})}
                for device, address in members.items()
            ]
        ),
    }


def _inline_member_interface(*, name: str, address: str, segment: dict) -> dict:
    """The primary member's <port>.<vlan> sub-interface carrying ``segment``."""
    return {
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "description": _v("Inline web segment"),
        "status": _v("active"),
        "role": _v("service"),
        "ip_address": _node({"address": _v(address)}),
        "parent_interface": _node({"name": _v(name.split(".")[0])}),
        "capabilities": _edges([segment]),
    }


def build_proxy_inline_data(*, device_name: str, platform: str) -> dict:
    """Proxy HA pair (device = primary member) terminating a segment inline on eth1.210."""
    data = build_proxy_data(device_name=device_name, platform=platform, proxy_type="transparent")
    segment = _inline_segment_cap(
        ha_name=f"{device_name}-HA",
        gateway="10.2.0.1/24",
        members={device_name: "10.2.0.2/24", device_name.replace("01", "02"): "10.2.0.3/24"},
    )
    data["DcimPhysicalDevice"]["edges"][0]["node"]["interfaces"]["edges"].append(
        {"node": _inline_member_interface(name="eth1.210", address="10.2.0.2/24", segment=segment)}
    )
    return data


LB_PLATFORMS = ["f5_tmos", "haproxy_technologies", "netscaler"]


def _lb_members(device_name: str, peer_name: str) -> list[dict]:
    """The HA members as LoadbalancerDeviceFields selects them: name and management address (192.168.2.1/.2)."""
    return [
        {"name": _v(member), "primary_address": _node({"address": _v(f"192.168.2.{host}/32")})}
        for host, member in enumerate((device_name, peer_name), start=1)
    ]


def build_lb_inline_data(*, device_name: str, platform: str) -> dict:
    """Load-balancer HA pair (device = primary member) terminating a segment
    inline on eth2.220, next to a management port."""
    peer_name = device_name.replace("01", "02")
    ha_cap = {
        "__typename": "ManagedLoadbalancerHA",
        "name": _v(f"{device_name}-{peer_name}-ha"),
        "group_id": _v(2),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "capabilities": _edges(_lb_members(device_name, peer_name)),
    }
    segment = _inline_segment_cap(
        ha_name=f"{device_name}-{peer_name}-ha",
        gateway="10.3.0.1/24",
        members={device_name: "10.3.0.2/24", peer_name: "10.3.0.3/24"},
    )
    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "description": _v("OOB management"),
            "status": _v("active"),
            "role": _v("management"),
            "ip_address": _node({"address": _v("192.168.2.1/24")}),
            "capabilities": _edges([]),
        },
        _inline_member_interface(name="eth2.220", address="10.3.0.2/24", segment=segment),
    ]
    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "id": f"dev-{device_name}",
        "name": _v(device_name),
        "role": _v("load-balancer"),
        "platform": _node(
            {
                "id": f"plat-{platform}",
                "name": _v(platform),
            }
        ),
        "primary_address": _node(None),
        "tags": _edges([]),
        "capabilities": _edges([ha_cap]),
        "interfaces": _edges(interfaces),
    }
    return {"DcimPhysicalDevice": _edges([device_node])}


_LB_CONTEXT_NAMESPACES: dict[str, dict[str, Any]] = {
    "PROD": {"namespace_type": "prod", "l3_vni": 60000},
    "NON-PROD": {"namespace_type": "non_prod", "l3_vni": 60001},
}


def _lb_leg_iface(*, device: str, name: str, address: str, namespace: str) -> dict:
    """A member's leg sub-interface as LoadbalancerContextConfigFields selects it."""
    ns = _LB_CONTEXT_NAMESPACES[namespace]
    return {
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "device": _node({"name": _v(device), "role": _v("load-balancer")}),
        "parent_interface": _node({"name": _v(name.split(".")[0])}),
        "ip_address": _node(
            {
                "address": _v(address),
                "ip_namespace": _node(
                    {"name": _v(namespace), "namespace_type": _v(ns["namespace_type"]), "l3_vni": _v(ns["l3_vni"])}
                ),
            }
        ),
    }


def _lb_vip(
    *,
    hostname: str,
    address: str,
    port: int,
    members: list[str],
    checks: list[dict],
    algorithm: str | None,
    persistence: str,
) -> dict:
    """A LoadbalancerVIP of a context (tcp, as the VIP producer writes them)."""
    return {
        "hostname": _v(hostname),
        "description": _v(None),
        "status": _v("active"),
        "protocol": _v("tcp"),
        "port": _v(port),
        "load_balancing_algorithm": _v(algorithm),
        "session_persistence": _v(persistence),
        "ssl_certificate": _v(None),
        "vip_ip": _node({"address": _v(address)}),
        "pool_members": _edges([{"address": _v(member)} for member in members]),
        "health_checks": _edges([{key: _v(value) for key, value in check.items()} for check in checks]),
    }


def build_lb_context_data(*, device_name: str, platform: str) -> dict:
    """Shared load-balancer pair (device = primary member) hosting a shared PROD and a shared
    NON-PROD context, one VIP each, next to a management port and the uplink trunk."""
    peer_name = device_name.replace("01", "02")
    cluster = f"{device_name}-{peer_name}-ha"
    # (namespace, context VLAN, leg /29 base, VIP /26, VIP)
    specs: list[tuple[str, int, str, str, dict[str, Any]]] = [
        (
            "PROD",
            3100,
            "100.66.64",
            "100.66.96.0/26",
            {
                "hostname": "web.c001.example.com",
                "address": "100.66.96.10/32",
                "port": 443,
                "members": ["10.10.1.11/24", "10.10.1.10/24"],
                "checks": [{"check": "http", "rise": 2, "fall": 3, "timeout": 2000}],
                "algorithm": "least_connections",
                "persistence": "source_ip",
            },
        ),
        (
            "NON-PROD",
            3101,
            "100.66.80",
            "100.66.112.0/26",
            {
                "hostname": "api.c002.example.com",
                "address": "100.66.112.10/32",
                "port": 8080,
                "members": ["10.20.1.10/24", "10.20.1.11/24"],
                "checks": [{"check": "tcp", "rise": None, "fall": None, "timeout": None}],
                "algorithm": None,
                "persistence": "none",
            },
        ),
    ]
    contexts = []
    leg_ifaces = []
    for namespace, vlan_id, leg_base, vip_prefix, vip in specs:
        leg_name = f"eth1.{vlan_id + (200 if namespace == 'NON-PROD' else 0)}"
        contexts.append(
            {
                "id": f"ctx-{namespace}",
                "name": _v(f"{cluster}-{namespace}"),
                "vlan_id": _v(vlan_id),
                "tenant": _node(None),
                "vip_prefix": _node({"prefix": _v(vip_prefix)}),
                "capabilities": _edges(
                    [
                        _lb_leg_iface(
                            device=device_name, name=leg_name, address=f"{leg_base}.5/29", namespace=namespace
                        ),
                        _lb_leg_iface(device=peer_name, name=leg_name, address=f"{leg_base}.6/29", namespace=namespace),
                        {
                            "__typename": "DcimPhysicalInterface",
                            "name": _v("Ethernet1/10"),
                            "device": _node({"name": _v("dc3-border-leaf-01")}),
                        },
                    ]
                ),
                "vips": _edges([_lb_vip(**vip)]),
            }
        )
        leg_ifaces.append(
            {
                "__typename": "DcimVirtualInterface",
                "name": _v(leg_name),
                "description": _v(None),
                "status": _v("active"),
                "role": _v("uplink"),
                "ip_address": _node({"address": _v(f"{leg_base}.5/29")}),
                "parent_interface": _node({"name": _v("eth1")}),
                "capabilities": _edges([{"__typename": "ManagedLoadbalancerContext"}]),
            }
        )
    ha_cap = {
        "__typename": "ManagedLoadbalancerHA",
        "name": _v(cluster),
        "group_id": _v(2),
        "mode": _v("active-passive"),
        "priority": _v(100),
        "preempt": _v(False),
        "lb_type": _v("l4_software" if platform.startswith("haproxy") else "l4_hardware"),
        "scheme": _v("internal"),
        "capabilities": _edges(_lb_members(device_name, peer_name)),
        "contexts": _edges(contexts),
    }
    interfaces = [
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("mgmt"),
            "description": _v("OOB management"),
            "status": _v("active"),
            "role": _v("management"),
            "ip_address": _node({"address": _v("192.168.2.1/24")}),
            "capabilities": _edges([]),
        },
        {
            "__typename": "DcimPhysicalInterface",
            "name": _v("eth1"),
            "description": _v("Uplink trunk to the border leaf"),
            "status": _v("active"),
            "role": _v("uplink"),
            "ip_address": _node(None),
            "capabilities": _edges([]),
        },
        *leg_ifaces,
    ]
    device_node: dict = {
        "__typename": "DcimPhysicalDevice",
        "name": _v(device_name),
        "role": _v("load-balancer"),
        "platform": _node(
            {
                "name": _v(platform),
            }
        ),
        "capabilities": _edges([ha_cap]),
        "interfaces": _edges(interfaces),
    }
    return {"DcimPhysicalDevice": _edges([device_node]), "DcimVirtualDevice": _edges([])}


# provider -> (OrganizationProvider name, scheme, [(protocol, port, health checks)])
_LB_CLOUD_SPECS: dict[str, tuple[str, str, list[tuple[str, int, list[dict]]]]] = {
    "aws": (
        "AWS",
        "internet_facing",
        [("tcp", 443, [{"check": "tcp", "rise": 3, "fall": 3, "timeout": 2000}]), ("udp", 443, [])],
    ),
    "azure": ("Azure", "internet_facing", [("tcp", 443, [{"check": "http", "rise": 2, "fall": 4, "timeout": 1000}])]),
    "gcp": ("Google Cloud", "internal", [("tcp", 8443, [{"check": "tcp", "rise": 3, "fall": 3, "timeout": 1000}])]),
}


def build_lb_cloud_data(*, provider: str) -> dict:
    """The CloudLoadBalancer of a load_balanced cloud component as generators/lb_vip.py writes it
    (queries/config/loadbalancer_cloud.gql): l4_cloud, one VIP per port without vip_ip, the
    component's instance addresses as pool members, the component's health checks."""
    provider_name, scheme, ports = _LB_CLOUD_SPECS[provider]
    fqdn = "frontend.custody-api.c003.demo.local"
    lb_name = "frontend-custody-api-c0-a9ea4206"
    load_balancer = {
        "id": f"lb-{provider}",
        "name": _v(lb_name),
        "lb_type": _v("l4_cloud"),
        "scheme": _v(scheme),
        "virtual_network": _node(
            {
                "id": f"vnet-{provider}",
                "name": _v(f"c003-vaultex-{provider}-vpc-prod"),
                "account": _node({"name": _v(f"c003-{provider}-prod"), "provider": _node({"name": _v(provider_name)})}),
            }
        ),
        "network_segments": _edges(
            [
                {
                    "id": f"seg-{provider}-{zone}",
                    "name": _v(f"c003-vaultex-subnet-prod-{zone}"),
                    "availability_zone": _node({"name": _v(f"euc1-az{zone}")}),
                }
                for zone in (1, 2)
            ]
        ),
    }
    vips = [
        {
            "id": f"vip-{provider}-{protocol}-{port}",
            "hostname": _v(fqdn),
            "protocol": _v(protocol),
            "port": _v(port),
            "ssl_certificate": _v(None),
            "load_balancing_algorithm": _v(None),
            "session_persistence": _v(None),
            # unsorted on purpose: the renderers order the members by address
            "pool_members": _edges([{"address": _v("10.20.2.11/24")}, {"address": _v("10.20.1.10/24")}]),
            "health_checks": _edges([{key: _v(value) for key, value in check.items()} for check in checks]),
        }
        for protocol, port, checks in ports
    ]
    return {"CloudLoadBalancer": _edges([load_balancer]), "LoadbalancerVIP": _edges(vips)}


def write_fixtures() -> tuple[int, int]:
    """Write the proxy, load-balancer and cloud load-balancer fixtures."""
    generated = errors = 0

    # Proxy scenarios: platform × proxy_type
    proxy_platforms = list(_PROXY_PLATFORM_VENDOR)
    print("\nGenerating proxy fixtures:")
    g, e = write_data_fixtures(
        (
            Proxy,
            f"proxy_{platform}_{proxy_type}",
            build_proxy_data(device_name="DC3-PRX-01", platform=platform, proxy_type=proxy_type),
        )
        for platform in proxy_platforms
        for proxy_type in ["explicit", "transparent", "reverse"]
    )
    generated += g
    errors += e

    # Proxy / load-balancer HA pairs terminating a segment inline: virtual IP per vendor
    print("\nGenerating proxy and load-balancer inline-segment fixtures:")
    g, e = write_data_fixtures(
        [
            *(
                (
                    Proxy,
                    f"proxy_{platform}_inline_segment",
                    build_proxy_inline_data(device_name="DC3-PRX-01", platform=platform),
                )
                for platform in proxy_platforms
            ),
            *(
                (
                    LoadBalancer,
                    f"loadbalancer_{platform}_inline_segment",
                    build_lb_inline_data(device_name="DC3-LB-01", platform=platform),
                )
                for platform in LB_PLATFORMS
            ),
        ]
    )
    generated += g
    errors += e

    # Load-balancer pair hosting a shared PROD and a shared NON-PROD context (one-arm leg, VIPs)
    print("\nGenerating load-balancer context fixtures:")
    g, e = write_data_fixtures(
        (
            LoadBalancer,
            f"loadbalancer_{platform}_context",
            build_lb_context_data(device_name="DC3-LB-01", platform=platform),
        )
        for platform in LB_PLATFORMS
    )
    generated += g
    errors += e

    # Cloud load balancer of a load_balanced cloud component, per provider (Terraform HCL)
    print("\nGenerating cloud load-balancer fixtures:")
    g, e = write_data_fixtures(
        (LoadBalancerCloud, f"loadbalancer_cloud_{provider}", build_lb_cloud_data(provider=provider))
        for provider in _LB_CLOUD_SPECS
    )
    return generated + g, errors + e
