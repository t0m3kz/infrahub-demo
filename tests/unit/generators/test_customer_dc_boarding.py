"""The firewall contexts of CustomerDeploymentDCExchangeGenerator, end to end on the in-memory Infrahub of
``tests/unit/customer_world.py``: what a customer's boarding leaves in the store, and what a second run leaves alone.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from generators.customers.customer_dc import CustomerDeploymentDCExchangeGenerator
from tests.unit.customer_world import CustomerWorld, border_vrfs, dc_payload, dc_world, generator
from utils.exchange_transit import transit_vlan

SHARED = "shared-DC10-FW1-FW2-prod"
SHARED_NON_PROD = "shared-DC10-FW1-FW2-nonprod"
MEMBERS = ("DC10-FW1", "DC10-FW2")


async def board(world: CustomerWorld, payload: dict[str, Any] | None = None, *, gen: Any | None = None) -> Any:
    """Run the DC customer generator once on ``world``; returns the generator and the border VRF record."""
    gen = gen or generator(world, CustomerDeploymentDCExchangeGenerator)
    with border_vrfs() as vrfs:
        await gen.generate(payload or dc_payload())
    gen.vrfs = vrfs
    return gen


def writes(world: CustomerWorld) -> tuple[int, int, int]:
    """What a run has written so far: saves, allocations and edges."""
    return len(world.saves), len(world.allocations) + len(world.address_pool_allocations), len(world.edge_adds)


def context_of(world: CustomerWorld, name: str = SHARED) -> Any:
    return world.named("ManagedFirewallContext", name)


def legs_of_member(world: CustomerWorld, member: str) -> dict[str, Any]:
    """The sub-interfaces of a member by tag."""
    return {
        node.data["name"].rsplit(".", 1)[1]: node
        for node in world.sub_interfaces()
        if node.data["device"]["id"] == member
    }


class TestFreshSharedBoarding:
    @pytest.mark.asyncio
    async def test_prod_customer_gets_prod_internet_and_management_legs_on_both_members(self) -> None:
        world = dc_world()

        await board(world)

        vlan = context_of(world).data["vlan_id"]
        assert vlan == 3000
        subs = {(node.data["device"]["id"], node.data["name"]) for node in world.sub_interfaces()}
        assert subs == {
            (member, f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")
            for member in MEMBERS
            for ns_type in ("prod", "internet", "management")
        }
        assert {tag for tag in legs_of_member(world, "DC10-FW1")} == {"3000", "3400", "3600"}

    @pytest.mark.asyncio
    async def test_the_context_is_named_after_its_cluster_and_the_tenant_namespace(self) -> None:
        world = dc_world()

        await board(world)

        context = context_of(world)
        assert context.data["deployment"] == {"id": "dc10-id"}
        assert context.data["shared_devices"] == [{"id": member} for member in MEMBERS]
        assert "tenant" not in context.data

    @pytest.mark.asyncio
    async def test_each_leg_rides_on_the_member_uplink_with_its_transit_tag(self) -> None:
        """A leg's parent and tag are its encapsulation: the uplink of its member, the transit VLAN of its namespace."""
        world = dc_world()

        await board(world)

        vlan = context_of(world).data["vlan_id"]
        riding = {
            (node.data["parent"]["id"], node.data["vlan_id"], node.data["name"]) for node in world.sub_interfaces()
        }
        assert riding == {
            (f"up-{member}", transit_vlan(vlan, ns_type), f"Ethernet1/25.{transit_vlan(vlan, ns_type)}")
            for member in MEMBERS
            for ns_type in ("prod", "internet", "management")
        }

    @pytest.mark.asyncio
    async def test_addresses_follow_the_fixed_offsets_per_namespace(self) -> None:
        world = dc_world()

        await board(world)

        by_pool: dict[str, set[str]] = {}
        for prefix in world.nodes("IpamPrefix"):
            if prefix.data.get("identifier"):
                network = ipaddress.ip_network(prefix.data["prefix"])
                by_pool[prefix.data["pool"]] = {str(network[offset]) for offset in (1, 4, 5, 6)}
        stored = {address.split("/")[0] for address in world.addresses}
        assert stored == {host for hosts in by_pool.values() for host in hosts}
        assert by_pool == {
            "FW-Transit-PROD-IPv4": {"100.66.0.1", "100.66.0.4", "100.66.0.5", "100.66.0.6"},
            "FW-Transit-INTERNET-IPv4": {"100.66.32.1", "100.66.32.4", "100.66.32.5", "100.66.32.6"},
            "FW-Transit-MANAGEMENT-IPv4": {"100.66.48.1", "100.66.48.4", "100.66.48.5", "100.66.48.6"},
        }
        prod_legs = {
            node.data["device"]["id"]: world.store[node.data["ip_address"]["id"]].data["address"]
            for node in world.sub_interfaces()
            if node.data["name"].endswith(".3000")
        }
        assert prod_legs == {"DC10-FW1": "100.66.0.5/29", "DC10-FW2": "100.66.0.6/29"}

    @pytest.mark.asyncio
    async def test_one_exchange_per_peer_names_its_gateway_and_the_vrfs_of_its_legs(self) -> None:
        """An exchange names its context and its two VRFs; its legs are derived (the context's legs in those VRFs)."""
        world = dc_world()

        await board(world)

        context = context_of(world)
        exchanges = {node.data["name"]: node.data for node in world.nodes("TopologyRoutedExchange")}
        assert sorted(exchanges) == [f"{SHARED}-PROD-INTERNET", f"{SHARED}-PROD-MANAGEMENT"]
        for peer in ("INTERNET", "MANAGEMENT"):
            exchange = exchanges[f"{SHARED}-PROD-{peer}"]
            assert exchange["gateway"] == {"id": context.id}
            assert (exchange["vrf_a"], exchange["vrf_z"]) == ({"id": "vrf-PROD"}, {"id": f"vrf-{peer}"})
        vlan = context.data["vlan_id"]
        for ns_type, zone in (("prod", "PROD"), ("internet", "INTERNET"), ("management", "MANAGEMENT")):
            sub = legs_of_member(world, "DC10-FW1")[str(transit_vlan(vlan, ns_type))]
            assert sub.data["capabilities"] == [{"id": f"vrf-{zone}"}], ns_type

    @pytest.mark.asyncio
    async def test_the_context_joins_service_contexts_once_its_legs_are_written(self) -> None:
        world = dc_world()

        await board(world)

        context = context_of(world)
        group = world.named("CoreStandardGroup", "service_contexts")
        assert world.edge_adds[-1] == (context.id, "member_of_groups", [group.id])
        assert all(node.data["capabilities"] for node in world.sub_interfaces())

    @pytest.mark.asyncio
    async def test_shared_context_writes_are_all_untracked(self) -> None:
        world = dc_world()

        await board(world)

        assert {kind for kind, *_ in world.saves} == {
            "ManagedFirewallContext",
            "TopologyRoutedExchange",
            "DcimVirtualInterface",
        }
        for kind, _id, _data, kwargs in world.saves:
            assert kwargs == {"allow_upsert": True, "update_group_context": False}, kind
        assert world.address_saves() == [{"allow_upsert": True, "update_group_context": False}] * len(world.addresses)

    @pytest.mark.asyncio
    async def test_writes_run_under_the_per_context_lock_and_link_the_deployment(self) -> None:
        world = dc_world()

        gen = await board(world)

        assert gen.locked_keys == [f"fw-context-{SHARED}"]
        assert world.store["cust-1"].data["serving_firewall_contexts"] == [{"id": context_of(world).id}]
        # The border devices' instances are held while the legs are written, converged after
        assert gen.vrfs.held == [
            {"BL-DC10-FW1": {"PROD", "INTERNET", "MANAGEMENT"}, "BL-DC10-FW2": {"PROD", "INTERNET", "MANAGEMENT"}}
        ]
        assert gen.vrfs.events == ["held", "released", "reconciled"]
        assert gen.vrfs.reconciled == [{"BL-DC10-FW1", "BL-DC10-FW2"}]

    @pytest.mark.asyncio
    async def test_the_legs_are_written_after_the_context_and_the_pools_it_draws_from(self) -> None:
        world = dc_world()

        await board(world)

        kinds = [kind for kind, *_ in world.saves]
        assert kinds.index("ManagedFirewallContext") < kinds.index("DcimVirtualInterface")
        assert kinds.index("TopologyRoutedExchange") < kinds.index("DcimVirtualInterface")


