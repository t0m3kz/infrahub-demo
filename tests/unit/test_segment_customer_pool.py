"""The sizing of a segment's prefix and its customer's pool (generators/topology/segment_addressing.py).

A segment with a ``host_count`` gets the smallest prefix that holds it (``segment_prefix_length``, a /24 without
one). Its prefix is drawn from the customer's pool, created by the first request from a block of the footprint
design's ``address_prefix_length`` out of the environment's tenant pool, and grown by one block when it cannot
satisfy a request. The server-side behaviour (the pool, the resource edge, the allocation) is mocked; what is held here
is which calls are made, with which identifiers and lengths.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.protocols import CoreIPPrefixPool

from generators.logger import GeneratorError
from generators.topology.segment import VxlanSegmentGenerator
from utils.limits import SEGMENT_HOST_COUNT_MAX
from utils.segment_addressing import addressing_footprint, customer_pool_name, segment_prefix_length

# ---------------------------------------------------------------------------
# The sizing function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host_count", "length"),
    [
        (None, 24),
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
    """hosts + 3 addresses, rounded up to a power of two; no host_count is a /24; never below a /29."""
    assert segment_prefix_length(host_count) == length


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


def _fp(kind: str, name: str) -> dict[str, Any]:
    return {"typename": kind, "name": name, "design": {"name": "S", "address_prefix_length": 23}}


def test_the_data_center_decides_before_the_colocation_then_the_first_by_name() -> None:
    """DC, then colocation, then the first by sorted name; an office has no design and decides nothing."""
    dc_b, dc_a = _fp("TopologyCustomerDC", "C1-P-DC12"), _fp("TopologyCustomerDC", "C1-P-DC10")
    colo = _fp("TopologyCustomerColocation", "C1-P-FR")
    office = _fp("TopologyCustomerOffice", "C1-OFFICE")

    assert addressing_footprint([colo, dc_b, office, dc_a]) is dc_a
    assert addressing_footprint([office, colo]) is colo
    assert addressing_footprint([office]) is None
    assert addressing_footprint([]) is None


def test_the_customer_pool_name_is_lowercase_owner_environment_facility() -> None:
    """``c005-p-dc-ipv4``; the facility word follows the footprint kind."""
    assert customer_pool_name("C005", "p", "TopologyCustomerDC") == "c005-p-dc-ipv4"
    assert customer_pool_name("C005", "D", "TopologyCustomerColocation") == "c005-d-colocation-ipv4"


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------


def _exhausted() -> GraphQLError:
    return GraphQLError(errors=[{"message": "no more prefix available in the pool"}])


class _Client:
    """The SDK calls the addressing makes, recorded; a pool answers for the names in ``pools``."""

    def __init__(self, *, pools: dict[str, MagicMock] | None = None, tenant_blocks: int = 8) -> None:
        self.pools = pools or {}
        self.tenant = MagicMock(id="pool-tenant")
        self.tenant.name.value = "Tenant-PROD-IPv4"
        self.allocations: list[dict[str, Any]] = []
        self.tenant_blocks = tenant_blocks  # blocks the tenant pool can still carve
        self.customer_room: dict[str, int] = {}  # pool name -> allocations it can still serve (absent: unlimited)
        self.edges: list[dict[str, Any]] = []
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.known: dict[tuple[str, str], MagicMock] = {}
        self.refill_on_edge: dict[str, int] = {}

    async def filters(self, *, kind: Any, **kwargs: Any) -> list[Any]:
        assert kind is CoreIPPrefixPool
        name = kwargs["name__value"]
        if name == "Tenant-PROD-IPv4":
            return [self.tenant]
        return [self.pools[name]] if name in self.pools else []

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> Any:
        """A prefix per (pool, identifier): the same identifier returns the same prefix; a length change is refused."""
        pool = kwargs["resource_pool"]
        self.allocations.append(kwargs)
        key = (pool.id, kwargs["identifier"])
        if key in self.known:
            known = self.known[key]
            if int(known.prefix.value.split("/")[1]) != kwargs["prefix_length"]:
                raise GraphQLError(errors=[{"message": "the prefix length cannot be changed for this identifier"}])
            return known
        if pool is self.tenant:
            if self.tenant_blocks <= 0:
                raise _exhausted()
            self.tenant_blocks -= 1
        else:
            room = self.customer_room.get(pool.name.value)
            if room is not None:
                if room <= 0:
                    raise _exhausted()
                self.customer_room[pool.name.value] = room - 1
        node = MagicMock(id=f"prefix-{len(self.known)}")
        node.prefix.value = f"10.176.{len(self.known)}.0/{kwargs['prefix_length']}"
        self.known[key] = node
        return node

    async def create(self, *, kind: str, data: dict[str, Any]) -> Any:
        self.created.append((kind, data))
        node = MagicMock()
        node.save = AsyncMock()
        return node

    async def execute_graphql(self, *, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.edges.append(variables["data"])
        self.customer_room.update(self.refill_on_edge)  # the new block gives the pool room
        return {"RelationshipAdd": {"ok": True}}


def _customer_pool(name: str, blocks: int = 1) -> MagicMock:
    pool = MagicMock(id=f"pool-{name}")
    pool.name.value = name
    pool.resources.peers = [MagicMock() for _ in range(blocks)]
    return pool


def _gen(client: _Client) -> Any:
    """A VxlanSegmentGenerator with the recording client, a raising logger and a lock that records its keys."""
    gen: Any = VxlanSegmentGenerator.__new__(VxlanSegmentGenerator)
    gen.client = client
    gen.logger = MagicMock()

    def _fail(message: str, *args: object, **kwargs: object) -> None:
        raise GeneratorError(message)

    gen.logger.error.side_effect = _fail
    gen.locks = []

    @asynccontextmanager
    async def lock(key: str) -> AsyncGenerator[None]:
        gen.locks.append(key)
        yield

    gen.resource_lock = lock

    async def create_pool(**kwargs: Any) -> MagicMock:
        gen.sliced.append(kwargs)
        pool = _customer_pool(kwargs["pool_name"])
        client.pools[kwargs["pool_name"]] = pool
        return pool

    gen.sliced = []
    gen.ensure_sliced_pool = create_pool
    gen._get_parent_pool_with_retry = AsyncMock(return_value=client.tenant)

    async def upsert(prefix: Any, *, offsets: tuple[int, ...], description: str, track: bool) -> dict[int, Any]:
        address = MagicMock(id="gw-1")
        address.address.value = prefix.prefix.value.replace(".0/", f".{offsets[0]}/")
        return {offsets[0]: address}

    gen.upsert_prefix_addresses = upsert
    return gen


def _segment(
    host_count: int | None = None, footprints: list[dict[str, Any]] | None = None, gateway: Any = None
) -> dict[str, Any]:
    """A cleaned vxlan_segment query result of customer C005 (PROD)."""
    return {
        "id": "seg-1",
        "typename": "ManagedVxlanSegment",
        "name": "c005-web-p",
        "environment": "p",
        "owner": {"id": "cust-1", "org_id": "C005"},
        "security_zone": {"name": "PROD"},
        "host_count": host_count,
        "customer_deployments": footprints if footprints is not None else [_fp("TopologyCustomerDC", "C005-P-DC10")],
        "gateway": gateway,
    }


class TestCustomerPool:
    """The pool is created lazily by the first request, then reused."""

    def test_a_missing_pool_is_created_from_a_block_of_the_designs_length(self) -> None:
        """No pool: ensure_sliced_pool carves the design's /23 from the tenant pool as the pool's first block."""
        client = _Client()
        gen = _gen(client)

        asyncio.run(gen.ensure_segment_addressing(_segment()))

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
        pool = _customer_pool("c005-p-dc-ipv4")
        client = _Client(pools={"c005-p-dc-ipv4": pool})
        gen = _gen(client)

        asyncio.run(gen.ensure_segment_addressing(_segment()))

        assert gen.sliced == [] and client.edges == []
        assert [a["resource_pool"] for a in client.allocations] == [pool]

    def test_the_colocation_footprint_of_a_segment_with_no_data_center_names_the_pool(self) -> None:
        """Colocation only: the pool is the customer's colocation pool, sized by the colocation design."""
        client = _Client()
        gen = _gen(client)
        colo = _fp("TopologyCustomerColocation", "C005-P-FR")
        colo["design"]["address_prefix_length"] = 21

        asyncio.run(gen.ensure_segment_addressing(_segment(footprints=[colo])))

        assert [(s["pool_name"], s["prefix_length"]) for s in gen.sliced] == [("c005-p-colocation-ipv4", 21)]

    def test_an_office_segment_is_addressed_from_the_global_tenant_pool(self) -> None:
        """An office footprint has no design: no customer pool, the prefix comes straight from the tenant pool."""
        client = _Client()
        gen = _gen(client)

        asyncio.run(gen.ensure_segment_addressing(_segment(footprints=[_fp("TopologyCustomerOffice", "C005-OFFICE")])))

        assert gen.sliced == []
        (allocation,) = client.allocations
        assert allocation["resource_pool"] is client.tenant and allocation["prefix_length"] == 24

    def test_the_prefix_length_follows_the_host_count_and_the_gateway_is_the_first_address(self) -> None:
        """100 hosts: a /25, the anycast gateway on offset 1, written to the segment."""
        client = _Client()
        gen = _gen(client)
        segment = _segment(host_count=100)

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.allocations[0]["prefix_length"] == 25 and client.allocations[0]["member_type"] == "address"
        assert segment["gateway"]["address"].endswith(".1/25")
        assert client.created == [("ManagedVxlanSegment", {"id": "seg-1", "gateway": {"id": "gw-1"}})]

    def test_a_segment_bigger_than_the_block_is_refused_before_anything_is_carved(self) -> None:
        """A /22 does not fit the S design's /23 block: growing could never help."""
        client = _Client()
        gen = _gen(client)

        with pytest.raises(GeneratorError, match=r"/22.*larger than the /23"):
            asyncio.run(gen.ensure_segment_addressing(_segment(host_count=900)))

        assert gen.sliced == [] and client.allocations == []

    def test_a_footprint_without_a_design_is_refused(self) -> None:
        """The relationship is required, so this is data that cannot exist: the run names the footprint."""
        client = _Client()
        gen = _gen(client)
        footprint = _fp("TopologyCustomerDC", "C005-P-DC10")
        footprint["design"] = None

        with pytest.raises(GeneratorError, match="no design"):
            asyncio.run(gen.ensure_segment_addressing(_segment(footprints=[footprint])))


