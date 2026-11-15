"""Batched async reconciler that persists a declarative TopologyPlan into Infrahub."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from infrahub_sdk.protocols import CoreIPAddressPool, CoreNumberPool

from generators.plan import (
    INTERFACE_KIND_BY_FLAVOR,
    BgpPeeringSpec,
    CableSpec,
    DeviceSpec,
    FirewallContextSpec,
    InterfaceSpec,
    IpSpec,
    LoadBalancerVipSpec,
    RoutingTagSpec,
    SecurityTagSpec,
    SegmentGatewaySpec,
    TopologyPlan,
    VrfSpec,
)
from generators.protocols import DcimCable, DcimInterface, IpamIPAddress, SecurityTag
from utils.limits import RECONCILE_CONCURRENCY


@dataclass
class ReconcileResult:
    device_ids: dict[str, str] = field(default_factory=dict)
    interface_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    interface_roles: dict[tuple[str, str], str] = field(default_factory=dict)
    node_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    nodes: dict[tuple[Any, str], Any] = field(default_factory=dict)


class PlanReconciler:
    """Applies a TopologyPlan in strict dependency order so links never outrun endpoints."""

    def __init__(self, generator: Any, *, concurrency: int = RECONCILE_CONCURRENCY) -> None:
        self.generator = generator
        self.client = generator.client
        self.sem = asyncio.Semaphore(concurrency)

    async def apply(self, plan: TopologyPlan) -> ReconcileResult:
        result = ReconcileResult()
        if not plan.items:
            return result

        by_type: dict[type[Any], list[Any]] = {}
        for item in plan.items:
            by_type.setdefault(type(item), []).append(item)

        if specs := by_type.get(SecurityTagSpec):
            await self._reconcile_security_tags(specs, result)
        if specs := by_type.get(RoutingTagSpec):
            await self._reconcile_routing_tags(specs, result)
        if specs := by_type.get(FirewallContextSpec):
            await self._reconcile_firewall_contexts(specs, result)
        if specs := by_type.get(LoadBalancerVipSpec):
            await self._reconcile_lb_vips(specs, result)
        if specs := by_type.get(SegmentGatewaySpec):
            await self._reconcile_segment_gateways(specs, result)
        if specs := by_type.get(DeviceSpec):
            await self._reconcile_devices(specs, result)
        if specs := by_type.get(InterfaceSpec):
            await self._reconcile_interfaces(specs, result)
        if specs := by_type.get(IpSpec):
            await self._reconcile_ips(specs, result)
        if specs := by_type.get(VrfSpec):
            await self._reconcile_vrfs(specs, result)
        if specs := by_type.get(BgpPeeringSpec):
            await self._reconcile_bgp_peerings(specs, result)
        if specs := by_type.get(CableSpec):
            await self._reconcile_cables(specs, result)

        return result

    async def _reconcile_security_tags(self, specs: list[SecurityTagSpec], result: ReconcileResult) -> None:
        for s in specs:
            tag_id = s.tag_id
            if not tag_id:
                existing = await self.client.filters(kind=SecurityTag, name__value=s.name)
                if existing:
                    tag_id = str(existing.id)
                else:
                    payload: dict[str, Any] = {
                        "name": s.name,
                        **({"group_id": {"from_pool": {"id": s.pool_id}}} if s.pool_id else {}),
                        **({"group_id": s.group_id} if s.group_id is not None and not s.pool_id else {}),
                        **({"description": s.description} if s.description else {}),
                        **s.extra,
                    }
                    tag = await self.client.create(kind=SecurityTag, data=payload)
                    await tag.save(allow_upsert=True, update_group_context=False)
                    tag_id = str(tag.id)
            result.node_ids[("SecurityTag", s.name)] = tag_id
            if s.consumer_kind and s.consumer_id:
                consumer = await self.client.create(
                    kind=s.consumer_kind, data={"id": s.consumer_id, "security_tag": {"id": tag_id}}
                )
                await consumer.save(allow_upsert=True, update_group_context=False)

    async def _reconcile_routing_tags(self, specs: list[RoutingTagSpec], result: ReconcileResult) -> None:
        for s in specs:
            pool = await self.client.get(kind=CoreNumberPool, name__value=s.pool_name)
            consumer = await self.client.create(
                kind=s.kind, data={"id": s.node_id, "routing_tag": {"from_pool": {"id": pool.id}}}
            )
            await consumer.save(allow_upsert=True, update_group_context=False)
            result.node_ids[(s.kind, s.name or s.node_id)] = s.node_id

    async def _reconcile_firewall_contexts(self, specs: list[FirewallContextSpec], result: ReconcileResult) -> None:
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
            node = await self.client.create(kind=s.kind, data=data)
            await node.save(**save_kwargs)
            kind_key = s.kind if isinstance(s.kind, str) else getattr(s.kind, "__name__", str(s.kind))
            result.node_ids[(kind_key, s.name)] = node.id
            result.nodes[(s.kind, s.name)] = node

    async def _reconcile_lb_vips(self, specs: list[LoadBalancerVipSpec], result: ReconcileResult) -> None:
        for s in specs:
            data: dict[str, Any] = {
                "hostname": s.hostname,
                **({"vip_ip": {"id": s.address_id}} if s.address_id else {}),
                **({"load_balancer": {"id": s.load_balancer_id}} if s.load_balancer_id else {}),
                **s.extra,
            }
            node = await self.client.create(kind=s.kind, data=data)
            await node.save(allow_upsert=True)
            kind_key = s.kind if isinstance(s.kind, str) else getattr(s.kind, "__name__", str(s.kind))
            result.node_ids[(kind_key, s.hostname)] = node.id

    async def _reconcile_segment_gateways(self, specs: list[SegmentGatewaySpec], result: ReconcileResult) -> None:
        for s in specs:
            seg_obj = await self.client.create(
                kind=s.segment_kind, data={"id": s.segment_id, "gateway": {"id": s.gateway_id}}
            )
            await seg_obj.save(allow_upsert=True, update_group_context=False)
            result.node_ids[(s.segment_kind, s.segment_id)] = s.segment_id

    async def _reconcile_devices(self, specs: list[DeviceSpec], result: ReconcileResult) -> None:
        by_kind: dict[Any, list[DeviceSpec]] = {}
        for s in specs:
            by_kind.setdefault(s.kind, []).append(s)

        existing_by_name: dict[str, Any] = {}
        for kind, group in by_kind.items():
            for dev in await self.client.filters(kind=kind, name__values=[s.name for s in group]):
                existing_by_name[dev.name.value] = dev

        async def _upsert(spec: DeviceSpec) -> tuple[str, str]:
            async with self.sem:
                data: dict[str, Any] = {"name": spec.name, "status": spec.status, **spec.extra}
                if spec.template_name:
                    data["object_template"] = spec.template_name
                if spec.device_type:
                    data["device_type"] = {"id": spec.device_type}
                if spec.role:
                    data["role"] = spec.role
                if spec.owner_id:
                    data["owner"] = {"id": spec.owner_id}
                if spec.deployment_id:
                    data["deployment"] = {"id": spec.deployment_id}
                if spec.rack_id:
                    data["rack"] = {"id": spec.rack_id}
                if spec.rack_unit is not None:
                    data["rack_unit"] = spec.rack_unit
                if spec.platform_id:
                    data["platform"] = {"id": spec.platform_id}
                if spec.description:
                    data["description"] = spec.description
                if spec.name in existing_by_name:
                    data["id"] = existing_by_name[spec.name].id
                node = await self.client.create(kind=spec.kind, data=data)
                await node.save(allow_upsert=True)
                return spec.name, node.id

        result.device_ids.update(await asyncio.gather(*(_upsert(s) for s in specs)))

    async def _reconcile_interfaces(self, specs: list[InterfaceSpec], result: ReconcileResult) -> None:
        base_specs = [s for s in specs if s.flavor != "subinterface" and not s.lag_parent]
        dep_specs = [s for s in specs if s.flavor == "subinterface" or s.lag_parent]
        for batch in (base_specs, dep_specs):
            if not batch:
                continue

            async def _upsert(spec: InterfaceSpec) -> tuple[tuple[str, str], str]:
                async with self.sem:
                    dev_id = result.device_ids.get(spec.device)
                    if not dev_id:
                        self.generator.fail(f"Interface {spec.device}:{spec.name} references unknown device")
                    kind = INTERFACE_KIND_BY_FLAVOR[spec.flavor]
                    role = spec.role or (
                        "loopback" if spec.flavor == "loopback" else "svi" if spec.flavor == "svi" else None
                    )
                    data: dict[str, Any] = {
                        "name": spec.name,
                        "device": {"id": dev_id},
                        "status": "active" if spec.enabled else "disabled",
                        **spec.extra,
                    }
                    if role:
                        data["role"] = role
                    if spec.mtu is not None:
                        data["mtu"] = spec.mtu
                    if spec.vlan_id is not None:
                        data["vlan_id"] = spec.vlan_id
                    if spec.vrf_id:
                        data["vrf"] = {"id": spec.vrf_id}
                    if spec.description:
                        data["description"] = spec.description
                    if spec.parent_interface:
                        pid = result.interface_ids.get((spec.device, spec.parent_interface))
                        if not pid:
                            self.generator.fail(f"Subinterface {spec.device}:{spec.name} missing parent")
                        data["parent_interface"] = {"id": pid}
                    if spec.lag_parent:
                        lid = result.interface_ids.get((spec.device, spec.lag_parent))
                        if not lid:
                            self.generator.fail(f"Interface {spec.device}:{spec.name} missing LAG parent")
                        data["lag"] = {"id": lid}
                    if existing_id := result.interface_ids.get((spec.device, spec.name)):
                        data["id"] = existing_id
                    node = await self.client.create(kind=kind, data=data)
                    await node.save(allow_upsert=True)
                    return (spec.device, spec.name), node.id

            result.interface_ids.update(await asyncio.gather(*(_upsert(s) for s in batch)))
            for s in batch:
                role = s.role or ("loopback" if s.flavor == "loopback" else "svi" if s.flavor == "svi" else None)
                if role:
                    result.interface_roles[(s.device, s.name)] = role

    async def _reconcile_ips(self, specs: list[IpSpec], result: ReconcileResult) -> None:
        pool_names = sorted({s.pool for s in specs if s.pool})
        resolved = await asyncio.gather(*(self.generator._resolve_pool(None, CoreIPAddressPool, n) for n in pool_names))
        pools_by_name = dict(zip(pool_names, resolved, strict=True))

        async def _assign(spec: IpSpec) -> None:
            async with self.sem:
                iface_id = result.interface_ids.get((spec.device, spec.interface))
                if not iface_id:
                    self.generator.fail(f"IP assignment references unknown interface {spec.device}:{spec.interface}")
                if spec.pool:
                    pool_obj = pools_by_name.get(spec.pool)
                    if not pool_obj:
                        self.generator.fail(f"IP address pool {spec.pool!r} not found")
                    kw: dict[str, Any] = {"prefix_length": spec.prefix_length} if spec.prefix_length is not None else {}
                    ip_node = await self.client.allocate_next_ip_address(resource_pool=pool_obj, **kw)
                elif spec.address:
                    data: dict[str, Any] = {"address": spec.address}
                    if spec.namespace_id:
                        data["ip_namespace"] = {"id": spec.namespace_id}
                    ip_node = await self.client.create(kind=IpamIPAddress, data=data)
                    await ip_node.save(allow_upsert=True)
                else:
                    return
                patch = await self.client.create(
                    kind=DcimInterface, data={"id": iface_id, "ip_address": {"id": ip_node.id}}
                )
                await patch.save(allow_upsert=True)

        await asyncio.gather(*(_assign(s) for s in specs))

    async def _reconcile_vrfs(self, specs: list[VrfSpec], result: ReconcileResult) -> None:
        existing = {
            v.name.value: v for v in await self.client.filters(kind="RoutingVRF", name__values=[s.name for s in specs])
        }
        for s in specs:
            data: dict[str, Any] = {"name": s.name, **s.extra}
            if s.rd:
                data["rd"] = s.rd
            if s.description:
                data["description"] = s.description
            if s.name in existing:
                data["id"] = existing[s.name].id
            node = await self.client.create(kind="RoutingVRF", data=data)
            await node.save(allow_upsert=True)
            result.node_ids[("RoutingVRF", s.name)] = node.id

    async def _reconcile_bgp_peerings(self, specs: list[BgpPeeringSpec], result: ReconcileResult) -> None:
        existing = {
            p.name.value: p
            for p in await self.client.filters(kind="RoutingBGPPeerGroup", name__values=[s.name for s in specs])
        }
        for s in specs:
            data: dict[str, Any] = {"name": s.name, **s.extra}
            if s.local_asn:
                data["local_as"] = s.local_asn
            if s.remote_asn:
                data["remote_as"] = s.remote_asn
            if s.name in existing:
                data["id"] = existing[s.name].id
            node = await self.client.create(kind="RoutingBGPPeerGroup", data=data)
            await node.save(allow_upsert=True)
            result.node_ids[("RoutingBGPPeerGroup", s.name)] = node.id

    async def _reconcile_cables(self, specs: list[CableSpec], result: ReconcileResult) -> None:
        async def _cable(spec: CableSpec) -> None:
            async with self.sem:
                a_id = result.interface_ids.get((spec.a_device, spec.a_interface))
                b_id = result.interface_ids.get((spec.b_device, spec.b_interface))
                if not a_id or not b_id:
                    self.generator.fail(
                        f"Cable {spec.a_device}:{spec.a_interface} <-> {spec.b_device}:{spec.b_interface} missing endpoint"
                    )
                data: dict[str, Any] = {"endpoints": [{"id": a_id}, {"id": b_id}], **spec.extra}
                if spec.cable_type:
                    data["cable_type"] = spec.cable_type
                if spec.label:
                    data["label"] = spec.label
                cable = await self.client.create(kind=DcimCable, data=data)
                await cable.save(allow_upsert=True)

        await asyncio.gather(*(_cable(s) for s in specs))
