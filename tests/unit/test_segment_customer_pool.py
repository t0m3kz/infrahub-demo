"""The sizing of a segment's prefix and its customer's pool (generators/topology/segment_addressing.py).

A segment with a ``host_count`` gets the smallest prefix that holds it (``segment_prefix_length``, the design's
``segment_prefix_length`` without one). Its prefix is drawn from the customer's pool, created when the footprint is
boarded (and repaired here when missing) from a block of the footprint design's ``address_prefix_length`` out of the
environment's tenant pool, and grown by one block when it cannot satisfy a request. The server-side behaviour (the pool, the resource edge, the allocation) is mocked; what is held here
is which calls are made, with which identifiers and lengths.
"""

from __future__ import annotations

import asyncio

import pytest
from infrahub_sdk.exceptions import GraphQLError

from generators.logger import GeneratorError
from tests.unit.segment_customer_pool_helpers import (
    Client,
    customer_pool,
    footprint,
    make_gen,
    segment_data,
)
from utils.limits import SEGMENT_HOST_COUNT_MAX
from utils.segment_addressing import addressing_footprint, customer_pool_name, segment_prefix_length

# ---------------------------------------------------------------------------
# The sizing function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host_count", "length"),
    [
        (1, 29),
        (5, 29),
        (6, 28),
        (13, 28),
        (14, 27),
        (29, 27),
        (30, 26),
        (61, 26),
        (62, 25),
        (125, 25),
        (126, 24),
        (253, 24),
        (254, 23),
        (509, 23),
        (510, 22),
        (SEGMENT_HOST_COUNT_MAX, 22),
    ],
)
def test_the_prefix_length_holds_the_hosts_plus_network_broadcast_and_gateway(
    host_count: int | None, length: int
) -> None:
    """hosts + 3 addresses, rounded up to a power of two; never below a /29."""
    assert segment_prefix_length(host_count) == length


def test_no_host_count_takes_the_default_and_needs_one() -> None:
    """The default is the design's segment_prefix_length (the pool's for an office); with neither it is refused."""
    assert segment_prefix_length(None, 24) == 24
    assert segment_prefix_length(None, 26) == 26
    assert segment_prefix_length(100, 26) == 25  # a host_count wins over the default
    with pytest.raises(ValueError, match="needs a default"):
        segment_prefix_length(None)


@pytest.mark.parametrize("host_count", range(1, SEGMENT_HOST_COUNT_MAX + 1, 7))
def test_every_sized_prefix_holds_its_hosts_and_the_next_smaller_one_does_not(host_count: int) -> None:
    """The prefix has room for the hosts and the three reserved addresses; one length longer would not (except the /29
    floor)."""
    length = segment_prefix_length(host_count)
    assert 2 ** (32 - length) >= host_count + 3
    if length < 29:
        assert 2 ** (32 - length - 1) < host_count + 3


@pytest.mark.parametrize("host_count", [0, -1, SEGMENT_HOST_COUNT_MAX + 1])
def test_a_host_count_outside_the_range_is_refused(host_count: int) -> None:
    """The schema bounds it (1..1021); the function refuses what the schema would."""
    with pytest.raises(ValueError, match="outside 1"):
        segment_prefix_length(host_count)


# ---------------------------------------------------------------------------
# Which footprint addresses a segment, and the pool's name
# ---------------------------------------------------------------------------


def test_the_data_center_decides_before_the_colocation_then_the_first_by_name() -> None:
    """DC, then colocation, then the first by sorted name; an office has no design and decides nothing."""
    dc_b, dc_a = footprint("TopologyCustomerDC", "C1-P-DC12"), footprint("TopologyCustomerDC", "C1-P-DC10")
    colo = footprint("TopologyCustomerColocation", "C1-P-FR")
    office = footprint("TopologyCustomerOffice", "C1-OFFICE")

    assert addressing_footprint([colo, dc_b, office, dc_a]) is dc_a
    assert addressing_footprint([office, colo]) is colo
    assert addressing_footprint([office]) is None
    assert addressing_footprint([]) is None


def test_the_customer_pool_name_is_lowercase_owner_environment_facility() -> None:
    """``c005-p-dc-ipv4``; the facility word follows the footprint kind, the family ends the name."""
    assert customer_pool_name("C005", "p", "TopologyCustomerDC") == "c005-p-dc-ipv4"
    assert customer_pool_name("C005", "D", "TopologyCustomerColocation") == "c005-d-colocation-ipv4"
    assert customer_pool_name("C005", "p", "TopologyCustomerDC", "ipv6") == "c005-p-dc-ipv6"
    assert customer_pool_name("C005", "p", "TopologyCustomerCloud") == "c005-p-cloud-ipv4"


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


