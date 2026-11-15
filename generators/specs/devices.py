"""Specs and reconcilers for devices, interfaces, and IP address assignments."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Literal

from infrahub_sdk.protocols import CoreIPAddressPool

from generators.protocols import DcimInterface, IpamIPAddress
from generators.specs.registry import PlanReconciler, ReconcileResult, register_spec

InterfaceFlavor = Literal["physical", "loopback", "lag", "svi", "subinterface", "virtual"]

INTERFACE_KIND_BY_FLAVOR: dict[InterfaceFlavor, str] = {
    "physical": "DcimPhysicalInterface",
    "loopback": "DcimVirtualInterface",
    "lag": "DcimLagInterface",
    "svi": "DcimVirtualInterface",
    "subinterface": "DcimSubInterface",
    "virtual": "DcimVirtualInterface",
}


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    kind: Any = "DcimPhysicalDevice"
    template_name: str | None = None
    discover_interface_kind: Any = None
    discover_interface_role: str | None = None
    device_type: str | None = None
    role: str | None = None
    status: str = "active"
    owner_id: str | None = None
    deployment_id: str | None = None
    rack_id: str | None = None
    rack_unit: int | None = None
    platform_id: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InterfaceSpec:
    device: str
    name: str
    flavor: InterfaceFlavor = "physical"
    role: str | None = None
    mtu: int | None = None
    enabled: bool = True
    vlan_id: int | None = None
    parent_interface: str | None = None
    lag_parent: str | None = None
    vrf_id: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IpSpec:
    device: str
    interface: str
    pool: str | None = None
    address: str | None = None
    prefix_length: int | None = None
    namespace_id: str | None = None
    role: str | None = None


@register_spec(DeviceSpec, order=30)
async def _reconcile_devices(reconciler: PlanReconciler, specs: list[DeviceSpec], result: ReconcileResult) -> None:
    by_kind: dict[Any, list[DeviceSpec]] = defaultdict(list)
    for spec in specs:
        by_kind[spec.kind].append(spec)

    existing_by_kind: dict[str, dict[str, Any]] = {}
    missing_templates: set[str] = set()
    for kind, kind_specs in by_kind.items():
        existing_nodes = await reconciler.client.filters(kind=kind, name__values=[s.name for s in kind_specs])
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
        templates = await reconciler.client.filters(kind="TemplateDcimPhysicalDevice", **tpl_filter)
        if len(tpl_list) == 1 and len(templates) == 1:
            template_ids = {tpl_list[0]: templates[0].id}
        else:
            template_ids = {tpl.template_name.value: tpl.id for tpl in templates}

    for kind, kind_specs in by_kind.items():
        existing_by_name = existing_by_kind[kind]

        async def _upsert_device(spec: DeviceSpec, existing_map: dict[str, Any] = existing_by_name) -> tuple[str, str]:
            async with reconciler.sem:
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
                        reconciler.generator.fail(f"Template {spec.template_name!r} not found for {spec.name!r}")
                    data["object_template"] = {"id": tpl_id}

                node = await reconciler.client.create(kind=kind, data=data)
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
        for iface in await reconciler.client.filters(kind=iface_kind, **flt):
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


@register_spec(InterfaceSpec, order=40)
async def _reconcile_interfaces(
    reconciler: PlanReconciler, specs: list[InterfaceSpec], result: ReconcileResult
) -> None:
    base_specs = [s for s in specs if s.flavor != "subinterface" and not s.lag_parent]
    dependent_specs = [s for s in specs if s.flavor == "subinterface" or s.lag_parent]

    for batch in (base_specs, dependent_specs):
        if not batch:
            continue

        async def _upsert_iface(spec: InterfaceSpec) -> tuple[tuple[str, str], str]:
            async with reconciler.sem:
                dev_id = result.device_ids.get(spec.device)
                if not dev_id:
                    reconciler.generator.fail(f"Interface {spec.device}:{spec.name} references unknown device")

                kind = INTERFACE_KIND_BY_FLAVOR[spec.flavor]
                default_role = "loopback" if spec.flavor == "loopback" else "svi" if spec.flavor == "svi" else None
                role = spec.role or default_role
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
                    parent_id = result.interface_ids.get((spec.device, spec.parent_interface))
                    if not parent_id:
                        reconciler.generator.fail(f"Subinterface {spec.device}:{spec.name} missing parent")
                    data["parent_interface"] = {"id": parent_id}
                if spec.lag_parent:
                    lag_id = result.interface_ids.get((spec.device, spec.lag_parent))
                    if not lag_id:
                        reconciler.generator.fail(f"Interface {spec.device}:{spec.name} missing LAG parent")
                    data["lag"] = {"id": lag_id}

                existing_id = result.interface_ids.get((spec.device, spec.name))
                if existing_id:
                    data["id"] = existing_id

                node = await reconciler.client.create(kind=kind, data=data)
                await node.save(allow_upsert=True)
                return (spec.device, spec.name), node.id

        saved = await asyncio.gather(*(_upsert_iface(spec) for spec in batch))
        result.interface_ids.update(saved)


@register_spec(IpSpec, order=50)
async def _reconcile_ips(reconciler: PlanReconciler, specs: list[IpSpec], result: ReconcileResult) -> None:
    pool_names = sorted({spec.pool for spec in specs if spec.pool})
    resolved = await asyncio.gather(
        *(reconciler.generator._resolve_pool(None, CoreIPAddressPool, name) for name in pool_names)
    )
    pools_by_name = dict(zip(pool_names, resolved, strict=True))

    async def _assign_ip(spec: IpSpec) -> None:
        async with reconciler.sem:
            iface_id = result.interface_ids.get((spec.device, spec.interface))
            if not iface_id:
                reconciler.generator.fail(f"IP assignment references unknown interface {spec.device}:{spec.interface}")

            if spec.pool:
                pool_obj = pools_by_name.get(spec.pool)
                if not pool_obj:
                    reconciler.generator.fail(f"IP address pool {spec.pool!r} not found")
                alloc_kwargs: dict[str, Any] = {}
                if spec.prefix_length is not None:
                    alloc_kwargs["prefix_length"] = spec.prefix_length
                ip_node = await reconciler.client.allocate_next_ip_address(resource_pool=pool_obj, **alloc_kwargs)
            elif spec.address:
                data: dict[str, Any] = {"address": spec.address}
                if spec.namespace_id:
                    data["ip_namespace"] = {"id": spec.namespace_id}
                ip_node = await reconciler.client.create(kind=IpamIPAddress, data=data)
                await ip_node.save(allow_upsert=True)
            else:
                return

            iface_patch = await reconciler.client.create(
                kind=DcimInterface,
                data={"id": iface_id, "ip_address": {"id": ip_node.id}},
            )
            await iface_patch.save(allow_upsert=True)

    await asyncio.gather(*(_assign_ip(spec) for spec in specs))
