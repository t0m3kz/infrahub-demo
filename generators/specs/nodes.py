"""Specs and reconcilers for tags, service contexts, VIPs, and locked find-or-create nodes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from infrahub_sdk.protocols import CoreNumberPool

from generators.protocols import LoadbalancerVIP, SecurityTag
from generators.specs.registry import PlanReconciler, ReconcileResult, register_spec


@dataclass(frozen=True)
class SecurityTagSpec:
    name: str
    group_id: int | None = None
    tag_id: str | None = None
    pool_id: str | None = None
    consumer_kind: Any = None
    consumer_id: str | None = None
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
    kind: Any = "ManagedFirewallContext"
    firewall_ha_id: str | None = None
    tenant_id: str | None = None
    track: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadBalancerVipSpec:
    hostname: str
    kind: Any = LoadbalancerVIP
    address_id: str | None = None
    load_balancer_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentGatewaySpec:
    segment_kind: str
    segment_id: str
    gateway_id: str


@register_spec(SecurityTagSpec, order=10)
async def _reconcile_security_tags(
    reconciler: PlanReconciler, specs: list[SecurityTagSpec], result: ReconcileResult
) -> None:
    for s in specs:
        tag_id = s.tag_id
        if not tag_id:
            existing = await reconciler.client.filters(kind=SecurityTag, name__value=s.name)
            if existing:
                tag_id = str(existing[0].id)
            else:
                payload: dict[str, Any] = {
                    "name": s.name,
                    **({"group_id": {"from_pool": {"id": s.pool_id}}} if s.pool_id else {}),
                    **({"group_id": s.group_id} if s.group_id is not None and not s.pool_id else {}),
                    **({"description": s.description} if s.description else {}),
                    **s.extra,
                }
                tag = await reconciler.client.create(kind=SecurityTag, data=payload)
                await tag.save(allow_upsert=True, update_group_context=False)
                tag_id = str(tag.id)
        result.node_ids[("SecurityTag", s.name)] = tag_id
        if s.consumer_kind and s.consumer_id:
            consumer = await reconciler.client.create(
                kind=s.consumer_kind,
                data={"id": s.consumer_id, "security_tag": {"id": tag_id}},
            )
            await consumer.save(allow_upsert=True, update_group_context=False)


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
        data: dict[str, Any] = {
            "name": s.name,
            **({"firewall_ha": {"id": s.firewall_ha_id}} if s.firewall_ha_id else {}),
            **({"tenant": {"id": s.tenant_id}} if s.tenant_id else {}),
            **s.extra,
        }
        save_kwargs: dict[str, Any] = {"allow_upsert": True}
        if not s.track:
            save_kwargs["update_group_context"] = False
        lock_fn = getattr(reconciler.generator, "resource_lock", None)
        if lock_fn:
            async with lock_fn(f"fw-context-{s.name}"):
                node = await reconciler.client.create(kind=s.kind, data=data)
                await node.save(**save_kwargs)
        else:
            node = await reconciler.client.create(kind=s.kind, data=data)
            await node.save(**save_kwargs)
        kind_key = s.kind if isinstance(s.kind, str) else getattr(s.kind, "__name__", str(s.kind))
        result.node_ids[(kind_key, s.name)] = node.id
        result.nodes[(s.kind, s.name)] = node


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
        else:
            node = await reconciler.client.create(kind=s.kind, data=data)
            await node.save(allow_upsert=True)
        kind_key = s.kind if isinstance(s.kind, str) else getattr(s.kind, "__name__", str(s.kind))
        result.node_ids[(kind_key, s.hostname)] = node.id


@register_spec(SegmentGatewaySpec, order=25)
async def _reconcile_segment_gateways(
    reconciler: PlanReconciler, specs: list[SegmentGatewaySpec], result: ReconcileResult
) -> None:
    for s in specs:
        seg_obj = await reconciler.client.create(
            kind=s.segment_kind,
            data={"id": s.segment_id, "gateway": {"id": s.gateway_id}},
        )
        await seg_obj.save(allow_upsert=True, update_group_context=False)
        result.node_ids[(s.segment_kind, s.segment_id)] = s.segment_id