class TestGrowOnDemand:
    """A pool that cannot satisfy a request gets another block of the same length, once."""

    def test_an_exhausted_pool_gets_a_second_block_and_the_request_is_retried(self) -> None:
        """The block is carved from the tenant pool with the identifier ``<pool>-2``, added alone to the pool's
        resources, then the same request is served."""
        pool = _customer_pool("c005-p-dc-ipv4", blocks=1)
        client = _Client(pools={"c005-p-dc-ipv4": pool})
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = _gen(client)

        client.refill_on_edge = {"c005-p-dc-ipv4": 10}

        asyncio.run(gen.ensure_segment_addressing(_segment()))

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
        pool = _customer_pool("c005-p-dc-ipv4", blocks=1)
        client = _Client(pools={"c005-p-dc-ipv4": pool})
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = _gen(client)

        with pytest.raises(GraphQLError):
            asyncio.run(gen.ensure_segment_addressing(_segment()))

        assert len([a for a in client.allocations if a["resource_pool"] is client.tenant]) == 1
        assert client.created == []

    def test_an_exhausted_tenant_pool_fails_the_run_naming_the_pool(self) -> None:
        """No room for another block in the global pool: the error names the tenant pool and the customer pool."""
        pool = _customer_pool("c005-p-dc-ipv4", blocks=1)
        client = _Client(pools={"c005-p-dc-ipv4": pool}, tenant_blocks=0)
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = _gen(client)

        with pytest.raises(GeneratorError, match=r"Tenant-PROD-IPv4 has no room for another /23 block.*c005-p-dc-ipv4"):
            asyncio.run(gen.ensure_segment_addressing(_segment()))

        assert client.edges == []

    def test_a_retry_after_a_failed_attach_carves_the_same_block(self) -> None:
        """The block key is ``<pool>-<resources + 1>`` and the allocation is idempotent per identifier, so a run that
        carved but did not attach returns the same prefix next time."""
        pool = _customer_pool("c005-p-dc-ipv4", blocks=2)
        client = _Client(pools={"c005-p-dc-ipv4": pool})
        client.customer_room["c005-p-dc-ipv4"] = 0
        gen = _gen(client)
        for _ in range(2):
            with pytest.raises(GraphQLError):
                asyncio.run(gen.ensure_segment_addressing(_segment()))

        carves = [a for a in client.allocations if a["resource_pool"] is client.tenant]
        assert [a["identifier"] for a in carves] == ["c005-p-dc-ipv4-3", "c005-p-dc-ipv4-3"]
        assert client.known[(client.tenant.id, "c005-p-dc-ipv4-3")] is not None
        assert len([k for k in client.known if k[0] == client.tenant.id]) == 1


