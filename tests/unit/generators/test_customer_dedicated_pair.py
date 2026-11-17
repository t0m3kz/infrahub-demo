"""A customer's dedicated pair of virtual firewalls or load balancers and the context on it, end to end on the in-memory
Infrahub of ``tests/unit/customer_world.py``: what it writes, that it is the customer's own (tracked), what a re-run leaves
alone and what it refuses to write.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.customers.customer_dc import CustomerDeploymentDCExchangeGenerator
from generators.customers.dedicated_pair import ensure_dedicated_pair
from generators.model.errors import GeneratorError
from tests.unit.customer_world import CustomerWorld, dc_payload, dc_world, generator
from tests.unit.generators.test_customer_dc_boarding import board, writes

FW_HA = "c005-DC10-FW1-DC10-FW2-ha-prod"
FW_CONTEXT = "c005-DC10-FW1-DC10-FW2-prod"
LB_HA = "c005-DC10-LB1-DC10-LB2-ha-prod"
LB_CONTEXT = "c005-DC10-LB1-DC10-LB2-prod"
TRACKED = {"allow_upsert": True}


def dedicated_world(**kwargs: Any) -> CustomerWorld:
    world = dc_world(loadbalancers=True, **kwargs)
    world.add_dedicated_prerequisites()
    return world


def _pin_saves(world: Any) -> list[Any]:
    """The saves that pin a NIC to its host port: a NIC (not a leg) written with a ``parent``."""
    return [
        save
        for save in world.saves_of("DcimVirtualInterface")
        if "parent" in save[2] and save[2].get("role") != "service" and "vlan_id" not in save[2]
    ]


class TestDedicatedFirewallPair:
    @pytest.mark.asyncio
    async def test_the_instances_are_hosted_on_the_pair_from_the_customer_template(self) -> None:
        world = dedicated_world()

        await board(world, dc_payload(dedicated_firewall=True))

        devices = {node.data["name"]: node for node in world.nodes("DcimVirtualDevice")}
        assert sorted(devices) == ["c005-DC10-FW1-prod", "c005-DC10-FW2-prod"]
        template = world.named("TemplateDcimVirtualDevice", "CloudGuard_EDGE_CUSTOMER_M")
        for host, name in (("DC10-FW1", "c005-DC10-FW1-prod"), ("DC10-FW2", "c005-DC10-FW2-prod")):
            data = devices[name].data
            assert data["hosting_device"] == {"id": host}
            assert data["object_template"] == {"id": template.id}
            assert (data["role"], data["deployment"], data["status"]) == (
                "internal-firewall",
                {"id": "dc10-id"},
                "active",
            )
            assert data["device_type"] == template.data["device_type"] and data["platform"] == template.data["platform"]
            # a management address of the DC's pool, keyed by the device name
            assert world.address_pool_allocations[
                f"{world.named('CoreIPAddressPool', 'dc10-management-pool').id}/{name}"
            ]

    @pytest.mark.asyncio
    async def test_the_instances_join_the_firewalls_group_and_the_pair_is_one_ha_domain_with_a_group_id(self) -> None:
        world = dedicated_world()

        await board(world, dc_payload(dedicated_firewall=True))

        ha = world.named("ManagedFirewallHA", FW_HA)
        devices = {node.data["name"]: node.id for node in world.nodes("DcimVirtualDevice")}
        assert ha.data["capabilities"] == [{"id": devices[name]} for name in sorted(devices)]
        assert ha.data["member_of_groups"] == [{"id": world.named("CoreStandardGroup", "ha_domains").id}]
        assert ha.data["group_id"] == {"from_pool": {"id": world.named("CoreNumberPool", "Firewall-HA-Group-ID").id}}
        group = world.named("CoreStandardGroup", "firewalls")
        for node in world.nodes("DcimVirtualDevice"):
            assert node.data["member_of_groups"] == [{"id": group.id}]

    @pytest.mark.asyncio
    async def test_each_nic_is_pinned_to_a_port_of_the_host_of_its_device(self) -> None:
        world = dedicated_world()

        await board(world, dc_payload(dedicated_firewall=True))

        pins = {
            f"{world.store[node.data['device']['id']].data['name']}:{node.data['name']}": node.data["parent"]["id"]
            for node in world.nodes("DcimVirtualInterface")
            if node.data["role"] in ("management", "uplink", "ha") and node.data.get("parent", {}).get("id")
        }
        assert pins == {
            "c005-DC10-FW1-prod:eth0": "DC10-FW1-MGMT",
            "c005-DC10-FW1-prod:eth1": "up-DC10-FW1",
            "c005-DC10-FW1-prod:eth7": "DC10-FW1-sync",
            "c005-DC10-FW2-prod:eth0": "DC10-FW2-MGMT",
            "c005-DC10-FW2-prod:eth1": "up-DC10-FW2",
            "c005-DC10-FW2-prod:eth7": "DC10-FW2-sync",
        }

    @pytest.mark.asyncio
    async def test_the_context_sits_on_the_dedicated_pair_with_the_tenant_and_its_legs_on_the_nic(self) -> None:
        world = dedicated_world()

        await board(world, dc_payload(dedicated_firewall=True))

        context = world.named("ManagedFirewallContext", FW_CONTEXT)
        assert context.data["tenant"] == {"id": "cust-1"}
        assert context.data["shared_devices"] == [
            {"id": node.id} for node in sorted(world.nodes("DcimVirtualDevice"), key=lambda n: n.data["name"])
        ]
        legs = {(node.data["device"]["id"], node.data["name"]) for node in world.sub_interfaces()}
        nics = {
            node.data["device"]["id"]: node
            for node in world.nodes("DcimVirtualInterface")
            if node.data.get("name") == "eth1"
        }
        assert legs == {
            (device.id, f"eth1.{tag}") for device in world.nodes("DcimVirtualDevice") for tag in (3000, 3400, 3600)
        }
        riding = {node.data["parent"]["id"] for node in world.sub_interfaces()}
        assert riding == {nic.id for nic in nics.values()}

    @pytest.mark.asyncio
    async def test_every_write_of_the_run_is_tracked(self) -> None:
        """The customer's own pair, context and legs belong to its run; only the shared objects they point at do not."""
        world = dedicated_world()

        await board(world, dc_payload(dedicated_firewall=True))

        by_kind: dict[str, set[bool]] = {}
        for kind, _id, _data, kwargs in world.saves:
            by_kind.setdefault(kind, set()).add(kwargs.get("update_group_context") is False)
        untracked = {kind for kind, flags in by_kind.items() if flags == {True}}
        assert (
            untracked == {"CoreStandardGroup", "DcimVirtualInterface"} & untracked
        )  # the role group, the template NICs' sync port
        tracked = {
            "DcimVirtualDevice",
            "ManagedFirewallHA",
            "ManagedFirewallContext",
            "TopologyRoutedExchange",
        }
        assert all(by_kind[kind] == {False} for kind in tracked)
        assert world.address_saves() == [TRACKED] * len(world.addresses)
        sub_saves = [save for save in world.saves_of("DcimVirtualInterface") if save[2].get("role") == "service"]
        assert sub_saves and all(kwargs == TRACKED for *_, kwargs in sub_saves)
        snat_saves = [save for save in world.saves_of("ManagedFirewallContext") if "snat_address" in save[2]]
        assert snat_saves and all(kwargs == TRACKED for *_, kwargs in snat_saves)

    @pytest.mark.asyncio
    async def test_a_rerun_creates_nothing_again_and_resaves_what_the_run_owns(self) -> None:
        world = dedicated_world()
        gen = await board(world, dc_payload(dedicated_firewall=True))
        devices = {node.id for node in world.nodes("DcimVirtualDevice")}
        objects = {
            kind: len(world.nodes(kind))
            for kind in (
                "DcimVirtualDevice",
                "ManagedFirewallHA",
                "TopologyRoutedExchange",
                "ManagedFirewallContext",
            )
        }
        saved = len(world.saves)
        pin_saves = len(_pin_saves(world))
        locks = len(gen.locked_keys)

        await board(world, dc_payload(dedicated_firewall=True), gen=gen)

        assert {node.id for node in world.nodes("DcimVirtualDevice")} == devices
        assert {kind: len(world.nodes(kind)) for kind in objects} == objects
        # A tracked context is re-saved with every leg (delete_unused_nodes reclaims what a run does not save again)
        assert len(world.saves) > saved
        assert gen.locked_keys[locks:] == ["firewall-ha-group-pool", f"ha-domain-{FW_HA}", f"fw-context-{FW_CONTEXT}"]
        # the pins that are right are kept, not re-saved
        assert pin_saves == 6 and len(_pin_saves(world)) == pin_saves

    @pytest.mark.asyncio
    async def test_the_pair_takes_the_ha_domain_and_group_pool_locks_before_the_context_lock(self) -> None:
        world = dedicated_world()

        gen = await board(world, dc_payload(dedicated_firewall=True))

        assert gen.locked_keys == ["firewall-ha-group-pool", f"ha-domain-{FW_HA}", f"fw-context-{FW_CONTEXT}"]

    @pytest.mark.asyncio
    async def test_a_controller_that_manages_the_role_takes_the_instances_instead_of_the_group(self) -> None:
        world = dedicated_world()
        template = world.named("TemplateDcimVirtualDevice", "CloudGuard_EDGE_CUSTOMER_M")
        controller = world.seed("ManagedController", name="panorama", managed_devices=[])
        payload = dc_payload(dedicated_firewall=True)
        payload["TopologyCustomerDC"][0]["parent"]["security_manager_controllers"] = [
            {
                "id": controller.id,
                "controller_type": "security_manager",
                "platform": {"id": template.data["platform"]["id"], "name": "checkpoint_gaia"},
            }
        ]

        await board(world, payload)

        devices = sorted(node.id for node in world.nodes("DcimVirtualDevice"))
        assert sorted(peer["id"] for peer in controller.data["managed_devices"]) == devices
        assert all(node.data["member_of_groups"] == [] for node in world.nodes("DcimVirtualDevice"))


