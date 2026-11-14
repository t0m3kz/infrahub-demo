"""The customer's pools are created when the footprint is boarded (generators/segments/segment_addressing.py)."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.customers.customer_cloud import CustomerDeploymentCloudGenerator
from generators.customers.customer_colocation import CustomerDeploymentColocationExchangeGenerator
from generators.customers.customer_dc import CustomerDeploymentDCExchangeGenerator
from generators.mixins.logger import GeneratorError
from generators.segments.segment_addressing import SegmentAddressingMixin
from tests.unit.cloud_range_helpers import CloudClient
from tests.unit.customer_generator_helpers import make_generator
from tests.unit.segment_customer_pool_helpers import Client, customer_pool, make_gen, segment_data

_DC = "TopologyCustomerDC"
_COLO = "TopologyCustomerColocation"
_CLOUD = "TopologyCustomerCloud"
_DESIGN = {"name": "M", "address_prefix_length": 21, "ipv6_prefix_length": 58}
_CLOUD_DESIGN = {"name": "S", "address_prefix_length": 28}


def _board(gen: Any, kind: str, *, design: dict[str, Any], environment: str = "p", org_id: str = "C005") -> None:
    asyncio.run(
        gen.ensure_customer_pools(
            kind=kind, org_id=org_id, environment=environment, design=design, footprint_name=f"{org_id}-FP"
        )
    )


class TestPoolsCreatedAtBoarding:
    """What is carved for each footprint kind."""

    @pytest.mark.parametrize("kind", [_DC, _COLO])
    def test_dc_and_colocation_get_an_ipv4_and_an_ipv6_pool_of_their_tiers_lengths(self, kind: str) -> None:
        """M: a /21 from Tenant-PROD-IPv4 and a /58 from Tenant-PROD-IPv6, named for the facility kind."""
        gen = make_gen(Client())

        _board(gen, kind, design=_DESIGN)

        facility = "dc" if kind == _DC else "colocation"
        assert [(s["pool_name"], s["parent_pool_name"], s["prefix_length"]) for s in gen.sliced] == [
            (f"c005-p-{facility}-ipv4", "Tenant-PROD-IPv4", 21),
            (f"c005-p-{facility}-ipv6", "Tenant-PROD-IPv6", 58),
        ]
        assert all(s["role"] == "customer" and s["member_type"] == "prefix" and s["is_pool"] for s in gen.sliced)
        assert all(s["track"] is False for s in gen.sliced)
        assert gen.locks == [f"customer-pool-c005-p-{facility}-ipv4", f"customer-pool-c005-p-{facility}-ipv6"]

    def test_a_cloud_footprint_gets_the_ipv4_routed_range_only(self) -> None:
        """The cloud tier's address_prefix_length (S /28) from the tenant pool; no IPv6 pool."""
        gen = make_gen(Client())

        _board(gen, _CLOUD, design=_CLOUD_DESIGN)

        assert [(s["pool_name"], s["parent_pool_name"], s["prefix_length"]) for s in gen.sliced] == [
            ("c005-p-cloud-ipv4", "Tenant-PROD-IPv4", 28)
        ]

    @pytest.mark.parametrize(
        ("environment", "zone"), [("p", "PROD"), ("n", "NONPROD"), ("d", "NONPROD"), ("t", "NONPROD")]
    )
    def test_the_environment_picks_the_tenant_pool_and_names_the_customer_pool(
        self, environment: str, zone: str
    ) -> None:
        """`p` is PROD, every other environment NONPROD; the environment letter is in the customer pool's name."""
        gen = make_gen(Client())

        _board(gen, _DC, design=_DESIGN, environment=environment)

        assert [(s["pool_name"], s["parent_pool_name"]) for s in gen.sliced] == [
            (f"c005-{environment}-dc-ipv4", f"Tenant-{zone}-IPv4"),
            (f"c005-{environment}-dc-ipv6", f"Tenant-{zone}-IPv6"),
        ]

    def test_a_second_footprint_of_the_same_customer_and_kind_shares_the_pools(self) -> None:
        """The pools exist: nothing is carved, nothing is allocated, the second footprint finds them."""
        client = Client(
            pools={"c005-p-dc-ipv4": customer_pool("c005-p-dc-ipv4"), "c005-p-dc-ipv6": customer_pool("c005-p-dc-ipv6")}
        )
        gen = make_gen(client)

        _board(gen, _DC, design=_DESIGN)

        assert gen.sliced == [] and client.allocations == [] and client.edges == []

    def test_another_kind_or_customer_gets_its_own_pools(self) -> None:
        """The pool is per (customer, environment, facility kind): the colocation footprint of the same customer is new."""
        client = Client(pools={"c005-p-dc-ipv4": customer_pool("c005-p-dc-ipv4")})
        gen = make_gen(client)

        _board(gen, _COLO, design=_DESIGN)
        _board(gen, _DC, design=_DESIGN, org_id="C006")

        assert sorted(s["pool_name"] for s in gen.sliced) == [
            "c005-p-colocation-ipv4",
            "c005-p-colocation-ipv6",
            "c006-p-dc-ipv4",
            "c006-p-dc-ipv6",
        ]

    def test_boarding_twice_is_idempotent(self) -> None:
        """The second run finds what the first created (the client answers for the created pool)."""
        client = Client()
        gen = make_gen(client)

        _board(gen, _DC, design=_DESIGN)
        _board(gen, _DC, design=_DESIGN)

        assert [s["pool_name"] for s in gen.sliced] == ["c005-p-dc-ipv4", "c005-p-dc-ipv6"]

    @pytest.mark.parametrize(
        ("kind", "design", "missing"),
        [
            (_DC, {"name": "M", "address_prefix_length": 21}, "ipv6"),
            (_DC, {"name": "M", "ipv6_prefix_length": 58}, "ipv4"),
            (_CLOUD, {"name": "S"}, "ipv4"),
        ],
    )
    def test_a_design_without_a_block_length_fails_the_run_naming_the_family(
        self, kind: str, design: dict[str, Any], missing: str
    ) -> None:
        """A tier row cannot lack its required numbers; if the query data does, the run fails and names it."""
        gen = make_gen(Client())

        with pytest.raises(
            GeneratorError, match=rf"C005-FP: its design {design['name']} has no {missing} block length"
        ):
            _board(gen, kind, design=design)

    def test_the_pool_is_still_repaired_by_a_segment_that_finds_it_missing(self) -> None:
        """One implementation: ensure_segment_addressing creates the pool through the same function when it is missing."""
        assert SegmentAddressingMixin._find_or_create_customer_pool is not None
        client = Client()
        gen = make_gen(client)
        asyncio.run(gen.ensure_segment_addressing(segment_data()))

        assert [s["pool_name"] for s in gen.sliced] == ["c005-p-dc-ipv4"]


