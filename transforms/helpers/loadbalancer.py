"""Load-balancer contexts of one device: the partition, route domain and leg of each, and its VIPs."""

from __future__ import annotations

import ipaddress
import math
from collections.abc import Callable, Mapping
from typing import Any

from transforms.helpers.addressing import host_ip, sub_interface_parent
from transforms.helpers.firewall import transit_legs
from utils.exchange_transit import LOADBALANCER_CONTEXT_KIND, TENANT_NAMESPACE_TYPES, TRANSIT_SLOT
from utils.routing_domain import interface_security_zone
from utils.service_contexts import leg_interfaces

HC_INTERVAL_S = 5
"""Probe interval of every health check (the model has none); stretched when a probe's own timeout needs more."""

HC_DEFAULTS = {"rise": 3, "fall": 3, "timeout": 1000}
"""LoadbalancerHealthCheck's schema defaults, for a check that leaves one unset."""

HC_HTTP_PATH = "/health"
"""The request path of an http/ssl check (the model has none)."""

_TLS_PROTOCOLS = frozenset({"https", "tls"})


def member_address(member: dict[str, Any]) -> str | None:
    """The address of a pool member, a server."""
    return host_ip((member.get("primary_address") or {}).get("address") or member.get("private_ip"))


def pool_member_ips(vip: dict[str, Any]) -> list[str]:
    """The distinct addresses of the pool members of a cleaned LoadbalancerVIP, in address order."""
    return sorted(
        {ip for member in vip.get("pool_members") or [] if (ip := member_address(member))},
        key=ipaddress.ip_address,
    )


def _vip_name(vip: dict[str, Any]) -> str:
    """Object-name stem of a VIP: hostname with . and - as _, then protocol and port."""
    hostname = str(vip.get("hostname")).replace(".", "_").replace("-", "_")
    return f"{hostname}_{vip.get('protocol')}_{vip.get('port')}"


def _health_checks(checks: list[dict[str, Any]] | None, vip_name: str) -> list[dict[str, Any]]:
    """The VIP's checks with their timers in every vendor's terms, in a stable order."""
    normalized: list[dict[str, Any]] = []
    for check in checks or []:
        rise: int = check.get("rise") or HC_DEFAULTS["rise"]
        fall: int = check.get("fall") or HC_DEFAULTS["fall"]
        timeout_ms: int = check.get("timeout") or HC_DEFAULTS["timeout"]
        normalized.append({"check": check.get("check"), "rise": rise, "fall": fall, "timeout_ms": timeout_ms})
    normalized.sort(key=lambda c: (str(c["check"]), c["rise"], c["fall"], c["timeout_ms"]))
    for index, check in enumerate(normalized, start=1):
        interval = max(HC_INTERVAL_S, math.ceil(check["timeout_ms"] / 1000) + 1)
        check.update(
            name=f"{vip_name}_mon{index}",
            interval_s=interval,
            down_after_s=interval * check["fall"] + 1,
            up_after_s=interval * check["rise"],
            path=HC_HTTP_PATH if check["check"] in ("http", "ssl") else None,
            tls=check["check"] == "ssl",
        )
    return normalized


def _refuse(platform: str, vip: dict[str, Any], what: str) -> ValueError:
    return ValueError(f"{platform} cannot render {what} of VIP {vip['hostname']} {vip['protocol']}/{vip['port']}")


def _token(table: Mapping[str, str | None], key: str, platform: str, vip: dict[str, Any], what: str) -> str | None:
    if key not in table:
        raise _refuse(platform, vip, f"{what} '{key}'")
    return table[key]


# Vendor tokens; `weighted` is each platform's weight-honouring round robin.

_F5_METHODS = {
    "round_robin": "round-robin",
    "least_connections": "least-connections-member",
    "weighted": "ratio-member",
}
_F5_PERSISTENCE: dict[str, str | None] = {"none": None, "source_ip": "/Common/source_addr", "cookie": "/Common/cookie"}
_F5_MONITORS = {"http": "http", "ssl": "https", "tcp": "tcp"}

_NS_METHODS = {
    "round_robin": "ROUNDROBIN",
    "least_connections": "LEASTCONNECTION",
    "ip_hash": "SOURCEIPHASH",
    "weighted": "ROUNDROBIN",
}
_NS_PERSISTENCE: dict[str, str | None] = {"none": "NONE", "source_ip": "SOURCEIP", "cookie": "COOKIEINSERT"}
_NS_MONITORS = {"http": "HTTP", "ssl": "HTTP", "tcp": "TCP"}

_HAPROXY_METHODS = {
    "round_robin": "roundrobin",
    "least_connections": "leastconn",
    "ip_hash": "source",
    "weighted": "roundrobin",
    "random": "random",
}
_HAPROXY_PERSISTENCE: dict[str, str | None] = {"none": None, "source_ip": "source_ip", "cookie": "cookie"}