class TestDedicatedLoadbalancerPair:
    @pytest.mark.asyncio
    async def test_the_pair_uses_the_lb_template_and_ha_kind_without_a_group_id(self) -> None:
        world = dedicated_world()

        await board(
            world, dc_payload(firewalls=(), loadbalancers=("DC10-LB1", "DC10-LB2"), dedicated_loadbalancer=True)
        )

        ha = world.named("ManagedLoadbalancerHA", LB_HA)
        assert "group_id" not in ha.data
        assert {node.data["object_template"]["id"] for node in world.nodes("DcimVirtualDevice")} == {
            world.named("TemplateDcimVirtualDevice", "BIG-IP-VE_LOAD_BALANCER_CUSTOMER_M").id
        }
        context = world.named("ManagedLoadbalancerContext", LB_CONTEXT)
        assert context.data["tenant"] == {"id": "cust-1"}
        assert context.data["vip_prefix"]
        assert world.tracked("ManagedLoadbalancerContext")

    @pytest.mark.asyncio
    async def test_the_flags_are_independent(self) -> None:
        world = dedicated_world()

        await board(world, dc_payload(loadbalancers=("DC10-LB1", "DC10-LB2"), dedicated_loadbalancer=True))

        names = {node.data["name"] for node in world.nodes("ManagedFirewallContext")}
        assert names == {"shared-DC10-FW1-FW2-prod"}
        assert {node.data["name"] for node in world.nodes("ManagedLoadbalancerContext")} == {LB_CONTEXT}