class TestRerunAndSecondCustomer:
    @pytest.mark.asyncio
    async def test_rerun_writes_nothing_and_never_takes_the_lock(self) -> None:
        world = dc_world()
        gen = await board(world)
        first = writes(world), len(gen.locked_keys)
        link = {"serving_firewall_contexts": [{"id": context_of(world).id, "name": SHARED}]}

        await board(world, dc_payload(serving=link), gen=gen)

        assert (writes(world), len(gen.locked_keys)) == first

    @pytest.mark.asyncio
    async def test_second_customer_on_the_complete_context_writes_nothing_but_its_link(self) -> None:
        world = dc_world()
        gen = await board(world, dc_payload(customer_id="cust-1"))
        saved, allocated, edges = writes(world)
        locks = len(gen.locked_keys)

        await board(world, dc_payload(customer_id="cust-2"), gen=gen)

        assert writes(world) == (saved, allocated, edges + 1)
        assert len(gen.locked_keys) == locks
        assert world.edge_adds[-1] == ("cust-2", "serving_firewall_contexts", [context_of(world).id])

    @pytest.mark.asyncio
    async def test_non_prod_customer_gets_its_own_context_and_never_pairs_with_prod(self) -> None:
        world = dc_world()
        await board(world, dc_payload(customer_id="cust-1", environment="p"))

        await board(world, dc_payload(customer_id="cust-2", environment="d"))

        assert {node.data["name"] for node in world.nodes("ManagedFirewallContext")} == {SHARED, SHARED_NON_PROD}
        exchanges = {node.data["name"]: node.data for node in world.nodes("TopologyRoutedExchange")}
        assert sorted(exchanges) == [
            f"{SHARED_NON_PROD}-NONPROD-INTERNET",
            f"{SHARED_NON_PROD}-NONPROD-MANAGEMENT",
            f"{SHARED}-PROD-INTERNET",
            f"{SHARED}-PROD-MANAGEMENT",
        ]
        for name, exchange in exchanges.items():
            tenant = "PROD" if "-PROD-" in name else "NONPROD"
            assert exchange["vrf_a"] == {"id": f"vrf-{tenant}"}
            assert exchange["gateway"] == {"id": context_of(world, SHARED if tenant == "PROD" else SHARED_NON_PROD).id}
        prod_vlan = context_of(world).data["vlan_id"]
        non_prod_vlan = context_of(world, SHARED_NON_PROD).data["vlan_id"]
        assert prod_vlan != non_prod_vlan
        # Each context takes its own INTERNET and MANAGEMENT /29: nothing is shared across the two environments.
        identifiers = [prefix.data["identifier"] for prefix in world.nodes("IpamPrefix") if "identifier" in prefix.data]
        assert len([i for i in identifiers if i.endswith("-INTERNET-transit")]) == 2
        assert len([i for i in identifiers if i.endswith("-MANAGEMENT-transit")]) == 2

    @pytest.mark.asyncio
    async def test_non_prod_customer_alone_uses_non_prod_and_the_peers_only(self) -> None:
        world = dc_world()

        await board(world, dc_payload(environment="t"))

        assert sorted(node.data["name"] for node in world.nodes("TopologyRoutedExchange")) == [
            f"{SHARED_NON_PROD}-NONPROD-INTERNET",
            f"{SHARED_NON_PROD}-NONPROD-MANAGEMENT",
        ]
        assert {node.data["name"].rsplit(".", 1)[1] for node in world.sub_interfaces()} == {"3200", "3400", "3600"}


