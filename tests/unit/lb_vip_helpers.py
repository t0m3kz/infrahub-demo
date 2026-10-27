"""Shared payload builders and generator stubs for the load balancer VIP tests (test_lb_vip_*.py)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.lb_vip import cloud_lb_name
from generators.protocols import LoadbalancerVIP
from generators.topology.application_security import AppApplicationGenerator

# ---------------------------------------------------------------------------
# Fixtures (cleaned application.gql shape)
# ---------------------------------------------------------------------------


def _context(
    ctx_id: str = "ctx-1",
    name: str = "dc10-lb-PROD",
    *,
    leg: str | None = "leg-1",
    vip_prefix: str | None = "vip-pfx-1",
) -> dict[str, Any]:
    """A serving context: its member sub-interfaces carry leg addresses, its border port none."""
    capabilities: list[dict[str, Any]] = [{"typename": "DcimPhysicalInterface"}]
    if leg:
        capabilities += [
            {"typename": "DcimVirtualInterface", "ip_address": {"ip_prefix": {"id": leg}}},
            {"typename": "DcimVirtualInterface", "ip_address": {"ip_prefix": {"id": leg}}},
        ]
    return {
        "id": ctx_id,
        "name": name,
        "vip_prefix": {"id": vip_prefix, "ip_namespace": {"id": "ns-prod"}} if vip_prefix else None,
        "capabilities": capabilities,
    }


def _instance(instance_id: str, context: dict[str, Any] | None, *, footprint: bool = True) -> dict[str, Any]:
    """A virtual machine running for a customer footprint in the data center of ``context``.

    The context is the one its segment's footprint in that data center serves (``_with_footprints`` builds the
    footprint from it): None is a footprint with no context yet, and ``footprint=False`` a data center where the
    segment is not activated at all. The default context ``ctx-1`` is the data center ``dc-c001`` of the footprint
    ``dep-c001``.
    """
    key = context["id"] if context and context["id"] != "ctx-1" else "c001"
    return {
        "id": instance_id,
        "name": instance_id,
        "typename": "DcimVirtualDevice",
        "deployment": {
            "id": f"dep-{key}",
            "typename": "TopologyCustomerDC",
            "parent": {"id": f"dc-{key}" if footprint else "dc-elsewhere"},
        },
        "_context": context,
    }


def _with_footprints(component: dict[str, Any]) -> dict[str, Any]:
    """Activate the component's segment in the data center of each of its instances: one footprint per data center.

    The context an instance was built with becomes the context its footprint serves, so a test says "this instance
    is served by that context" and the payload has the cleaned application.gql shape: the context sits on the
    segment's footprint, not on the instance. A data center set apart with ``footprint=False`` gets none.
    """
    footprints: dict[str, dict[str, Any]] = {}
    for instance in component.get("instances") or []:
        dc = instance["deployment"]["parent"]["id"]
        if dc != "dc-elsewhere" and dc not in footprints:
            footprints[dc] = {
                "id": instance["deployment"]["id"],
                "typename": "TopologyCustomerDC",
                "parent": {"id": dc},
                "serving_loadbalancer_context": instance.get("_context"),
            }
    segment = component.setdefault("network_segment", {})
    segment["customer_deployments"] = list(footprints.values())
    return component


def _segment(seg_id: str, *, prefix: bool = True) -> dict[str, Any]:
    segment: dict[str, Any] = {
        "id": seg_id,
        "name": f"{seg_id}-name",
        "typename": "ManagedVxlanSegment",
        "isolation_mode": "normal",
        "environment": "p",
        "security_zone": {"name": "PROD-ZONE"},
    }
    if prefix:
        segment["gateway"] = {
            "ip_prefix": {"id": f"pfx-{seg_id}", "prefix": "10.60.0.0/24", "ip_namespace": {"id": "ns-prod"}}
        }
    return segment


def _web(
    *,
    ports: list[str] | None = None,
    instances: list[dict[str, Any]] | None = None,
    load_balanced: bool = True,
    segment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The load-balanced backend: two instances on one context."""
    context = _context()
    return _with_footprints(
        {
            "id": "comp-web",
            "name": "web",
            "fqdn": "web.c001.example",
            "ports": ["tcp/443"] if ports is None else ports,
            "component_type": "backend",
            "load_balanced": load_balanced,
            "instances": [_instance("vm-2", context), _instance("vm-1", context)] if instances is None else instances,
            "health_checks": [{"id": "hc-1"}],
            "network_segment": segment or _segment("seg-web"),
        }
    )


def _client_comp(*deps: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "comp-portal",
        "name": "portal",
        "fqdn": "portal.c001.example",
        "ports": ["tcp/8080"],
        "component_type": "frontend",
        "network_segment": _segment("seg-portal"),
        "depends_on": list(deps),
    }


