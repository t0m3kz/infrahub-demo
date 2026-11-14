"""The routed range of a cloud virtual network: one /29 slice of the footprint's pool, written on its attachments."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.colocation.cloud_attachment_range import CloudAttachmentRangeGenerator
from generators.customers.customer_cloud import CustomerDeploymentCloudGenerator
from tests.unit.cloud_range_helpers import ACCOUNT, REGION_ID, CloudClient, attachment, footprint, network
from tests.unit.segment_customer_pool_helpers import make_gen
from utils.limits import CLOUD_RANGE_PREFIX_LENGTH


def _run(client: CloudClient, **overrides: Any) -> Any:
    gen = make_gen(client, CustomerDeploymentCloudGenerator)
    asyncio.run(gen.ensure_cloud_ranges(footprint(**overrides)))
    return gen


def _drawn(client: CloudClient) -> list[tuple[str, int]]:
    """(identifier, prefix length) of the slices drawn from the customer's cloud pool (not the pool's own block)."""
    return [
        (a["identifier"], a["prefix_length"])
        for a in client.allocations
        if a["resource_pool"].name.value.startswith("c003")
    ]


class TestAllocation:
    """A slice per virtual network that has an attachment, from the footprint's pool."""

    def test_one_slice_per_network_keyed_by_account_and_network(self) -> None:
        """Two networks, each with an attachment: two /29 from c003-p-cloud-ipv4, one edge on each attachment."""
        client = CloudClient(
            [network("n1", "vpc-a"), network("n2", "vpc-b")], [attachment("a1", "n1"), attachment("a2", "n2")]
        )

        _run(client)

        assert _drawn(client) == [("vaultex-aws/vpc-a", 29), ("vaultex-aws/vpc-b", 29)]
        assert CLOUD_RANGE_PREFIX_LENGTH == 29
        assert [(e["id"], e["name"]) for e in client.edges] == [("a1", "allowed_prefixes"), ("a2", "allowed_prefixes")]
        assert client.edges[0]["nodes"] != client.edges[1]["nodes"]

    def test_the_pool_is_the_footprints_and_the_footprint_is_boarded_first(self) -> None:
        """The customer pool is created from the tier's block (S /28) before any slice is drawn from it."""
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1")])

        gen = _run(client)

        assert [(s["pool_name"], s["prefix_length"]) for s in gen.sliced] == [("c003-p-cloud-ipv4", 28)]
        assert gen.locks == ["customer-pool-c003-p-cloud-ipv4", "customer-pool-c003-p-cloud-ipv4"]

    def test_attachments_of_one_network_share_its_one_slice(self) -> None:
        """Two bare attachments to one network: one draw, the same prefix on both."""
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1"), attachment("a2", "n1")])

        _run(client)

        assert _drawn(client) == [("vaultex-aws/vpc-a", 29)]
        assert [e["id"] for e in client.edges] == ["a1", "a2"]
        assert client.edges[0]["nodes"] == client.edges[1]["nodes"]

    def test_a_network_with_no_attachment_gets_no_slice(self) -> None:
        """Nothing announces its range, so no address is spent."""
        client = CloudClient([network("n1", "vpc-a"), network("n2", "vpc-b")], [attachment("a1", "n1")])

        _run(client)

        assert _drawn(client) == [("vaultex-aws/vpc-a", 29)]

    def test_only_the_networks_of_the_footprints_account_and_region_are_read(self) -> None:
        """The network query is filtered by the footprint's account and region, the attachment query by those networks."""
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1")])

        _run(client)

        assert client.queries[0] == (
            "CloudVirtualNetwork",
            {"account__ids": [ACCOUNT["id"]], "region__ids": [REGION_ID]},
        )
        kind, kwargs = client.queries[1]
        assert kind == "CloudGatewayAttachment" and kwargs["virtual_network__ids"] == ["n1"]

    def test_no_network_means_no_attachment_query_and_no_slice(self) -> None:
        """The pool is boarded; nothing else is read or drawn."""
        client = CloudClient([], [])

        _run(client)

        assert [kind for kind, _ in client.queries] == ["CloudVirtualNetwork"]
        assert _drawn(client) == [] and client.edges == []

    def test_a_footprint_without_account_or_region_is_logged_and_skipped(self) -> None:
        """No account to look networks up by: a warning, the pool is still boarded."""
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1")])

        gen = _run(client, account=None)

        gen.logger.warning.assert_called_once()
        assert client.queries == [] and client.edges == []