class TestADedicatedPairThatCannotBeBuilt:
    """A dedicated request is never moved onto the shared context: the run fails and writes nothing."""

    @staticmethod
    def untouched(world: CustomerWorld) -> None:
        assert writes(world) == (0, 0, 0)
        assert world.nodes("DcimVirtualDevice") == []

    @pytest.mark.asyncio
    async def test_a_dc_without_a_design_is_an_error(self) -> None:
        world = dedicated_world()
        payload = dc_payload(dedicated_firewall=True)
        payload["TopologyCustomerDC"][0]["parent"]["design"] = {}

        with pytest.raises(GeneratorError, match="no design set"):
            await board(world, payload)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_a_platform_without_a_virtual_template_mapping_is_an_error(self) -> None:
        world = dedicated_world()
        payload = dc_payload(dedicated_firewall=True)
        for device in payload["TopologyCustomerDC"][0]["parent"]["firewall_devices"]:
            device["platform"] = {"name": "unmapped"}

        with pytest.raises(GeneratorError, match="No virtual template mapping for platform=unmapped"):
            await board(world, payload)

        self.untouched(world)

    @pytest.mark.asyncio
    async def test_a_missing_customer_template_is_an_error(self) -> None:
        world = dedicated_world()
        world.store.pop(world.named("TemplateDcimVirtualDevice", "CloudGuard_EDGE_CUSTOMER_M").id)

        with pytest.raises(GeneratorError, match="No virtual template 'CloudGuard_EDGE_CUSTOMER_M'"):
            await board(world, dc_payload(dedicated_firewall=True))

        self.untouched(world)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hosts", [1, 3])
    async def test_not_exactly_two_physical_peers_is_an_error(self, hosts: int) -> None:
        world = dedicated_world()
        gen = generator(world, CustomerDeploymentDCExchangeGenerator)
        devices = [
            {"id": f"h{index}", "name": f"h{index}", "platform": {"name": "checkpoint_gaia"}} for index in range(hosts)
        ]

        with pytest.raises(GeneratorError, match=f"expected 2 physical internal-firewall peers.*found {hosts}"):
            await ensure_dedicated_pair(
                gen,
                role="internal-firewall",
                ha_kind="ManagedFirewallHA",
                physical_devices=devices,
                parent_id="dc10-id",
                parent_name="DC10",
                dc_design="M",
                name_parts=("c005", "prod"),
                controllers=[],
            )

        self.untouched(world)