class TestIdempotencyAndLengthChange:
    """The segment name is the identifier; a changed host_count is reported and never re-allocated."""

    def test_a_re_run_returns_the_same_prefix(self) -> None:
        """Two runs with the same segment name allocate with one identifier and get one prefix."""
        client = _Client()
        first, second = _segment(), _segment()
        asyncio.run(_gen(client).ensure_segment_addressing(first))
        asyncio.run(_gen(client).ensure_segment_addressing(second))

        assert [a["identifier"] for a in client.allocations] == ["c005-web-p", "c005-web-p"]
        assert first["gateway"]["ip_prefix"]["prefix"] == second["gateway"]["ip_prefix"]["prefix"]

    def test_a_changed_length_for_an_allocated_identifier_is_reported_with_the_remedy(self) -> None:
        """The server refuses another length; the run names the segment, the wanted length and the remedy, and
        carves nothing else."""
        client = _Client()
        asyncio.run(_gen(client).ensure_segment_addressing(_segment()))  # a /24 first
        client.created.clear()

        with pytest.raises(GeneratorError, match=r"c005-web-p.*another length than /25.*delete the segment's gateway"):
            asyncio.run(_gen(client).ensure_segment_addressing(_segment(host_count=100)))

        assert client.created == []
        assert [a for a in client.allocations if a["resource_pool"] is client.tenant] == []

    def test_a_gateway_of_another_length_than_the_host_count_asks_for_is_reported(self) -> None:
        """An addressed segment whose host_count was edited: the allocated /24 against the wanted /25."""
        gateway = {"id": "gw", "ip_prefix": {"prefix": "10.176.0.0/24"}}
        client = _Client()
        gen = _gen(client)

        with pytest.raises(GeneratorError, match=r"10.176.0.0/24 is not the /25 its host_count 100"):
            asyncio.run(gen.ensure_segment_addressing(_segment(host_count=100, gateway=gateway)))

        assert client.allocations == []

    def test_an_authored_gateway_is_kept_and_nothing_is_allocated(self) -> None:
        """No host_count: the gateway is never judged or touched, whatever its length."""
        client = _Client()
        gen = _gen(client)
        segment = _segment(gateway={"id": "gw", "ip_prefix": {"prefix": "10.250.0.0/22"}})

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert client.allocations == [] and client.created == [] and gen.sliced == []

    def test_a_gateway_of_the_wanted_length_passes(self) -> None:
        """Addressed with a host_count that still matches: a quiet no-op."""
        gateway = {"id": "gw", "ip_prefix": {"prefix": "10.176.0.0/25"}}
        gen = _gen(_Client())

        asyncio.run(gen.ensure_segment_addressing(_segment(host_count=100, gateway=gateway)))