def _f5_vip(vip: dict[str, Any], partition: str) -> dict[str, Any]:
    """F5 TMOS: pool method, persistence profile, virtual-server profiles and the monitor types."""
    platform = "f5_tmos"
    client_ssl = f"/{partition}/{vip['name']}_clientssl" if vip["terminate_tls"] else None
    profiles = [{"name": "/Common/udp" if vip["protocol"] == "udp" else "/Common/tcp", "context": None}]
    if vip["l7"]:
        profiles.append({"name": "/Common/http", "context": None})
    if client_ssl:
        profiles += [
            {"name": client_ssl, "context": "clientside"},
            {"name": "/Common/serverssl", "context": "serverside"},
        ]
    for check in vip["health_checks"]:
        check["type"] = _token(_F5_MONITORS, str(check["check"]), platform, vip, "health check")
    return {
        "method": _token(_F5_METHODS, vip["algorithm"], platform, vip, "load_balancing_algorithm"),
        "persist": _token(_F5_PERSISTENCE, vip["persistence"], platform, vip, "session_persistence"),
        "ip_protocol": "udp" if vip["protocol"] == "udp" else "tcp",
        "profiles": profiles,
        "client_ssl": client_ssl,
    }


def _netscaler_vip(vip: dict[str, Any], _partition: str) -> dict[str, Any]:
    """NetScaler: service type (vserver and service group alike), method, persistence, monitor types."""
    platform = "netscaler"
    if vip["protocol"] in _TLS_PROTOCOLS:
        service_type = ("SSL" if vip["protocol"] == "https" else "SSL_TCP") if vip["terminate_tls"] else "SSL_BRIDGE"
    else:
        service_type = str(vip["protocol"]).upper()
    for check in vip["health_checks"]:
        check["type"] = _token(_NS_MONITORS, str(check["check"]), platform, vip, "health check")
    return {
        "service_type": service_type,
        "method": _token(_NS_METHODS, vip["algorithm"], platform, vip, "load_balancing_algorithm"),
        "persist": _token(_NS_PERSISTENCE, vip["persistence"], platform, vip, "session_persistence"),
    }


def _haproxy_vip(vip: dict[str, Any], partition: str) -> dict[str, Any]:
    """HAProxy: proxy mode, balance, persistence, TLS and the one health check a server line holds."""
    platform = "haproxy_technologies"
    if vip["protocol"] == "udp":
        raise _refuse(platform, vip, "protocol udp")
    if len(vip["health_checks"]) > 1:
        raise _refuse(platform, vip, "more than one health check")
    return {
        "proxy_name": f"{partition}_{vip['name']}",
        "mode": "http" if vip["l7"] else "tcp",
        "method": _token(_HAPROXY_METHODS, vip["algorithm"], platform, vip, "load_balancing_algorithm"),
        "persist": _token(_HAPROXY_PERSISTENCE, vip["persistence"], platform, vip, "session_persistence"),
        "certificate": f"/etc/haproxy/certs/{vip['ssl']}.pem" if vip["terminate_tls"] else None,
        "check": vip["health_checks"][0] if vip["health_checks"] else None,
    }


_VENDORS: dict[str, Callable[[dict[str, Any], str], dict[str, Any]]] = {
    "f5_tmos": _f5_vip,
    "netscaler": _netscaler_vip,
    "haproxy_technologies": _haproxy_vip,
}
"""platform name -> VIP token builder; one per template in templates/configs/loadbalancers/."""


def _vip(raw: dict[str, Any], platform: str, partition: str) -> dict[str, Any]:
    """One LoadbalancerVIP, platform-neutral fields plus the platform's ``tokens``."""
    vip = {
        "name": _vip_name(raw),
        "hostname": raw.get("hostname"),
        "description": raw.get("description"),
        "enabled": (raw.get("status") or "active") == "active",
        "protocol": raw.get("protocol"),
        "port": raw.get("port"),
        "address": host_ip((raw.get("vip_ip") or {}).get("address")),
        "members": pool_member_ips(raw),
        "algorithm": raw.get("load_balancing_algorithm") or "round_robin",
        "persistence": raw.get("session_persistence") or "none",
        "ssl": raw.get("ssl_certificate"),
    }
    if not vip["address"]:
        raise ValueError(f"VIP {vip['hostname']} {vip['protocol']}/{vip['port']} has no vip_ip")
    vip["terminate_tls"] = vip["protocol"] in _TLS_PROTOCOLS and bool(vip["ssl"])
    vip["l7"] = vip["protocol"] == "http" or (vip["protocol"] == "https" and vip["terminate_tls"])
    if vip["persistence"] == "cookie" and not vip["l7"]:
        raise _refuse(platform, vip, "cookie persistence on an L4 VIP")
    vip["health_checks"] = _health_checks(raw.get("health_checks"), vip["name"])
    vip["tokens"] = _VENDORS[platform](vip, partition)
    return vip