class TestCustomerPool:
    """The pool is created lazily by the first request, then reused."""

    def test_a_missing_pool_is_created_from_a_block_of_the_designs_length(self) -> None:
        """No pool: ensure_sliced_pool carves the design's /23 from the tenant pool as the pool's first block."""
        client = Client()
        gen = make_gen(client)

        asyncio.run(gen.ensure_segment_addressing(segment_data()))

        (sliced,) = gen.sliced
        assert sliced["pool_name"] == "c005-p-dc-ipv4"
        assert sliced["parent_pool_name"] == "Tenant-PROD-IPv4"
        assert sliced["prefix_length"] == 23
        assert (sliced["role"], sliced["kind"], sliced["member_type"], sliced["is_pool"]) == (
            "customer",
            "prefix",
            "prefix",
            True,
        )
        (allocation,) = client.allocations
        assert allocation["resource_pool"] is client.pools["c005-p-dc-ipv4"]
        assert allocation["identifier"] == "c005-web-p" and allocation["prefix_length"] == 24
        assert "customer-pool-c005-p-dc-ipv4" in gen.locks

    def test_an_existing_pool_is_reused_and_nothing_is_carved(self) -> None:
        """The second segment of the customer draws from the pool; no block, no pool is created."""
        pool = customer_pool("c005-p-dc-ipv4")
        client = Client(pools={"c005-p-dc-ipv4": pool})
        gen = make_gen(client)

        asyncio.run(gen.ensure_segment_addressing(segment_data()))

        assert gen.sliced == [] and client.edges == []
        assert [a["resource_pool"] for a in client.allocations] == [pool]

    def test_the_colocation_footprint_of_a_segment_with_no_data_center_names_the_pool(self) -> None:
        """Colocation only: the pool is the customer's colocation pool, sized by the colocation design."""
        client = Client()
        gen = make_gen(client)
        colo = footprint("TopologyCustomerColocation", "C005-P-FR")
        colo["design"]["address_prefix_length"] = 21

        asyncio.run(gen.ensure_segment_addressing(segment_data(footprints=[colo])))

        assert [(s["pool_name"], s["prefix_length"]) for s in gen.sliced] == [("c005-p-colocation-ipv4", 21)]

    def test_an_office_segment_is_addressed_from_the_global_tenant_pool(self) -> None:
        """An office footprint has no design: no customer pool, the prefix comes straight from the tenant pool."""
        client = Client()
        gen = make_gen(client)

        asyncio.run(
            gen.ensure_segment_addressing(segment_data(footprints=[footprint("TopologyCustomerOffice", "C005-OFFICE")]))
        )

        assert gen.sliced == []
        (allocation,) = client.allocations
        assert allocation["resource_pool"] is client.tenant and allocation["prefix_length"] == 24

    def test_the_prefix_length_follows_the_host_count_and_the_gateway_is_the_first_address(self) -> None:
        """100 hosts: a /25, the anycast gateway on offset 1, written to the segment."""
        client = Client()
        gen = make_gen(client)
        segment = segment_data(host_count=100)

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.allocations[0]["prefix_length"] == 25 and client.allocations[0]["member_type"] == "address"
        assert segment["gateway"]["address"].endswith(".1/25")
        assert client.created == [("ManagedVxlanSegment", {"id": "seg-1", "gateway": {"id": "gw-1"}})]

    def test_a_segment_bigger_than_the_block_is_refused_before_anything_is_carved(self) -> None:
        """A /22 does not fit the S design's /23 block: growing could never help."""
        client = Client()
        gen = make_gen(client)

        with pytest.raises(GeneratorError, match=r"/22.*larger than the /23"):
            asyncio.run(gen.ensure_segment_addressing(segment_data(host_count=900)))

        assert gen.sliced == [] and client.allocations == []

    def test_a_footprint_without_a_design_is_refused(self) -> None:
        """The relationship is required, so this is data that cannot exist: the run names the footprint."""
        client = Client()
        gen = make_gen(client)
        undesigned = footprint("TopologyCustomerDC", "C005-P-DC10")
        undesigned["design"] = None

        with pytest.raises(GeneratorError, match="no design"):
            asyncio.run(gen.ensure_segment_addressing(segment_data(footprints=[undesigned])))


