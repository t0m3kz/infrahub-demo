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

_BASE_VIPS: list = [
    {
        "hostname": "app.example.com",
        "port": 443,
        "protocol": "https",
        "ssl_certificate": "arn:aws:acm:us-east-1:123:certificate/abc",
        "pool_members": [{"address": "10.0.1.20/24"}, {"address": "10.0.1.3/24"}],
        "health_checks": [
            {
                "check": "https",
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
    return copy.deepcopy(_BASE_LB)


def _vips() -> list:
    return copy.deepcopy(_BASE_VIPS)
