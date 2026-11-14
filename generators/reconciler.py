"""Batched async reconciler that persists a declarative TopologyPlan into Infrahub."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from infrahub_sdk.protocols import CoreIPAddressPool

from generators.plan import (
    INTERFACE_KIND_BY_FLAVOR,
    CableSpec,
    DeviceSpec,
    InterfaceSpec,
    IpSpec,
    TopologyPlan,
)
from generators.protocols import DcimCable, DcimInterface, IpamIPAddress


@dataclass
class ReconcileResult:
    device_ids: dict[str, str] = field(default_factory=dict)
    interface_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    interface_roles: dict[tuple[str, str], str] = field(default_factory=dict)


class PlanReconciler:
    def __init__(self, generator: Any, concurrency: int = 8) -> None:
        self.generator = generator
        self.client = generator.client
        self.sem = asyncio.Semaphore(concurrency)

    async def apply(self, plan: TopologyPlan) -> ReconcileResult:
        result = ReconcileResult()
        if plan.devices:
            await self._reconcile_devices(plan.devices, result)
        if plan.interfaces:
            await self._reconcile_interfaces(plan.interfaces, result)
        if plan.ips:
            await self._reconcile_ips(plan.ips, result)
        if plan.cables:
            await self._reconcile_cables(plan.cables, result)
        return result

    async def _reconcile_devices(self, specs: list[DeviceSpec], result: ReconcileResult) -> None:
        by_kind: dict[Any, list[DeviceSpec]] = defaultdict(list)
        for spec in specs:
            by_kind[spec.kind].append(spec)

        existing_by_kind: dict[str, dict[str, Any]] = {}
        missing_templates: set[str] = set()
        for kind, kind_specs in by_kind.items():
            existing_nodes = await self.client.filters(kind=kind, name__values=[s.name for s in kind_specs])
            if len(kind_specs) == 1 and len(existing_nodes) == 1:
                existing_map = {kind_specs[0].name: existing_nodes[0]}
            else:
                existing_map = {node.name.value: node for node in existing_nodes}
            existing_by_kind[kind] = existing_map
            for spec in kind_specs:
                if spec.name not in existing_map and spec.template_name:
                    missing_templates.add(spec.template_name)

        template_ids: dict[str, str] = {}
        if missing_templates:
            tpl_list = sorted(missing_templates)
            tpl_filter = (
                {"template_name__value": tpl_list[0]} if len(tpl_list) == 1 else {"template_name__values": tpl_list}
            )
            templates = await self.client.filters(kind="TemplateDcimPhysicalDevice", **tpl_filter)
            if len(tpl_list) == 1 and len(templates) == 1:
                template_ids = {tpl_list[0]: templates[0].id}
            else:
                template_ids = {tpl.template_name.value: tpl.id for tpl in templates}

        for kind, kind_specs in by_kind.items():
            existing_by_name = existing_by_kind[kind]

            async def _upsert_device(
                spec: DeviceSpec, existing_map: dict[str, Any] = existing_by_name
            ) -> tuple[str, str]:
                async with self.sem:
                    data: dict[str, Any] = {"name": spec.name, "status": spec.status, **spec.extra}
                    if spec.role:
                        data["role"] = spec.role
                    if spec.device_type:
                        data["device_type"] = {"id": spec.device_type}
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

                    existing = existing_map.get(spec.name)
                    if existing:
                        data["id"] = existing.id
                    elif spec.template_name:
                        tpl_id = template_ids.get(spec.template_name)
                        if not tpl_id:
                            self.generator.fail(f"Template {spec.template_name!r} not found for {spec.name!r}")
                        data["object_template"] = {"id": tpl_id}

                    node = await self.client.create(kind=kind, data=data)
                    await node.save(allow_upsert=True)
                    return spec.name, node.id

            saved = await asyncio.gather(*(_upsert_device(spec) for spec in kind_specs))
            result.device_ids.update(saved)

        discover_specs = [s for s in specs if s.template_name or s.discover_interface_kind]
        if discover_specs:
            id_to_name = {dev_id: name for name, dev_id in result.device_ids.items()}
            only_name = discover_specs[0].name if len(discover_specs) == 1 else None
            iface_kind = discover_specs[0].discover_interface_kind or DcimInterface
            flt: dict[str, Any] = {"device__ids": [result.device_ids[s.name] for s in discover_specs]}
            if len(discover_specs) == 1 and discover_specs[0].discover_interface_role:
                flt["role__value"] = discover_specs[0].discover_interface_role
            for iface in await self.client.filters(kind=iface_kind, **flt):
                dev_id = getattr(getattr(iface, "device", None), "id", None)
                dev_name = id_to_name.get(dev_id) or only_name
                if dev_name:
                    key = (dev_name, iface.name.value)
                    result.interface_ids[key] = iface.id
                    role_val = discover_specs[0].discover_interface_role if len(discover_specs) == 1 else None
                    role_attr = getattr(iface, "role", None)
                    raw_role = getattr(role_attr, "value", None)
                    if isinstance(raw_role, str):
                        role_val = raw_role
                    if role_val:
                        result.interface_roles[key] = role_val

    async def _reconcile_interfaces(self, specs: list[InterfaceSpec], result: ReconcileResult) -> None:
        base_specs = [s for s in specs if s.flavor != "subinterface" and not s.lag_parent]
        dependent_specs = [s for s in specs if s.flavor == "subinterface" or s.lag_parent]

        for batch in (base_specs, dependent_specs):
            if not batch:
                continue

            async def _upsert_iface(spec: InterfaceSpec) -> tuple[tuple[str, str], str]:
                async with self.sem:
                    dev_id = result.device_ids.get(spec.device)
                    if not dev_id:
                        self.generator.fail(f"Interface {spec.device}:{spec.name} references unknown device")

                    kind = INTERFACE_KIND_BY_FLAVOR[spec.flavor]
                    default_role = "loopback" if spec.flavor == "loopback" else "svi" if spec.flavor == "svi" else None
                    role = spec.role or default_role
                    data: dict[str, Any] = {
                        "name": spec.name,
                        "device": {"id": dev_id},
                        "enabled": spec.enabled,
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
                        parent_id = result.interface_ids.get((spec.device, spec.parent_interface))
                        if not parent_id:
                            self.generator.fail(f"Subinterface {spec.device}:{spec.name} missing parent")
                        data["parent_interface"] = {"id": parent_id}
                    if spec.lag_parent:
                        lag_id = result.interface_ids.get((spec.device, spec.lag_parent))
                        if not lag_id:
                            self.generator.fail(f"Interface {spec.device}:{spec.name} missing LAG parent")
                        data["lag"] = {"id": lag_id}

                    existing_id = result.interface_ids.get((spec.device, spec.name))
                    if existing_id:
                        data["id"] = existing_id

                    node = await self.client.create(kind=kind, data=data)
                    await node.save(allow_upsert=True)
                    return (spec.device, spec.name), node.id

            saved = await asyncio.gather(*(_upsert_iface(spec) for spec in batch))
            result.interface_ids.update(saved)

    async def _reconcile_ips(self, specs: list[IpSpec], result: ReconcileResult) -> None:
        pool_names = sorted({spec.pool for spec in specs if spec.pool})
        pools_by_name = {name: await self.generator._resolve_pool(None, CoreIPAddressPool, name) for name in pool_names}

        async def _assign_ip(spec: IpSpec) -> None:
            async with self.sem:
                iface_id = result.interface_ids.get((spec.device, spec.interface))
                if not iface_id:
                    self.generator.fail(f"IP assignment references unknown interface {spec.device}:{spec.interface}")

                if spec.pool:
                    pool_obj = pools_by_name.get(spec.pool)
                    if not pool_obj:
                        self.generator.fail(f"IP address pool {spec.pool!r} not found")
                    alloc_kwargs: dict[str, Any] = {"data": {"interface": {"id": iface_id}}}
                    if spec.prefix_length is not None:
                        alloc_kwargs["prefix_length"] = spec.prefix_length
                    await self.client.allocate_next_ip_address(resource_pool=pool_obj, **alloc_kwargs)
                elif spec.address:
                    data: dict[str, Any] = {"address": spec.address, "interface": {"id": iface_id}}
                    if spec.namespace_id:
                        data["ip_namespace"] = {"id": spec.namespace_id}
                    if spec.role:
                        data["role"] = spec.role
                    ip_node = await self.client.create(kind=IpamIPAddress, data=data)
                    await ip_node.save(allow_upsert=True)

        await asyncio.gather(*(_assign_ip(spec) for spec in specs))

    async def _reconcile_cables(self, specs: list[CableSpec], result: ReconcileResult) -> None:
        async def _upsert_cable(spec: CableSpec) -> None:
            async with self.sem:
                a_id = result.interface_ids.get((spec.a_device, spec.a_interface))
                b_id = result.interface_ids.get((spec.b_device, spec.b_interface))
                if not a_id or not b_id:
                    self.generator.fail(
                        f"Cable {spec.a_device}:{spec.a_interface} <-> {spec.b_device}:{spec.b_interface} unresolved"
                    )
                data: dict[str, Any] = {
                    "connected_endpoints": [{"id": a_id}, {"id": b_id}],
                    "interconnection_layer": "physical",
                }
                if spec.cable_type:
                    data["cable_type"] = spec.cable_type
                cable = await self.client.create(kind=DcimCable, data=data)
                await cable.save(allow_upsert=True)

        await asyncio.gather(*(_upsert_cable(spec) for spec in specs))
