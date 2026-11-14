"""Unit tests for declarative TopologyPlan, PlanReconciler, and SdwanEdgeGenerator.build_plan()."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.fabric.sdwan_edge import SdwanEdgeGenerator
from generators.plan import CableSpec, DeviceSpec, InterfaceSpec, IpSpec, TopologyPlan
from generators.reconciler import PlanReconciler


def test_sdwan_edge_build_plan_emits_device_spec() -> None:
    plan = SdwanEdgeGenerator.build_plan(office_name="munich-01", office_id="off-1", owner_id="own-1")
    assert len(plan.devices) == 1
    dev = plan.devices[0]
    assert dev.name == "munich-01-EDGE1"
    assert dev.template_name == "VCE-610_EDGE"
    assert dev.owner_id == "own-1"
    assert dev.deployment_id == "off-1"


@pytest.mark.asyncio
async def test_plan_reconciler_batches_devices_interfaces_ips_and_cables() -> None:
    created_calls: list[tuple[str, dict]] = []

    async def _create(kind: object, data: dict) -> MagicMock:
        kind_name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
        created_calls.append((kind_name, data))
        node = MagicMock()
        node.id = f"{kind_name}:{data.get('name', len(created_calls))}"
        node.save = AsyncMock()
        return node

    tpl = SimpleNamespace(id="tpl-1", template_name=SimpleNamespace(value="VCE-610_EDGE"))
    template_uplink = SimpleNamespace(
        id="if-ge0",
        device=SimpleNamespace(id="DcimPhysicalDevice:leaf-01"),
        name=SimpleNamespace(value="ge-0/0/0"),
        role=SimpleNamespace(value="uplink"),
    )

    async def _filters(kind: object, **_kwargs: object) -> list:
        kind_name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
        if kind_name == "TemplateDcimPhysicalDevice":
            return [tpl]
        if kind_name == "DcimInterface":
            return [template_uplink]
        return []

    gen = MagicMock()
    gen.client.create = AsyncMock(side_effect=_create)
    gen.client.filters = AsyncMock(side_effect=_filters)
    gen.client.allocate_next_ip_address = AsyncMock()
    gen._resolve_pool = AsyncMock(return_value=SimpleNamespace(id="pool-lo"))

    plan = TopologyPlan()
    plan.add_device(DeviceSpec(name="leaf-01", template_name="VCE-610_EDGE"))
    plan.add_interface(InterfaceSpec(device="leaf-01", name="Loopback0", flavor="loopback"))
    plan.add_interface(
        InterfaceSpec(
            device="leaf-01", name="ge-0/0/0.100", flavor="subinterface", parent_interface="ge-0/0/0", vlan_id=100
        )
    )
    plan.add_ip(IpSpec(device="leaf-01", interface="Loopback0", pool="Loopback-Pool"))
    plan.add_cable(CableSpec(a_device="leaf-01", a_interface="ge-0/0/0", b_device="leaf-01", b_interface="Loopback0"))

    result = await PlanReconciler(gen).apply(plan)

    assert result.device_ids["leaf-01"] == "DcimPhysicalDevice:leaf-01"
    assert result.interface_roles[("leaf-01", "ge-0/0/0")] == "uplink"
    assert ("leaf-01", "Loopback0") in result.interface_ids
    assert ("leaf-01", "ge-0/0/0.100") in result.interface_ids
    gen.client.allocate_next_ip_address.assert_awaited_once()
