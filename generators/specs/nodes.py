"""Specs and reconcilers for tags, service contexts, VIPs, and locked find-or-create nodes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from infrahub_sdk.protocols import CoreNumberPool

from generators.specs.registry import PlanReconciler, ReconcileResult, register_spec


@dataclass(frozen=True)
class SecurityTagSpec:
    name: str
    group_id: int | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingTagSpec:
    kind: str
    node_id: str
    pool_name: str
    name: str = ""


@dataclass(frozen=True)
class FirewallContextSpec:
    name: str
    kind: str = "ManagedFirewallContext"
    firewall_ha_id: str | None = None
    tenant_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadBalancerVipSpec:
    hostname: str
    kind: str = "LoadbalancerVIP"
    address_id: str | None = None
    load_balancer_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


async def _upsert_named_nodes(
    reconciler: PlanReconciler,
    kind: str,
    items: list[tuple[str, dict[str, Any]]],
    result: ReconcileResult,
) -> None:
    existing = await reconciler.client.filters(kind=kind, name__values=[name for name, _ in items])
    existing_map = {n.name.value: n for n in existing}

    async def _save(name: str, payload: dict[str, Any]) -> None:
        async with reconciler.sem:
            data = {"name": name, **payload}
            if name in existing_map:
                data["id"] = existing_map[name].id
            node = await reconciler.client.create(kind=kind, data=data)
            await node.save(allow_upsert=True)
            result.node_ids[(kind, name)] = node.id

    await asyncio.gather(*(_save(name, payload) for name, payload in items))


@register_spec(SecurityTagSpec, order=10)
async def _reconcile_security_tags(
    reconciler: PlanReconciler, specs: list[SecurityTagSpec], result: ReconcileResult
) -> None:
    items = [
        (
            s.name,
            {
                **({"group_id": s.group_id} if s.group_id is not None else {}),
                **({"description": s.description} if s.description else {}),
                **s.extra,
            },
        )
        for s in specs
    ]
    await _upsert_named_nodes(reconciler, "SecurityTag", items, result)


@register_spec(RoutingTagSpec, order=12)
async def _reconcile_routing_tags(
    reconciler: PlanReconciler, specs: list[RoutingTagSpec], result: ReconcileResult
) -> None:
    for s in specs:
        pool = await reconciler.client.get(kind=CoreNumberPool, name__value=s.pool_name)
        consumer = await reconciler.client.create(
            kind=s.kind,
            data={"id": s.node_id, "routing_tag": {"from_pool": {"id": pool.id}}},
        )
        await consumer.save(allow_upsert=True, update_group_context=False)
        result.node_ids[(s.kind, s.name or s.node_id)] = s.node_id


@register_spec(FirewallContextSpec, order=20)
async def _reconcile_firewall_contexts(
    reconciler: PlanReconciler, specs: list[FirewallContextSpec], result: ReconcileResult
) -> None:
    for s in specs:
        payload = {
            **({"firewall_ha": {"id": s.firewall_ha_id}} if s.firewall_ha_id else {}),
            **({"tenant": {"id": s.tenant_id}} if s.tenant_id else {}),
            **s.extra,
        }
        lock_fn = getattr(reconciler.generator, "resource_lock", None)
        if lock_fn:
            async with lock_fn(f"fw-context-{s.name}"):
                await _upsert_named_nodes(reconciler, s.kind, [(s.name, payload)], result)
        else:
            await _upsert_named_nodes(reconciler, s.kind, [(s.name, payload)], result)


@register_spec(LoadBalancerVipSpec, order=22)
async def _reconcile_lb_vips(
    reconciler: PlanReconciler, specs: list[LoadBalancerVipSpec], result: ReconcileResult
) -> None:
    for s in specs:
        data: dict[str, Any] = {
            "hostname": s.hostname,
            **({"vip_ip": {"id": s.address_id}} if s.address_id else {}),
            **({"load_balancer": {"id": s.load_balancer_id}} if s.load_balancer_id else {}),
            **s.extra,
        }
        lock_fn = getattr(reconciler.generator, "resource_lock", None)
        if lock_fn:
            async with lock_fn(f"lb-vip-{s.hostname}"):
                node = await reconciler.client.create(kind=s.kind, data=data)
                await node.save(allow_upsert=True)
                result.node_ids[(s.kind, s.hostname)] = node.id
        else:
            node = await reconciler.client.create(kind=s.kind, data=data)
            await node.save(allow_upsert=True)
            result.node_ids[(s.kind, s.hostname)] = node.id