def _dep(target: dict[str, Any], ports: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": "dep-1",
        "name": "portal-to-web",
        "ports": ports or [],
        "target_fqdn": None,
        "access_status": "auto",
        "description": None,
        "target": target,
    }


# ===========================================================================
# LoadbalancerVipMixin
# ===========================================================================


def _vip_gen() -> Any:
    """An AppApplicationGenerator with every Infrahub boundary of the VIP producer stubbed."""
    gen: Any = AppApplicationGenerator.__new__(AppApplicationGenerator)
    gen.client = AsyncMock()
    gen.client.filters = AsyncMock(return_value=[])
    gen.logger = MagicMock()
    gen.locks = []

    @asynccontextmanager
    async def _lock(key: str) -> AsyncGenerator[None]:
        gen.locks.append(key)
        yield

    gen.resource_lock = _lock
    gen.ensure_prefix_address_pool = AsyncMock(side_effect=lambda **kw: MagicMock(name=kw["pool_name"]))
    gen.allocate_prefix_address = AsyncMock(side_effect=lambda **kw: f"ip:{kw['identifier']}")
    gen.upsert_ip_address = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"ip:{kw['address']}"))
    gen._get_or_create_by_name = AsyncMock(return_value=MagicMock(id="pol-lb"))
    gen._upsert_policy_rule = AsyncMock(return_value=100)
    # Nothing in flight, nothing more on a re-read, and no real sleep between re-reads.
    gen.wait_for_parent_generators = AsyncMock(return_value=False)
    gen.client.execute_graphql = AsyncMock(return_value={})
    gen.lb_reread_sleep = AsyncMock()
    return gen


def _vip_payloads(gen: Any) -> list[dict[str, Any]]:
    return [call.kwargs["data"] for call in gen.client.create.await_args_list if call.kwargs["kind"] is LoadbalancerVIP]


def _reconcile(
    gen: Any,
    components: list[dict[str, Any]],
    edges: list[Any] | None = None,
    security_profile: str = "internal_standard",
) -> Any:
    return asyncio.run(gen._reconcile_load_balancing(components, edges or [], {}, security_profile=security_profile))


# ===========================================================================
# Cloud: a CloudLoadBalancer per load_balanced component on cloud instances
# ===========================================================================

_CLOUD_FQDN = "frontend.custody-api.c003.demo.local"


_CLOUD_LB = cloud_lb_name({"fqdn": _CLOUD_FQDN})


def _vpc(vpc_id: str = "vpc-1") -> dict[str, Any]:
    return {
        "id": vpc_id,
        "name": f"{vpc_id}-name",
        "cidr_blocks": [{"prefix": "10.30.0.0/16", "ip_namespace": {"id": "ns-prod"}}],
    }


def _subnet(seg_id: str = "sub-a", vpc_id: str = "vpc-1") -> dict[str, Any]:
    return {"id": seg_id, "name": f"{seg_id}-name", "virtual_network": _vpc(vpc_id)}


def _cloud_instance(
    instance_id: str, private_ip: str | None = "10.30.1.11/24", subnet: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "id": instance_id,
        "name": instance_id,
        "typename": "CloudInstance",
        "private_ip": private_ip,
        "network_segment": _subnet() if subnet is None else subnet,
    }


def _cloud_frontend(
    *, ports: list[str] | None = None, instances: list[dict[str, Any]] | None = None, load_balanced: bool = True
) -> dict[str, Any]:
    """c003's api-frontend: two AWS instances in one subnet of one VPC."""
    return {
        "id": "comp-frontend",
        "name": "api-frontend",
        "fqdn": _CLOUD_FQDN,
        "ports": ["tcp/443"] if ports is None else ports,
        "component_type": "frontend",
        "load_balanced": load_balanced,
        "instances": (
            [_cloud_instance("i-2", "10.30.1.12/24"), _cloud_instance("i-1", "10.30.1.11/24")]
            if instances is None
            else instances
        ),
        "health_checks": [{"id": "hc-1"}],
        "network_segment": {
            **_subnet(),
            "typename": "CloudNetworkSegment",
            "cidr_block": "10.30.1.0/24",
            "environment": "p",
        },
    }


def _lb_gen() -> Any:
    """_vip_gen() whose created nodes carry their own id, so a VIP points at its load balancer."""
    gen = _vip_gen()
    gen.created = []

    def create(kind: Any, data: dict[str, Any]) -> Any:
        node = MagicMock(id=f"id-{data.get('name') or data.get('hostname')}", save=AsyncMock())
        gen.created.append(node)
        return node

    gen.client.create = AsyncMock(side_effect=create)
    return gen