class TestTheBoardingGenerators:
    """The footprint generators board the pools first; the cloud generator does only that."""

    @pytest.mark.parametrize(
        "cls", [CustomerDeploymentDCExchangeGenerator, CustomerDeploymentColocationExchangeGenerator]
    )
    def test_the_dc_and_colocation_generators_inherit_the_one_implementation(self, cls: Any) -> None:
        """No copy: they get ensure_customer_pools from SegmentAddressingMixin through CustomerFacilityMixin."""
        assert cls.ensure_customer_pools is SegmentAddressingMixin.ensure_customer_pools

    @pytest.mark.parametrize(
        ("cls", "kind"),
        [(CustomerDeploymentDCExchangeGenerator, _DC), (CustomerDeploymentColocationExchangeGenerator, _COLO)],
    )
    def test_generate_boards_the_pools_from_the_footprints_design(self, cls: Any, kind: str) -> None:
        """generate() passes the kind, the owner, the environment and the design to ensure_customer_pools, first."""
        gen = make_generator(cls)
        gen.ensure_customer_pools = AsyncMock()
        payload = {
            kind: [
                {
                    "id": "cust-1",
                    "name": "C005-P-X",
                    "environment": "p",
                    "owner": {"org_id": "C005"},
                    "parent": {"id": "parent-1", "name": "X"},
                    "design": {**_DESIGN, "dedicated_firewall": {"value": False}},
                }
            ]
        }

        asyncio.run(gen.generate(payload))

        gen.ensure_customer_pools.assert_awaited_once()
        call = gen.ensure_customer_pools.await_args
        assert call is not None
        kwargs = call.kwargs
        assert (kwargs["kind"], kwargs["org_id"], kwargs["environment"], kwargs["footprint_name"]) == (
            kind,
            "C005",
            "p",
            "C005-P-X",
        )
        assert kwargs["design"]["address_prefix_length"] == 21

    def test_the_cloud_generator_hands_the_footprint_to_the_one_range_function(self) -> None:
        """generate() passes the cleaned footprint to ensure_cloud_ranges; the footprint joins the tracking group."""
        gen = CustomerDeploymentCloudGenerator.__new__(CustomerDeploymentCloudGenerator)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.ensure_cloud_ranges = AsyncMock()
        footprint = {
            "id": "cloud-1",
            "name": "C003-P-VAULTEX-AWS-EU-CENTRAL-1",
            "environment": "p",
            "owner": {"org_id": "C003"},
            "design": {"name": "S", "address_prefix_length": 28},
        }

        asyncio.run(gen.generate({_CLOUD: [footprint]}))

        gen.client.group_context.related_node_ids.append.assert_called_once_with("cloud-1")
        gen.ensure_cloud_ranges.assert_awaited_once_with(footprint)

    def test_the_cloud_generator_creates_the_real_pool_end_to_end(self) -> None:
        """With the real mixins and a recording client: one IPv4 pool of the cloud tier, no IPv6 pool."""
        client = CloudClient([], [])
        gen = make_gen(client, CustomerDeploymentCloudGenerator)
        cloud = CustomerDeploymentCloudGenerator.generate
        gen.client.group_context = MagicMock()
        payload = {
            _CLOUD: [
                {
                    "id": "cloud-1",
                    "name": "C003-P-X",
                    "environment": "n",
                    "owner": {"org_id": "C003"},
                    "account": {"id": "acc-1", "name": "vaultex-aws"},
                    "parent": {"id": "region-1", "name": "eu-central-1"},
                    "design": {"name": "XL", "address_prefix_length": 25},
                }
            ]
        }

        asyncio.run(cloud(gen, payload))

        assert [(s["pool_name"], s["parent_pool_name"], s["prefix_length"]) for s in gen.sliced] == [
            ("c003-n-cloud-ipv4", "Tenant-NONPROD-IPv4", 25)
        ]

    @pytest.mark.parametrize("payload", [{}, {"TopologyCustomerDC": [{"id": "x"}]}, {_CLOUD: []}])
    def test_the_cloud_generator_ignores_a_response_without_its_kind(self, payload: dict[str, Any]) -> None:
        """Not this generator's kind: an info log, nothing carved."""
        gen = CustomerDeploymentCloudGenerator.__new__(CustomerDeploymentCloudGenerator)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.ensure_cloud_ranges = AsyncMock()

        asyncio.run(gen.generate(payload))

        gen.ensure_cloud_ranges.assert_not_awaited()

    def test_the_cloud_generator_refuses_a_footprint_without_an_id(self) -> None:
        """No id: the error is logged and nothing is carved."""
        gen = CustomerDeploymentCloudGenerator.__new__(CustomerDeploymentCloudGenerator)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.ensure_cloud_ranges = AsyncMock()

        asyncio.run(gen.generate({_CLOUD: [{"name": "C003-P-X"}]}))

        gen.logger.error.assert_called_once()
        gen.ensure_cloud_ranges.assert_not_awaited()
