"""Load-balancer fixtures."""

from __future__ import annotations

from typing import Any

from tests.smoke.fixture_builders.common import _edges, _node, _v, write_data_fixtures
from transforms.config.loadbalancer import LoadBalancer

LB_PLATFORMS = ["f5_tmos", "haproxy_technologies", "netscaler"]


def _lb_members(device_name: str, peer_name: str) -> list[dict]:
    """The HA members as LoadbalancerDeviceFields selects them: name and management address (192.168.2.1/.2)."""
    return [
        {"name": _v(member), "primary_address": _node({"address": _v(f"192.168.2.{host}/32")})}
        for host, member in enumerate((device_name, peer_name), start=1)
    ]


_LB_CONTEXT_NAMESPACES: dict[str, dict[str, Any]] = {
    "PROD": {"vrf_type": "prod", "l3_vni": 60000},
    "NONPROD": {"vrf_type": "non_prod", "l3_vni": 60001},
}


def _lb_leg_iface(*, device: str, name: str, address: str, namespace: str) -> dict:
    """A member's leg sub-interface as LoadbalancerContextConfigFields selects it."""
    ns = _LB_CONTEXT_NAMESPACES[namespace]
    return {
        "security_zone": _node({"name": _v(namespace), "vrf_type": _v(ns["vrf_type"]), "l3_vni": _v(ns["l3_vni"])}),
        "__typename": "DcimVirtualInterface",
        "name": _v(name),
        "device": _node({"name": _v(device), "role": _v("load-balancer")}),
        "parent_interface": _node({"name": _v(name.split(".")[0])}),
        "ip_address": _node(
            {
                "address": _v(address),
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            }
        ),
    }


def _server_member(address: str) -> dict:
    """A pool member of an on-prem VIP as LoadbalancerContextConfigFields selects it: a server and its address."""
    return {
        "__typename": "DcimVirtualDevice",
        "name": _v(f"srv-{address.split('/')[0].replace('.', '-')}"),
        "primary_address": _node({"address": _v(address)}),
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
        "pool_members": _edges([_server_member(member) for member in members]),
        "health_checks": _edges([{key: _v(value) for key, value in check.items()} for check in checks]),
    }


def build_lb_context_data(*, device_name: str, platform: str) -> dict:
    """Shared load-balancer pair (device = primary member) hosting a shared PROD and a shared
    NONPROD context, one VIP each, next to a management port and the uplink trunk."""
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
            "NONPROD",
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
        leg_name = f"eth1.{vlan_id + (200 if namespace == 'NONPROD' else 0)}"
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


def write_fixtures() -> tuple[int, int]:
    """Write the load-balancer fixtures."""
    generated = errors = 0

    # Load-balancer pair hosting a shared PROD and a shared NONPROD context (one-arm leg, VIPs)
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

    return generated, errors
