"""The plan builders of a hosted virtual device and of the HA domain of two of them (generators/dcim/device_plan.py,
generators/capabilities/ha.py), as data."""

from __future__ import annotations

from typing import Any

import pytest

from generators.capabilities.ha import (
    HaDomain,
    HaReads,
    plan_virtual_ha_pair,
    read_virtual_ha_reads,
    virtual_ha_lock_keys,
)
from generators.dcim.device_plan import DeviceReads, ExistingDevice, plan_virtual_device, read_device_state
from generators.plan import AddressAllocationSpec, EdgesSpec, Hfid, NodeRef, NodeSpec, TopologyPlan, TrackSpec
from tests.unit.customer_world import CustomerWorld

TEMPLATE = {"id": "tmpl-1", "device_type": {"id": "dt-1"}, "platform": {"id": "pf-1"}}


def device_plan(reads: DeviceReads | None = None, controller: dict[str, Any] | None = None) -> TopologyPlan:
    plan = TopologyPlan(root_id=None)
    plan_virtual_device(
        plan,
        reads=reads or DeviceReads(),
        name="c005-DC10-FW1-prod",
        fabric_name="dc10",
        role="internal-firewall",
        deployment_id="dc10-id",
        template=TEMPLATE,
        host_id="host-1",
        controller=controller,
    )
    return plan


class TestPlanVirtualDevice:
    def test_a_new_instance_is_the_runs_own_hosted_from_the_template_with_a_management_address(self) -> None:
        plan = device_plan()

        (address,) = plan.of(AddressAllocationSpec)
        assert (address.identifier, address.pool_name, address.prefix_length, address.ownership) == (
            "c005-DC10-FW1-prod",
            "dc10-management-pool",
            32,
            "shared",
        )
        (group, device) = [spec for spec in plan.of(NodeSpec)]
        assert (group.kind, group.data["name"], group.ownership) == ("CoreStandardGroup", "firewalls", "shared")
        assert (device.kind, device.ownership, device.hfid) == (
            "DcimVirtualDevice",
            "owned",
            Hfid("DcimVirtualDevice", ("c005-DC10-FW1-prod",)),
        )
        assert device.data == {
            "name": "c005-DC10-FW1-prod",
            "object_template": {"id": "tmpl-1"},
            "status": "active",
            "role": "internal-firewall",
            "deployment": {"id": "dc10-id"},
            "device_type": {"id": "dt-1"},
            "platform": {"id": "pf-1"},
            "hosting_device": {"id": "host-1"},
            "primary_address": NodeRef("management:c005-DC10-FW1-prod"),
            "member_of_groups": [{"id": NodeRef("group:firewalls")}],
        }

    def test_an_existing_instance_keeps_its_address_and_groups_and_never_resends_its_template(self) -> None:
        reads = DeviceReads(
            devices={"c005-DC10-FW1-prod": ExistingDevice(group_ids=("g-other",), primary_address_id="addr-1")},
            groups={"firewalls": "g-fw"},
        )

        plan = device_plan(reads)

        assert plan.of(AddressAllocationSpec) == []
        (device,) = plan.of(NodeSpec)
        assert "object_template" not in device.data
        assert device.data["primary_address"] == "addr-1"
        assert device.data["member_of_groups"] == [{"id": "g-other"}, {"id": "g-fw"}]

    def test_a_controller_manages_the_instance_instead_of_the_role_group(self) -> None:
        plan = device_plan(controller={"id": "controller-1"})

        (device,) = plan.of(NodeSpec)
        assert device.data["member_of_groups"] == []
        (edge,) = plan.of(EdgesSpec)
        assert (edge.node, edge.relation, edge.peers) == (
            "controller-1",
            "managed_devices",
            (NodeRef("device:c005-DC10-FW1-prod"),),
        )

    @pytest.mark.asyncio
    async def test_the_existing_devices_are_read_by_kind(self) -> None:
        world = CustomerWorld()
        world.seed("DcimVirtualDevice", name="v1", member_of_groups=[], primary_address=None)

        from generators.protocols import DcimVirtualDevice

        reads = await read_device_state(world, ["v1", "v2"], {"firewalls"}, kind=DcimVirtualDevice)

        assert sorted(reads.devices) == ["v1"]
        assert world.reads[0][0] == "DcimVirtualDevice"


