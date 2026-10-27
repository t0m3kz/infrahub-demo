"""Shared cloud load balancer fixture data for the ``test_loadbalancer_transforms_*`` modules."""

from __future__ import annotations

import copy

# ---------------------------------------------------------------------------
# Shared cleaned fixture data for cloud helper tests
# ---------------------------------------------------------------------------

_BASE_LB: dict = {
    "name": "test-alb",
    "lb_type": "l7_cloud",
    "scheme": "internet_facing",
    "virtual_network": {
        "name": "prod-vpc",
        "account": {
            "provider": {"name": "aws"},
        },
    },
    "network_segments": [{"name": "subnet-a"}, {"name": "subnet-b"}],
}

_BASE_MEMBERS: list = [
    {"typename": "CloudInstance", "name": "app-2", "cloud_id": "i-0bbb", "private_ip": "10.0.1.20/24"},
    {"typename": "CloudInstance", "name": "app-1", "cloud_id": "i-0aaa", "private_ip": "10.0.1.3/24"},
]

_BASE_VIPS: list = [
    {
        "hostname": "app.example.com",
        "port": 443,
        "protocol": "https",
        "ssl_certificate": "arn:aws:acm:us-east-1:123:certificate/abc",
        "pool_members": _BASE_MEMBERS,
        "health_checks": [
            {
                "check": "http",
                "rise": 2,
                "fall": 5,
                "timeout": 5000,
            }
        ],
    }
]

# pool_name derived from hostname-protocol-port
_POOL_NAME = "app.example.com-https-443"


def _lb() -> dict:
    """A layer 7 (l7_cloud) load balancer; ``_vips()`` are its https listener."""
    return copy.deepcopy(_BASE_LB)


def _vips() -> list:
    """One https/443 VIP with a certificate and an http health check (a layer 7 listener)."""
    return copy.deepcopy(_BASE_VIPS)


def _lb_l4() -> dict:
    """A layer 4 (l4_cloud) load balancer; ``_vips_l4()`` are its VIPs."""
    return {**_lb(), "lb_type": "l4_cloud"}


def _vips_l4() -> list:
    """Two VIPs of one layer 4 component as generators/lb_vip.py writes them: tcp (with a tcp health check) and udp
    on the same port."""
    return [
        {
            "hostname": "app.example.com",
            "protocol": protocol,
            "port": 443,
            "ssl_certificate": None,
            "pool_members": copy.deepcopy(_BASE_MEMBERS),
            "health_checks": [{"check": "tcp", "rise": 2, "fall": 5, "timeout": 5000}] if protocol == "tcp" else [],
        }
        for protocol in ("tcp", "udp")
    ]
