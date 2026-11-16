"""The plans the segment generators build, as data (generators/segments/segment_addressing.py, segment_policy.py,
segment_vlan.py), and the reconciler's allocation of a prefix (``PrefixAllocationSpec``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.model.errors import GeneratorError
from generators.plan import (
    Hfid,
    NodeRef,
    NodeSpec,
    PoolGrowth,
    PrefixAllocationSpec,
    RetireSpec,
    TopologyPlan,
)
from generators.plan_graph import lookup_hfid_nodes
from generators.protocols import ManagedSegmentVlan, SecurityPolicy
from generators.segments.segment_addressing import (
    AddressingRefused,
    check_allocated_length,
    plan_segment_addressing,
    segment_lock_keys,
    segment_pool,
)
from generators.segments.segment_policy import segment_policy_spec
from generators.segments.segment_vlan import SegmentRows, plan_segment_rows
from tests.unit.segment_customer_pool_helpers import Client, customer_pool, footprint, make_gen, segment_data

# plan_segment_addressing


def tenant_segment(**kwargs: Any) -> dict[str, Any]:
    """A segment of an office: no footprint with a design, so the tenant pool addresses it."""
    return segment_data(footprints=[footprint("TopologyCustomerOffice", "C005-OFFICE")], **kwargs)


class TestAddressingPlan:
    def test_a_segment_of_an_office_draws_the_pools_default_length_from_the_tenant_pool(self) -> None:
        segment = tenant_segment()
        pool = segment_pool(segment)

        plan = plan_segment_addressing(segment, pool, 24)

        assert plan.specs == [
            PrefixAllocationSpec(
                key="prefix",
                identifier="c005-web-p",
                pool_name="Tenant-PROD-IPv4",
                prefix_length=24,
                role="customer",
                member_type="address",
                status="active",
                description="c005-web-p",
                label="c005-web-p",
                address_offsets=(1,),
                address_description="c005-web-p anycast gateway",
            ),
            NodeSpec(
                "segment",
                "ManagedVxlanSegment",
                {"id": "seg-1", "gateway": {"id": NodeRef("prefix#1")}},
                ownership="shared",
            ),
        ]
        assert segment_lock_keys(segment, pool) == ("segment-addressing-seg-1",)

    def test_a_segment_with_a_design_draws_from_its_customers_pool_which_grows_by_a_block(self) -> None:
        segment = segment_data(host_count=100)
        pool = segment_pool(segment)

        plan = plan_segment_addressing(segment, pool, None)

        (prefix, _) = plan.specs
        assert isinstance(prefix, PrefixAllocationSpec)
        assert (prefix.pool_name, prefix.prefix_length) == ("c005-p-dc-ipv4", 25)
        assert prefix.grow == PoolGrowth("Tenant-PROD-IPv4", 23)
        assert segment_lock_keys(segment, pool) == ("segment-addressing-seg-1", "customer-pool-c005-p-dc-ipv4")

    def test_an_ipv6_segment_ignores_the_host_count(self) -> None:
        segment = segment_data(host_count=100, ip_version="ipv6")

        plan = plan_segment_addressing(segment, segment_pool(segment), None)

        (prefix, _) = plan.specs
        assert isinstance(prefix, PrefixAllocationSpec)
        assert (prefix.pool_name, prefix.prefix_length) == ("c005-p-dc-ipv6", 64)

    def test_a_footprint_without_a_design_is_refused(self) -> None:
        segment = segment_data(footprints=[{"typename": "TopologyCustomerDC", "name": "FP"}])

        with pytest.raises(AddressingRefused, match="footprint FP has no design to size c005-p-dc-ipv4"):
            plan_segment_addressing(segment, segment_pool(segment), None)

    def test_a_segment_larger_than_the_designs_block_is_refused(self) -> None:
        segment = segment_data(host_count=900)

        with pytest.raises(AddressingRefused, match=r"needs a /22, larger than the /23 address block"):
            plan_segment_addressing(segment, segment_pool(segment), None)

    def test_an_allocated_prefix_of_another_length_than_the_host_count_asks_for_is_refused(self) -> None:
        segment = segment_data(host_count=100, gateway={"ip_prefix": {"prefix": "10.1.0.0/24"}})

        with pytest.raises(AddressingRefused, match=r"its prefix 10.1.0.0/24 is not the /25"):
            check_allocated_length(segment, 100)

        check_allocated_length(segment, None)  # no host_count: not judged


# the policy


class TestPolicyPlan:
    def test_the_policy_is_found_by_its_name_and_created_with_its_contract(self) -> None:
        spec = segment_policy_spec({"id": "seg-1", "name": "c900-web-p"}, "seg-c900-web-p-egress")

        assert spec == NodeSpec(
            "policy",
            SecurityPolicy,
            {"name": "seg-c900-web-p-egress"},
            hfid=Hfid("SecurityPolicy", ("seg-c900-web-p-egress",)),
            ownership="shared",
            create_only={
                "description": "Egress contract of segment c900-web-p",
                "default_action": "deny",
                "enabled": True,
                "segment": {"id": "seg-1"},
            },
        )
        assert not spec.tracked


# plan_segment_rows


def rows(**kwargs: Any) -> SegmentRows:
    names = {"a": "sw-a", "b": "sw-b", "c": "sw-c"}
    return SegmentRows(
        names=names,
        new=kwargs.get("new", {}),
        kept=kwargs.get("kept", {}),
        stale=kwargs.get("stale", {}),
    )


def vlan_row(device: str, vlan: int, deployment: str = "sd-1", **kwargs: Any) -> NodeSpec:
    return NodeSpec(
        f"row-{device}",
        ManagedSegmentVlan,
        {
            "segment_deployment": {"id": deployment},
            "device": Hfid("DcimPhysicalDevice", (f"sw-{device}",)),
            "vlan_id": vlan,
        },
        hfid=Hfid("ManagedSegmentVlan", (f"sw-{device}", str(vlan))),
        ownership="owned",
        **kwargs,
    )


class TestRowsPlan:
    def test_a_new_row_is_owned_and_named_by_its_device_and_vlan_id(self) -> None:
        plan = plan_segment_rows(rows(new={"a": "sd-1"}), {"a": 100})

        assert plan.specs == [vlan_row("a", 100)]
        (spec,) = plan.specs
        assert isinstance(spec, NodeSpec) and spec.tracked

    def test_a_row_that_stays_is_written_again_so_the_run_tracks_it(self) -> None:
        plan = plan_segment_rows(rows(kept={"b": ("sd-2", 101)}), {})

        assert plan.specs == [vlan_row("b", 101, "sd-2")]

    def test_a_new_row_replaces_the_stale_row_of_its_device(self) -> None:
        plan = plan_segment_rows(rows(new={"a": "sd-2"}, stale={"a": ("row-old", 100)}), {"a": 101})

        assert plan.specs == [vlan_row("a", 101, "sd-2", replaces=(Hfid("ManagedSegmentVlan", ("sw-a", "100")),))]

    def test_a_new_row_with_the_vlan_id_of_the_stale_one_is_the_same_row_moved(self) -> None:
        """The HFID is the same: the upsert finds the row and moves it under the new deployment, nothing is deleted."""
        plan = plan_segment_rows(rows(new={"a": "sd-2"}, stale={"a": ("row-old", 100)}), {"a": 100})

        assert plan.specs == [vlan_row("a", 100, "sd-2")]

    def test_the_stale_rows_of_devices_with_no_new_row_are_retired(self) -> None:
        plan = plan_segment_rows(rows(new={"a": "sd-1"}, stale={"b": ("row-b", 102), "c": ("row-c", 103)}), {"a": 100})

        assert plan.specs[-1] == RetireSpec(
            (Hfid("ManagedSegmentVlan", ("sw-b", "102")), Hfid("ManagedSegmentVlan", ("sw-c", "103")))
        )
        assert len(plan.specs) == 2

    def test_nothing_to_write_is_an_empty_plan(self) -> None:
        assert plan_segment_rows(rows(), {}).specs == []


# the allocation of a prefix by the reconciler


def prefix_plan(
    *, pool: str = "Tenant-PROD-IPv4", prefix_length: int = 24, grow: PoolGrowth | None = None
) -> TopologyPlan:
    """A segment's prefix, its gateway and the segment's edge, as ``plan_segment_addressing`` writes them."""
    spec = PrefixAllocationSpec(
        key="prefix",
        identifier="c005-web-p",
        pool_name=pool,
        prefix_length=prefix_length,
        role="customer",
        member_type="address",
        status="active",
        description="c005-web-p",
        label="c005-web-p",
        grow=grow,
        address_offsets=(1,),
        address_description="c005-web-p anycast gateway",
    )
    segment = NodeSpec("segment", "ManagedVxlanSegment", {"id": "seg-1", "gateway": {"id": NodeRef("prefix#1")}})
    return TopologyPlan(None, [spec, segment])


class TestPrefixAllocation:
    def test_the_prefix_is_drawn_from_the_tenant_pool_and_the_gateway_follows_it(self) -> None:
        client = Client()
        gen = make_gen(client)

        result = asyncio.run(gen.apply_plan(prefix_plan()))

        (allocation,) = client.allocations
        assert allocation["identifier"] == "c005-web-p" and allocation["prefix_length"] == 24
        assert allocation["data"] == {"role": "customer", "status": "active", "description": "c005-web-p"}
        assert result.nodes["prefix#1"].address.value == "10.176.0.1/24"
        assert client.created == [("ManagedVxlanSegment", {"id": "seg-1", "gateway": {"id": "gw-1"}})]

    def test_the_reconciler_takes_no_lock_the_caller_of_apply_plan_holds_them(self) -> None:
        client = Client()
        gen = make_gen(client)

        asyncio.run(gen.apply_plan(prefix_plan()))

        assert gen.locks == []

    def test_a_second_apply_allocates_nothing_new(self) -> None:
        client = Client()
        gen = make_gen(client)
        plan = prefix_plan()

        first = asyncio.run(gen.apply_plan(plan))
        second = asyncio.run(gen.apply_plan(plan))

        assert len(client.known) == 1
        assert first.nodes["prefix"] is second.nodes["prefix"]

    def test_a_customer_pool_that_is_full_grows_by_one_block_of_the_global_pool(self) -> None:
        client = Client(pools={"c005-p-dc-ipv4": customer_pool("c005-p-dc-ipv4", blocks=1)})
        client.customer_room["c005-p-dc-ipv4"] = 0
        client.refill_on_edge = {"c005-p-dc-ipv4": 5}
        gen = make_gen(client)

        asyncio.run(
            gen.apply_plan(
                prefix_plan(pool="c005-p-dc-ipv4", grow=PoolGrowth("Tenant-PROD-IPv4", 23), prefix_length=25)
            )
        )

        block = next(a for a in client.allocations if a["member_type"] == "prefix")
        assert block["identifier"] == "c005-p-dc-ipv4-2" and block["prefix_length"] == 23
        assert len(client.edges) == 1  # the new block joins the pool's resources

    def test_a_missing_customer_pool_is_created_from_a_block(self) -> None:
        client = Client()
        gen = make_gen(client)

        asyncio.run(
            gen.apply_plan(
                prefix_plan(pool="c005-p-dc-ipv4", grow=PoolGrowth("Tenant-PROD-IPv4", 23), prefix_length=25)
            )
        )

        (sliced,) = gen.sliced
        assert (sliced["pool_name"], sliced["parent_pool_name"], sliced["prefix_length"]) == (
            "c005-p-dc-ipv4",
            "Tenant-PROD-IPv4",
            23,
        )

    def test_another_length_for_an_allocated_identifier_fails_naming_the_remedy(self) -> None:
        client = Client()
        gen = make_gen(client)
        asyncio.run(gen.apply_plan(prefix_plan()))

        with pytest.raises(GeneratorError, match="cannot change in place"):
            asyncio.run(gen.apply_plan(prefix_plan(prefix_length=26)))

    def test_without_a_label_the_refusal_of_another_length_is_the_servers_own(self) -> None:
        client = Client()
        gen = make_gen(client)
        first = TopologyPlan(
            None,
            [
                PrefixAllocationSpec(
                    key="p", identifier="x", pool_name="Tenant-PROD-IPv4", prefix_length=24, role="technical"
                )
            ],
        )
        longer = TopologyPlan(
            None,
            [
                PrefixAllocationSpec(
                    key="p", identifier="x", pool_name="Tenant-PROD-IPv4", prefix_length=26, role="technical"
                )
            ],
        )
        asyncio.run(gen.apply_plan(first))

        with pytest.raises(GraphQLError, match="cannot be changed"):
            asyncio.run(gen.apply_plan(longer))

    def test_an_exhausted_tenant_pool_fails_and_no_gateway_is_written(self) -> None:
        client = Client(tenant_blocks=0)
        gen = make_gen(client)

        with pytest.raises(GraphQLError):
            asyncio.run(gen.apply_plan(prefix_plan()))

        assert client.created == []

    def test_a_spec_needs_an_identifier_one_pool_and_a_named_pool_to_grow(self) -> None:
        with pytest.raises(ValueError, match="deterministic identifier"):
            PrefixAllocationSpec(key="p", identifier=" ", pool_name="P", prefix_length=24, role="customer")
        with pytest.raises(ValueError, match="exactly one of a parent pool and a pool name"):
            PrefixAllocationSpec(key="p", identifier="x", prefix_length=24, role="customer")
        with pytest.raises(ValueError, match="exactly one of a parent pool and a pool name"):
            PrefixAllocationSpec(key="p", identifier="x", parent="id", pool_name="P", prefix_length=24, role="customer")
        with pytest.raises(ValueError, match="found by name"):
            PrefixAllocationSpec(
                key="p", identifier="x", parent="id", prefix_length=24, role="customer", grow=PoolGrowth("G", 23)
            )

    def test_the_gateway_is_written_after_the_prefix_and_untracked(self) -> None:
        client = Client()
        gen = make_gen(client)

        asyncio.run(gen.apply_plan(TopologyPlan(None, list(reversed(prefix_plan().specs)))))

        assert len(client.allocations) == 1 and len(client.created) == 1


class TestHfidLookup:
    def test_a_number_part_of_an_hfid_is_filtered_as_an_integer(self) -> None:
        """The server wants strings in an HFID array and integers in the filter of a Number attribute."""
        seen: list[dict[str, Any]] = []

        class Recorder:
            async def filters(self, **kwargs: Any) -> list[Any]:
                seen.append(kwargs)
                return []

        wanted = [Hfid("ManagedSegmentVlan", ("sw-a", "100"))]

        assert asyncio.run(lookup_hfid_nodes(Recorder(), wanted, ["member_of_groups"])) == {}
        assert seen == [
            {
                "kind": "ManagedSegmentVlan",
                "device__name__values": ["sw-a"],
                "vlan_id__values": [100],
                "include": ["device", "member_of_groups"],
            }
        ]
