"""Tests for spec registry execution order and mid-run crash recovery across topologies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from generators.plan import (
    BgpPeeringSpec,
    CableSpec,
    DeviceSpec,
    FirewallContextSpec,
    InterfaceSpec,
    IpSpec,
    LoadBalancerVipSpec,
    RoutingTagSpec,
    SecurityTagSpec,
    TopologyPlan,
    VrfSpec,
)
from generators.reconciler import PlanReconciler


class _StatefulFakeClient:
    """In-memory fake InfrahubClient that persists upserts by (kind, unique_key) across retries."""

    def __init__(self, *, fail_on_kind: str | None = None) -> None:
        self.store: dict[tuple[str, str], Any] = {}
        self.fail_on_kind = fail_on_kind
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}-{self._seq}"

    async def get(self, *, kind: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(id="pool-rt-1", name=SimpleNamespace(value=kwargs.get("name__value", "pool")))

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        kind_name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
        names = set(kwargs.get("name__values") or [])
        if "name__value" in kwargs:
            names.add(kwargs["name__value"])
        if not names:
            return []
        return [obj for (k, _), obj in self.store.items() if k == kind_name and obj.name.value in names]

    async def create(self, *, kind: Any, data: dict[str, Any]) -> Any:
        kind_name = kind if isinstance(kind, str) else getattr(kind, "__name__", str(kind))
        if self.fail_on_kind == kind_name:
            self.fail_on_kind = None
            raise RuntimeError(f"Simulated mid-run crash on {kind_name}")

        if kind_name == "DcimCable":
            endpoints = sorted(ep["id"] for ep in data["endpoints"])
            key = f"{endpoints[0]}<->{endpoints[1]}"
        elif "device" in data:
            key = f"{data['device']['id']}:{data['name']}"
        elif "address" in data:
            key = str(data["address"])
        else:
            key = str(data.get("name") or self._next_id(kind_name))

        existing = self.store.get((kind_name, key))
        obj_id = data.get("id") or (existing.id if existing else self._next_id(kind_name))
        node = SimpleNamespace(
            id=obj_id,
            name=SimpleNamespace(value=data.get("name", key)),
            device=SimpleNamespace(id=data["device"]["id"]) if "device" in data else None,
        )

        async def _save(**_kw: Any) -> None:
            self.store[(kind_name, key)] = node

        node.save = _save
        return node


def _build_topology_plan(topology: str) -> TopologyPlan:
    plan = TopologyPlan()
    if topology == "DC":
        plan.add(SecurityTagSpec(name="dc-sec-tag", group_id=100))
        plan.add(FirewallContextSpec(name="dc-fw-ctx"))
        plan.add(LoadBalancerVipSpec(hostname="dc-lb-vip"))
        plan.add(DeviceSpec(name="dc1-sp01", role="spine"))
        plan.add(DeviceSpec(name="dc1-lf01", role="leaf"))
        plan.add(InterfaceSpec(device="dc1-sp01", name="Ethernet1"))
        plan.add(InterfaceSpec(device="dc1-lf01", name="Ethernet1"))
        plan.add(IpSpec(device="dc1-sp01", interface="Ethernet1", address="10.0.0.0/31"))
        plan.add(VrfSpec(name="PROD"))
        plan.add(BgpPeeringSpec(name="dc1-sp01-lf01"))
        plan.add(CableSpec(a_device="dc1-lf01", a_interface="Ethernet1", b_device="dc1-sp01", b_interface="Ethernet1"))
    elif topology == "Colocation":
        plan.add(
            RoutingTagSpec(
                kind="ManagedVxlanSegment", node_id="seg-1", pool_name="Segment-Routing-Tag", name="colo-rt-tag"
            )
        )
        plan.add(DeviceSpec(name="edg-ams01", role="edge"))
        plan.add(DeviceSpec(name="fw-ams01", role="internal-firewall"))
        plan.add(InterfaceSpec(device="edg-ams01", name="xe-0/0/0"))
        plan.add(InterfaceSpec(device="fw-ams01", name="ethernet1/1"))
        plan.add(IpSpec(device="edg-ams01", interface="xe-0/0/0", address="10.1.0.0/31"))
        plan.add(
            CableSpec(a_device="fw-ams01", a_interface="ethernet1/1", b_device="edg-ams01", b_interface="xe-0/0/0")
        )
    else:
        plan.add(DeviceSpec(name="dist-off01", role="distribution-switch"))
        plan.add(DeviceSpec(name="acc-off01", role="access-switch"))
        plan.add(InterfaceSpec(device="dist-off01", name="GigabitEthernet1/0/1"))
        plan.add(InterfaceSpec(device="acc-off01", name="GigabitEthernet1/0/1"))
        plan.add(IpSpec(device="dist-off01", interface="GigabitEthernet1/0/1", address="10.2.0.0/31"))
        plan.add(
            CableSpec(
                a_device="acc-off01",
                a_interface="GigabitEthernet1/0/1",
                b_device="dist-off01",
                b_interface="GigabitEthernet1/0/1",
            )
        )
    return plan


@pytest.mark.parametrize("topology", ["DC", "Colocation", "Office"])
async def test_mid_run_failure_recovers_idempotently_on_rerun(topology: str) -> None:
    client = _StatefulFakeClient(fail_on_kind="DcimPhysicalInterface")
    gen = SimpleNamespace(client=client)
    plan = _build_topology_plan(topology)

    # First run crashes mid-way during step 40 (interfaces) after step 30 (devices) persisted
    with pytest.raises(RuntimeError, match="Simulated mid-run crash"):
        await PlanReconciler(gen).apply(plan)

    assert len([k for k in client.store if k[0] == "DcimPhysicalDevice"]) == 2
    assert len([k for k in client.store if k[0] == "DcimCable"]) == 0

    # Re-run converges cleanly with zero duplicates
    result = await PlanReconciler(gen).apply(plan)
    assert len(result.device_ids) == 2
    assert len(result.interface_ids) == 2
    assert len([k for k in client.store if k[0] == "DcimPhysicalDevice"]) == 2
    assert len([k for k in client.store if k[0] == "DcimPhysicalInterface"]) == 2
    assert len([k for k in client.store if k[0] == "DcimCable"]) == 1