class TestGrowOnDemand:
    """A pool that cannot satisfy a request gets another block of the same length, once."""

    def test_an_exhausted_pool_gets_a_second_block_and_the_request_is_retried(self) -> None:
        """The block is carved from the tenant pool with the identifier ``<pool>-2``, added alone to the pool's
        resources, then the same request is served."""
        pool = customer_pool("c005-p-dc-ipv4", blocks=1)
        client = Client(pools={"c005-p-dc-ipv4": pool})
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = make_gen(client)

        client.refill_on_edge = {"c005-p-dc-ipv4": 10}

        asyncio.run(gen.ensure_segment_addressing(segment_data()))

        carve = next(a for a in client.allocations if a["resource_pool"] is client.tenant)
        assert carve["identifier"] == "c005-p-dc-ipv4-2" and carve["prefix_length"] == 23
        assert carve["member_type"] == "prefix" and carve["data"]["is_pool"] is True
        (edge,) = client.edges
        assert edge["id"] == pool.id and edge["name"] == "resources" and len(edge["nodes"]) == 1
        segment_calls = [a for a in client.allocations if a["identifier"] == "c005-web-p"]
        assert len(segment_calls) == 2
        assert segment_calls[0]["resource_pool"] is segment_calls[1]["resource_pool"] is pool

    def test_a_pool_that_stays_exhausted_after_growing_fails_the_run(self) -> None:
        """One growth per request: a second refusal is the run's error, not another block."""
        pool = customer_pool("c005-p-dc-ipv4", blocks=1)
        client = Client(pools={"c005-p-dc-ipv4": pool})
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = make_gen(client)

        with pytest.raises(GraphQLError):
            asyncio.run(gen.ensure_segment_addressing(segment_data()))

        assert len([a for a in client.allocations if a["resource_pool"] is client.tenant]) == 1
        assert client.created == []

    def test_an_exhausted_tenant_pool_fails_the_run_naming_the_pool(self) -> None:
        """No room for another block in the global pool: the error names the tenant pool and the customer pool."""
        pool = customer_pool("c005-p-dc-ipv4", blocks=1)
        client = Client(pools={"c005-p-dc-ipv4": pool}, tenant_blocks=0)
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = make_gen(client)

        with pytest.raises(GeneratorError, match=r"Tenant-PROD-IPv4 has no room for another /23 block.*c005-p-dc-ipv4"):
            asyncio.run(gen.ensure_segment_addressing(segment_data()))

        assert client.edges == []

    def test_a_retry_after_a_failed_attach_carves_the_same_block(self) -> None:
        """The block key is ``<pool>-<resources + 1>`` and the allocation is idempotent per identifier, so a run that
        carved but did not attach returns the same prefix next time."""
        pool = customer_pool("c005-p-dc-ipv4", blocks=2)
        client = Client(pools={"c005-p-dc-ipv4": pool})
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = make_gen(client)
        for _ in range(2):
            with pytest.raises(GraphQLError):
                asyncio.run(gen.ensure_segment_addressing(segment_data()))

        carves = [a for a in client.allocations if a["resource_pool"] is client.tenant]
        assert [a["identifier"] for a in carves] == ["c005-p-dc-ipv4-3", "c005-p-dc-ipv4-3"]
        assert client.known[(client.tenant.id, "c005-p-dc-ipv4-3")] is not None
        assert len([k for k in client.known if k[0] == client.tenant.id]) == 1


class TestIdempotencyAndLengthChange:
    """The segment name is the identifier; a changed host_count is reported and never re-allocated."""

    def test_a_re_run_returns_the_same_prefix(self) -> None:
        """Two runs with the same segment name allocate with one identifier and get one prefix."""
        client = Client()
        first, second = segment_data(), segment_data()
        asyncio.run(make_gen(client).ensure_segment_addressing(first))
        asyncio.run(make_gen(client).ensure_segment_addressing(second))

        assert [a["identifier"] for a in client.allocations] == ["c005-web-p", "c005-web-p"]
        assert first["gateway"]["ip_prefix"]["prefix"] == second["gateway"]["ip_prefix"]["prefix"]

    def test_a_changed_length_for_an_allocated_identifier_is_reported_with_the_remedy(self) -> None:
        """The server refuses another length; the run names the segment, the wanted length and the remedy, and
        carves nothing else."""
        client = Client()
        asyncio.run(make_gen(client).ensure_segment_addressing(segment_data()))  # a /24 first
        client.created.clear()

        with pytest.raises(GeneratorError, match=r"c005-web-p.*another length than /25.*delete the segment's gateway"):
            asyncio.run(make_gen(client).ensure_segment_addressing(segment_data(host_count=100)))

        assert client.created == []
        assert [a for a in client.allocations if a["resource_pool"] is client.tenant] == []

    def test_a_gateway_of_another_length_than_the_host_count_asks_for_is_reported(self) -> None:
        """An addressed segment whose host_count was edited: the allocated /24 against the wanted /25."""
        gateway = {"id": "gw", "ip_prefix": {"prefix": "10.176.0.0/24"}}
        client = Client()
        gen = make_gen(client)

        with pytest.raises(GeneratorError, match=r"10.176.0.0/24 is not the /25 its host_count 100"):
            asyncio.run(gen.ensure_segment_addressing(segment_data(host_count=100, gateway=gateway)))

        assert client.allocations == []

    def test_an_authored_gateway_is_kept_and_nothing_is_allocated(self) -> None:
        """No host_count: the gateway is never judged or touched, whatever its length."""
        client = Client()
        gen = make_gen(client)
        segment = segment_data(gateway={"id": "gw", "ip_prefix": {"prefix": "10.250.0.0/22"}})

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.allocations == [] and client.created == [] and gen.sliced == []

    def test_a_gateway_of_the_wanted_length_passes(self) -> None:
        """Addressed with a host_count that still matches: a quiet no-op."""
        gateway = {"id": "gw", "ip_prefix": {"prefix": "10.176.0.0/25"}}
        gen = make_gen(Client())

        asyncio.run(gen.ensure_segment_addressing(segment_data(host_count=100, gateway=gateway)))
