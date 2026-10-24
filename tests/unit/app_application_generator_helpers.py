"""Shared harness for the AppApplicationGenerator unit tests (generator stub and GraphQL-shaped payload builders)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.topology.application_security import AppApplicationGenerator


def _make_gen() -> Any:
    gen = AppApplicationGenerator.__new__(AppApplicationGenerator)
    gen.client = AsyncMock()
    gen._init_client = AsyncMock()
    gen.logger = MagicMock()
    # Zone assignment has its own tests (test_segment_firewall_mixin.py,
    # test_security_zone_helper.py) - stubbed at the boundary here.
    gen._ensure_segment_zones = AsyncMock()
    return gen


def _segment(seg_id: str, typename: str = "ManagedVxlanSegment") -> dict[str, Any]:
    """A cleaned network_segment as application.gql returns it."""
    seg: dict[str, Any] = {
        "id": seg_id,
        "name": f"{seg_id}-name",
        "typename": typename,
        "isolation_mode": "normal",
        "environment": "p",
        "security_zone": {"name": "PROD-ZONE", "trust_level": 70},
    }
    if typename != "CloudNetworkSegment":
        seg["gateway"] = {"ip_prefix": {"prefix": "10.60.0.0/24"}}
    if typename == "CloudNetworkSegment":
        seg["virtual_network"] = {"id": "vnet-1", "name": "vnet-prod", "account": {"id": "acct-1"}}
        seg["cidr_block"] = "10.50.0.0/24"
    return seg


def _backend(
    ports: list[str] | None = None,
    typename: str = "ManagedVxlanSegment",
) -> dict[str, Any]:
    """Target component: a backend listening on tcp/8443 and a udp gossip range."""
    return {
        "id": "comp-backend",
        "name": "backend",
        "fqdn": "backend.checkout.c001.example",
        "ports": ["tcp/8443", "udp/30000-30010"] if ports is None else ports,
        "component_type": "backend",
        "network_segment": _segment("seg-dst", typename),
    }


def _frontend(*deps: dict[str, Any], typename: str = "ManagedVxlanSegment") -> dict[str, Any]:
    return {
        "id": "comp-frontend",
        "name": "frontend",
        "fqdn": "frontend.checkout.c001.example",
        "ports": ["tcp/443"],
        "component_type": "frontend",
        "network_segment": _segment("seg-src", typename),
        "depends_on": list(deps),
    }


def _dep(
    name: str = "frontend-to-backend",
    *,
    ports: list[str] | None = None,
    target: dict[str, Any] | None = None,
    target_fqdn: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": name,
        "ports": ports or [],
        "target_fqdn": target_fqdn,
        "access_status": "auto",
        "description": None,
        "target": target,
    }


def _app(*components: dict[str, Any], profile: str = "internal_standard") -> dict[str, Any]:
    return {"name": "checkout", "security_profile": profile, "children": list(components)}
