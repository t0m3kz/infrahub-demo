"""An IPv6 segment's addressing (generators/segments/segment_addressing.py)."""

from __future__ import annotations

import asyncio

import pytest

from generators.mixins.logger import GeneratorError
from tests.unit.segment_customer_pool_helpers import Client, customer_pool, footprint, make_gen, segment_data

_POOL = "c005-p-dc-ipv6"


class TestIpv6Allocation:
    """The pool, the /64 and the gateway of an IPv6 segment."""

    def test_the_prefix_is_a_64_from_the_customers_ipv6_pool(self) -> None:
        """An existing IPv6 pool is drawn from with the design's ipv6_segment_prefix_length; no IPv4 pool is touched."""
        pool = customer_pool(_POOL)
        client = Client(pools={_POOL: pool})
        gen = make_gen(client)
        segment = segment_data(ip_version="ipv6")

        asyncio.run(gen.ensure_segment_addressing(segment))

        (allocation,) = client.allocations
        assert allocation["resource_pool"] is pool and allocation["prefix_length"] == 64
        assert allocation["identifier"] == "c005-web-p" and allocation["member_type"] == "address"
        assert gen.sliced == [] and f"customer-pool-{_POOL}" in gen.locks

    def test_the_gateway_is_the_first_address_of_the_subnet_with_the_subnets_mask(self) -> None:
        """Offset 1 of the /64 (OFFSET_ANYCAST), saved as `<first address>/64` and written to the segment."""
        client = Client(pools={_POOL: customer_pool(_POOL)})
        gen = make_gen(client)
        segment = segment_data(ip_version="ipv6")

        asyncio.run(gen.ensure_segment_addressing(segment))

        gateway = segment["gateway"]
        assert gateway["address"].endswith("::1/64") and ":" in gateway["address"]
        assert gateway["ip_prefix"]["prefix"].endswith("::/64")
        assert client.created == [("ManagedVxlanSegment", {"id": "seg-1", "gateway": {"id": "gw-1"}})]

    def test_a_missing_pool_is_repaired_from_the_ipv6_tenant_pool_with_the_ipv6_block_length(self) -> None:
        """Boarding normally made it; if not, the same function carves the design's /60 from Tenant-PROD-IPv6."""
        client = Client()
        gen = make_gen(client)

        asyncio.run(gen.ensure_segment_addressing(segment_data(ip_version="ipv6")))

        (sliced,) = gen.sliced
        assert (sliced["pool_name"], sliced["parent_pool_name"], sliced["prefix_length"]) == (
            _POOL,
            "Tenant-PROD-IPv6",
            60,
        )
        assert sliced["track"] is False  # shared by every run of the customer: no run's cleanup may reclaim it

    def test_the_nonprod_environment_uses_the_nonprod_ipv6_tenant_pool(self) -> None:
        """environment d -> zone NONPROD -> Tenant-NONPROD-IPv6; the pool name carries the environment."""
        client = Client()
        gen = make_gen(client)
        segment = segment_data(ip_version="ipv6")
        segment.update({"environment": "d", "security_zone": {"name": "NONPROD"}})

        asyncio.run(gen.ensure_segment_addressing(segment))

        (sliced,) = gen.sliced
        assert (sliced["pool_name"], sliced["parent_pool_name"]) == ("c005-d-dc-ipv6", "Tenant-NONPROD-IPv6")

    def test_a_host_count_sizes_nothing_on_an_ipv6_segment(self) -> None:
        """host_count 100 would be a /25 in IPv4; the IPv6 segment is the design's /64 whatever it states."""
        client = Client(pools={_POOL: customer_pool(_POOL)})
        gen = make_gen(client)

        asyncio.run(gen.ensure_segment_addressing(segment_data(host_count=100, ip_version="ipv6")))

        assert client.allocations[0]["prefix_length"] == 64

    def test_the_colocation_footprint_names_a_colocation_ipv6_pool(self) -> None:
        """No data center: the colocation footprint's pool and block (customer_pool_name, the same rule as IPv4)."""
        client = Client()
        gen = make_gen(client)
        colo = footprint("TopologyCustomerColocation", "C005-P-FR")
        colo["design"]["ipv6_prefix_length"] = 56

        asyncio.run(gen.ensure_segment_addressing(segment_data(footprints=[colo], ip_version="ipv6")))

        assert [(s["pool_name"], s["prefix_length"]) for s in gen.sliced] == [("c005-p-colocation-ipv6", 56)]

    def test_an_office_segment_takes_the_ipv6_tenant_pools_default_length(self) -> None:
        """No design: straight from Tenant-PROD-IPv6 at the pool's default_prefix_length (64)."""
        client = Client()
        gen = make_gen(client)
        office = {"typename": "TopologyCustomerOffice", "name": "C005-OFFICE", "design": None}

        asyncio.run(gen.ensure_segment_addressing(segment_data(footprints=[office], ip_version="ipv6")))

        (allocation,) = client.allocations
        assert allocation["resource_pool"] is client.tenant6 and allocation["prefix_length"] == 64

    def test_a_design_without_the_ipv6_numbers_is_refused(self) -> None:
        """An IPv6 segment on a footprint whose design states no IPv6 block names the footprint."""
        client = Client()
        gen = make_gen(client)
        bare = footprint("TopologyCustomerDC", "C005-P-DC10")
        del bare["design"]["ipv6_prefix_length"]

        with pytest.raises(GeneratorError, match="C005-P-DC10 has no design to size c005-p-dc-ipv6"):
            asyncio.run(gen.ensure_segment_addressing(segment_data(footprints=[bare], ip_version="ipv6")))

    def test_the_default_family_is_ipv4_when_the_query_does_not_say(self) -> None:
        """A payload without ip_version (an older query) is an IPv4 segment: the IPv4 pool and length."""
        client = Client()
        gen = make_gen(client)
        segment = segment_data()
        del segment["ip_version"]

        asyncio.run(gen.ensure_segment_addressing(segment))

        assert [s["pool_name"] for s in gen.sliced] == ["c005-p-dc-ipv4"]
        assert client.allocations[0]["prefix_length"] == 24