class TestHealing:
    @pytest.mark.asyncio
    async def test_partial_failure_is_healed_by_the_next_run(self) -> None:
        world = dc_world()
        world.fail_saves_of = {"DcimVirtualInterface"}
        with pytest.raises(RuntimeError, match="DcimVirtualInterface save failed"):
            await board(world)
        world.fail_saves_of = set()

        gen = await board(world)

        assert len(world.sub_interfaces()) == 6
        assert all(node.data["capabilities"] for node in world.sub_interfaces())
        healed = writes(world)
        link = {"serving_firewall_contexts": [{"id": context_of(world).id, "name": SHARED}]}

        await board(world, dc_payload(serving=link), gen=gen)  # back to the zero-write fast path

        assert writes(world) == healed

    @pytest.mark.asyncio
    async def test_a_leg_out_of_its_vrf_alone_is_healed(self) -> None:
        """A leg that lost its VRF edge makes the context incomplete: the next run puts it back, edge by edge."""
        world = dc_world()
        await board(world)
        sub = next(
            node
            for node in world.sub_interfaces()
            if node.data["device"]["id"] == "DC10-FW2" and node.data["name"].endswith(".3000")
        )
        sub.data["capabilities"] = []
        world.store["vrf-PROD"].data["capabilities"] = [
            peer for peer in world.store["vrf-PROD"].data["capabilities"] if peer["id"] != sub.id
        ]
        edges = len(world.edge_adds)

        await board(world)

        assert (sub.id, "capabilities", ["vrf-PROD"]) in world.edge_adds[edges:]
        assert sub.data["capabilities"] == [{"id": "vrf-PROD"}]

    @pytest.mark.asyncio
    async def test_a_leg_in_another_zones_vrf_is_a_defect_that_fails_before_any_write(self) -> None:
        world = dc_world()
        await board(world)
        sub = next(node for node in world.sub_interfaces() if node.data["name"].endswith(".3400"))
        sub.data["capabilities"] = [{"id": "vrf-MANAGEMENT"}]
        world.store["vrf-INTERNET"].data["capabilities"] = []
        world.store["vrf-MANAGEMENT"].data["capabilities"].append({"id": sub.id})
        before = writes(world)

        with pytest.raises(ValueError, match="is in security zone"):
            await board(world)

        assert writes(world) == before