class TestPlanVirtualHaPair:
    @staticmethod
    def plan(reads: HaReads, role: str = "internal-firewall") -> tuple[TopologyPlan, list[str]]:
        plan = TopologyPlan(root_id=None)
        problems: list[str] = []
        plan_virtual_ha_pair(plan, names=["v2", "v1"], role=role, ha_name="c005-ha", reads=reads, problems=problems)
        return plan, problems

    def test_a_new_firewall_pair_is_a_domain_of_its_two_members_with_a_group_id_and_their_sync_nics(self) -> None:
        plan, problems = self.plan(HaReads(sync_ports={"v1": "eth7", "v2": "eth7"}, group_pool_id="pool-1"))

        assert problems == []
        domain = next(spec for spec in plan.of(NodeSpec) if spec.kind == "ManagedFirewallHA")
        assert domain.data["capabilities"] == [Hfid("DcimVirtualDevice", ("v1",)), Hfid("DcimVirtualDevice", ("v2",))]
        assert domain.create_only == {"group_id": {"from_pool": {"id": "pool-1"}}}
        assert (domain.ownership, domain.sequential) == ("owned", True)
        ports = [spec for spec in plan.of(NodeSpec) if spec.kind == "DcimVirtualInterface"]
        assert [(spec.hfid, spec.ownership, spec.existing, spec.data["status"]) for spec in ports] == [
            (Hfid("DcimVirtualInterface", ("v1", "eth7")), "template", True, "active"),
            (Hfid("DcimVirtualInterface", ("v2", "eth7")), "template", True, "active"),
        ]
        assert [
            spec for spec in plan.of(NodeSpec) if spec.kind == "DcimCable"
        ] == []  # no cable between hosted instances

    def test_a_missing_group_id_pool_is_created_once_and_shared(self) -> None:
        plan, _ = self.plan(HaReads(sync_ports={"v1": "eth7", "v2": "eth7"}))

        pool = next(spec for spec in plan.of(NodeSpec) if spec.kind == "CoreNumberPool")
        assert pool.ownership == "shared"

    def test_a_load_balancer_pair_draws_no_group_id(self) -> None:
        plan, _ = self.plan(HaReads(sync_ports={"v1": "eth7", "v2": "eth7"}), role="load-balancer")

        domain = next(spec for spec in plan.of(NodeSpec) if spec.kind == "ManagedLoadbalancerHA")
        assert domain.create_only == {} and not domain.sequential

    def test_a_domain_that_exists_stays_in_the_runs_group_and_is_not_rewritten(self) -> None:
        plan, _ = self.plan(HaReads(domains={"c005-ha": HaDomain("ha-1", 7)}, sync_ports={"v1": "eth7", "v2": "eth7"}))

        assert plan.of(TrackSpec) == [TrackSpec(ids=("ha-1",))]
        assert not [spec for spec in plan.of(NodeSpec) if spec.kind == "ManagedFirewallHA"]

    def test_a_member_without_a_sync_nic_is_a_problem_for_the_caller(self) -> None:
        _, problems = self.plan(HaReads(sync_ports={"v1": "eth7"}, group_pool_id="pool-1"))

        assert problems == ["[v2] No HA sync interface found (expected role=ha)"]

    def test_the_locks_are_the_domains_and_for_a_firewall_the_group_id_pool(self) -> None:
        assert virtual_ha_lock_keys("c005-ha", role="internal-firewall") == [
            "ha-domain-c005-ha",
            "firewall-ha-group-pool",
        ]
        assert virtual_ha_lock_keys("c005-ha", role="load-balancer") == ["ha-domain-c005-ha"]

    @pytest.mark.asyncio
    async def test_the_reads_find_the_domain_and_the_first_sync_nic_of_each_member(self) -> None:
        world = CustomerWorld()
        world.seed("ManagedFirewallHA", name="c005-ha", group_id=12)
        for device in ("v1", "v2"):
            world.seed("DcimVirtualDevice", id=f"id-{device}", name=device)
            world.seed("DcimVirtualInterface", name="eth9", role="ha", device={"id": f"id-{device}"})
            world.seed("DcimVirtualInterface", name="eth7", role="ha", device={"id": f"id-{device}"})
            world.seed("DcimVirtualInterface", name="eth1", role="uplink", device={"id": f"id-{device}"})
        world.seed("CoreNumberPool", name="Firewall-HA-Group-ID")

        reads = await read_virtual_ha_reads(
            world, ha_name="c005-ha", role="internal-firewall", device_names=["v1", "v2"]
        )

        assert reads.domains["c005-ha"].group_id == 12
        assert reads.sync_ports == {"v1": "eth7", "v2": "eth7"}
        assert reads.group_pool_id == world.named("CoreNumberPool", "Firewall-HA-Group-ID").id