class TestIpv6GrowthAndIdempotency:
    """Growth by one block of the IPv6 block length, and the same prefix on a re-run."""

    def test_an_exhausted_ipv6_pool_gets_a_second_block_of_the_ipv6_length(self) -> None:
        """The block is carved from Tenant-PROD-IPv6 with `<pool>-2` and attached alone, then the request is retried."""
        pool = customer_pool(_POOL, blocks=1)
        client = Client(pools={_POOL: pool})
        client.customer_room[_POOL] = 0
        client.refill_on_edge = {_POOL: 10}
        gen = make_gen(client)

        asyncio.run(gen.ensure_segment_addressing(segment_data(ip_version="ipv6")))

        carve = next(a for a in client.allocations if a["resource_pool"] is client.tenant6)
        assert carve["identifier"] == f"{_POOL}-2" and carve["prefix_length"] == 60
        assert carve["member_type"] == "prefix" and carve["data"]["is_pool"] is True
        (edge,) = client.edges
        assert edge["id"] == pool.id and edge["name"] == "resources" and len(edge["nodes"]) == 1

    def test_a_tenant_ipv6_pool_with_no_room_names_the_pool_and_the_length(self) -> None:
        """No room for another /60: the error names Tenant-PROD-IPv6 and the customer pool."""
        client = Client(pools={_POOL: customer_pool(_POOL)}, tenant_blocks=0)
        client.customer_room[_POOL] = 0
        gen = make_gen(client)

        with pytest.raises(GeneratorError, match=rf"Tenant-PROD-IPv6 has no room for another /60 block.*{_POOL}"):
            asyncio.run(gen.ensure_segment_addressing(segment_data(ip_version="ipv6")))

    def test_a_re_run_returns_the_same_prefix(self) -> None:
        """One identifier (the segment name), one prefix."""
        client = Client()
        first, second = segment_data(ip_version="ipv6"), segment_data(ip_version="ipv6")
        asyncio.run(make_gen(client).ensure_segment_addressing(first))
        asyncio.run(make_gen(client).ensure_segment_addressing(second))

        assert [a["identifier"] for a in client.allocations] == ["c005-web-p", "c005-web-p"]
        assert first["gateway"]["ip_prefix"]["prefix"] == second["gateway"]["ip_prefix"]["prefix"]

    def test_a_changed_ipv6_length_is_reported_with_the_remedy(self) -> None:
        """The server refuses another length for an allocated identifier: named, with the remedy, nothing re-carved."""
        client = Client()
        asyncio.run(make_gen(client).ensure_segment_addressing(segment_data(ip_version="ipv6")))
        client.created.clear()
        changed = footprint("TopologyCustomerDC", "C005-P-DC10")
        changed["design"]["ipv6_segment_prefix_length"] = 80

        with pytest.raises(GeneratorError, match=r"c005-web-p.*another length than /80.*delete the segment's gateway"):
            asyncio.run(
                make_gen(client).ensure_segment_addressing(segment_data(footprints=[changed], ip_version="ipv6"))
            )

        assert client.created == []

    def test_an_authored_ipv6_gateway_is_kept(self) -> None:
        """A gateway already there is never judged by the template or touched, host_count or not."""
        client = Client()
        gen = make_gen(client)
        gateway = {"id": "gw", "address": "fd5a:1760::1/64", "ip_prefix": {"prefix": "fd5a:1760::/64"}}

        asyncio.run(gen.ensure_segment_addressing(segment_data(host_count=100, ip_version="ipv6", gateway=gateway)))

        assert client.allocations == [] and client.created == [] and gen.sliced == []