class TestAuthoredWins:
    """An attachment that already has allowed_prefixes is never written."""

    def test_an_attachment_with_prefixes_is_left_alone(self) -> None:
        """Authored prefixes: no draw, no edge."""
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1", prefixes=("authored-1",))])

        _run(client)

        assert _drawn(client) == [] and client.edges == []

    def test_a_bare_sibling_still_gets_the_slice(self) -> None:
        """One attachment authored, one bare, same network: the slice goes on the bare one only."""
        client = CloudClient(
            [network("n1", "vpc-a")], [attachment("a1", "n1", prefixes=("authored-1",)), attachment("a2", "n1")]
        )

        _run(client)

        assert [e["id"] for e in client.edges] == ["a2"]


class TestIdempotency:
    """A second run draws the same slice and adds no duplicate edge."""

    def test_the_same_identifier_returns_the_same_prefix(self) -> None:
        """Re-run before the edge is visible: the draw returns the same prefix, the same edge is added."""
        client = CloudClient([network("n1", "vpc-a")], [attachment("a1", "n1")])

        _run(client)
        _run(client)

        assert client.edges[0] == client.edges[1]
        assert len(client.known) == 1

    def test_once_written_the_attachment_has_prefixes_and_nothing_more_happens(self) -> None:
        """After the first run the attachment lists the slice: the second run draws nothing and adds no edge."""
        bare = attachment("a1", "n1")
        client = CloudClient([network("n1", "vpc-a")], [bare])
        _run(client)
        bare.allowed_prefixes.peer_ids = [client.edges[0]["nodes"][0]["id"]]
        drawn, edges = len(client.allocations), len(client.edges)

        _run(client)

        assert (len(client.allocations), len(client.edges)) == (drawn, edges)


class TestAttachmentGenerator:
    """add_cloud_attachment_range runs the same function for the footprint of the network's account."""

    @staticmethod
    def _generator(footprints: list[Any]) -> Any:
        gen: Any = CloudAttachmentRangeGenerator.__new__(CloudAttachmentRangeGenerator)
        gen.logger = MagicMock()
        gen.client = MagicMock()
        gen.client.filters = AsyncMock(return_value=footprints)
        gen.ensure_cloud_ranges = AsyncMock()
        return gen

    @staticmethod
    def _footprint_node() -> MagicMock:
        node = MagicMock(id="cloud-1")
        node.name.value = "C003-P-X"
        node.environment.value = "p"
        node.owner.peer.org_id.value = "C003"
        node.design.peer.name.value = "S"
        node.design.peer.address_prefix_length.value = 28
        node.parent.id = REGION_ID
        return node

    @staticmethod
    def _data(**network: Any) -> dict[str, Any]:
        virtual_network = {
            "node": {
                "id": "n1",
                "name": {"value": "vpc-a"},
                "account": {"node": {"id": "acc-1", "name": {"value": "vaultex-aws"}}},
            }
        }
        return {
            "CloudGatewayAttachment": {
                "edges": [
                    {
                        "node": {
                            "id": "a1",
                            "name": {"value": "tgwa"},
                            "virtual_network": network.get("vn", virtual_network),
                        }
                    }
                ]
            }
        }

    def test_the_footprint_of_the_networks_account_is_ranged(self) -> None:
        """The footprint is found by the account of the attachment's network and handed to ensure_cloud_ranges."""
        gen = self._generator([self._footprint_node()])

        asyncio.run(gen.generate(self._data()))

        kwargs = gen.client.filters.await_args.kwargs
        assert kwargs["account__ids"] == ["acc-1"]
        gen.ensure_cloud_ranges.assert_awaited_once_with(
            {
                "id": "cloud-1",
                "name": "C003-P-X",
                "environment": "p",
                "owner": {"org_id": "C003"},
                "design": {"name": "S", "address_prefix_length": 28},
                "account": ACCOUNT | {"name": "vaultex-aws", "id": "acc-1"},
                "parent": {"id": REGION_ID},
            }
        )
        gen.client.group_context.related_node_ids.append.assert_called_once_with("a1")

    def test_an_attachment_to_a_gateway_is_left_alone(self) -> None:
        """No virtual network: logged, no footprint lookup, nothing ranged."""
        gen = self._generator([self._footprint_node()])

        asyncio.run(gen.generate(self._data(vn={"node": None})))

        gen.client.filters.assert_not_awaited()
        gen.ensure_cloud_ranges.assert_not_awaited()

    def test_a_network_of_an_account_without_a_footprint_is_left_alone(self) -> None:
        """A hub account has no footprint: logged, nothing ranged."""
        gen = self._generator([])

        asyncio.run(gen.generate(self._data()))

        gen.ensure_cloud_ranges.assert_not_awaited()

    def test_a_response_without_the_kind_is_ignored(self) -> None:
        """Not this generator's kind: an info log."""
        gen = self._generator([])

        asyncio.run(gen.generate({}))

        gen.ensure_cloud_ranges.assert_not_awaited()