def _partition_name(context: dict[str, Any], namespace_name: str) -> str:
    """``SHARED-<NAMESPACE>`` for a shared context, ``<ORG_ID>-<ENV>`` for a dedicated one."""
    tenant = context.get("tenant")
    if not tenant:
        return f"SHARED-{namespace_name}"
    # clean_data collapses a tenant that resolved only its id to that id string.
    tenant = tenant if isinstance(tenant, dict) else {}
    org_id = (tenant.get("owner") or {}).get("org_id")
    environment = tenant.get("environment")
    if not org_id or not environment:
        raise ValueError(f"Dedicated LB context {context.get('name')}: its tenant has no org_id or environment")
    return f"{org_id}-{environment}".upper()


def _leg(
    context: dict[str, Any], own_legs: list[dict[str, Any]], device_name: str, ha: dict[str, Any] | None
) -> dict[str, Any] | None:
    """This device's leg of ``context`` (``own_legs``."""
    for iface in own_legs:
        if iface.get("typename") != "DcimVirtualInterface":
            continue
        ip_obj = iface.get("ip_address") or {}
        address = ip_obj.get("address")
        # IPv4 by design: a load-balancer leg is a /29 of the IPv4 LB leg pools; pool members are not read here.
        if not address or ipaddress.ip_interface(address).version != 4:
            return None
        namespace = interface_security_zone(iface)
        if namespace.get("vrf_type") not in TENANT_NAMESPACE_TYPES or not namespace.get("l3_vni"):
            raise ValueError(
                f"LB context {context.get('name')}: leg {iface.get('name')} on {device_name} is not in a tenant VRF "
                f"with an l3_vni (namespace {namespace.get('name')})"
            )
        leg = transit_legs({**context, "legs": own_legs}).get(namespace.get("name"))
        if leg is None:
            return None
        own_ip = host_ip(address)
        members = (ha or {}).get("members") or []
        if device_name in members and own_ip != (expected := leg["members"][members.index(device_name)]):
            raise ValueError(
                f"LB context {context.get('name')}: {device_name} holds {own_ip} on {iface.get('name')}, "
                f"its member address by name order is {expected}"
            )
        return {
            "name": iface.get("name"),
            "parent_interface": (sub_interface_parent(iface) or {}).get("name"),
            "vlan": leg["vlan"],
            "vni": leg["vni"],
            "prefixlen": leg["prefixlen"],
            "anycast": leg["anycast"],
            "floating": leg["vip"],
            "virtual_ip": f"{leg['vip']}/{leg['prefixlen']}",
            "own_address": address,
            "own_ip": own_ip,
            "namespace": namespace,
        }
    return None


def get_loadbalancer_contexts(device: dict[str, Any], ha: dict[str, Any] | None, platform: str) -> list[dict[str, Any]]:
    """The load balancer contexts the device is a member of that have a leg on this device."""
    device_name = device.get("name") or ""
    if platform not in _VENDORS:
        raise ValueError(f"{platform}: no load-balancer template; refusing to render {device_name}")
    contexts = [
        cap for cap in device.get("shared_capabilities") or [] if cap.get("typename") == LOADBALANCER_CONTEXT_KIND
    ]
    legs = leg_interfaces(device)
    rendered: list[tuple[int, dict[str, Any]]] = []
    for context in contexts:
        leg = _leg(context, legs.get(context["id"], []), device_name, ha)
        if leg is None:
            continue
        namespace = leg.pop("namespace")
        partition = _partition_name(context, namespace["name"])
        raw_vips = sorted(
            context.get("vips") or [],
            key=lambda v: (str(v.get("hostname")), str(v.get("protocol")), v.get("port") or 0),
        )
        rendered.append(
            (
                TRANSIT_SLOT[namespace["vrf_type"]],
                {
                    "name": context.get("name"),
                    "partition": partition,
                    "dedicated": bool(context.get("tenant")),
                    "vrf": namespace["name"],
                    "route_domain": namespace["l3_vni"],
                    "vrf_device": f"vrf-{namespace['name']}",
                    "vip_prefix": (context.get("vip_prefix") or {}).get("prefix"),
                    "leg": leg,
                    "vips": [_vip(raw, platform, partition) for raw in raw_vips],
                },
            )
        )
    vrfs = [context["vrf"] for _, context in rendered]
    if len(vrfs) != len(set(vrfs)):
        raise ValueError(
            f"{device_name}: two load-balancer contexts in one VRF ({sorted(vrfs)}) would share a route domain"
        )
    return [context for _, context in sorted(rendered, key=lambda item: (item[0], item[1]["name"] or ""))]
