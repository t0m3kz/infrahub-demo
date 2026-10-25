"""Unit tests for generators/helpers/routing_tag.py and the customer routing tag generator.

A tag is drawn from its kind's Number pool once: a node that has it writes nothing, a missing pool fails the run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.protocols import CoreNumberPool

from generators.helpers.routing_tag import (
    ensure_routing_tag,
    ensure_segment_routing_tag,
)
from generators.logger import GeneratorError
from generators.topology.customer_routing_tag import CustomerRoutingTagGenerator
from utils.limits import CUSTOMER_ROUTING_TAG_POOL, SEGMENT_ROUTING_TAG_POOL


def _client(*, pool: bool = True, drawn: int = 41) -> Any:
    """A client whose pool lookup returns a pool (or None) and whose created consumer reports ``drawn``."""
    client = MagicMock()
    client.get = AsyncMock(return_value=MagicMock(id="pool-1") if pool else None)
    consumers: list[tuple[str, dict[str, Any], Any]] = []

    async def _create(*, kind: str, data: dict[str, Any]) -> Any:
        consumer = MagicMock()
        consumer.save = AsyncMock()
        consumer.routing_tag.value = drawn
        consumers.append((kind, data, consumer))
        return consumer

    client.create = AsyncMock(side_effect=_create)
    client.consumers = consumers
    return client


def _segment(
    *, routing_tag: int | None = None, namespace_type: str | None = "prod", kind: str = "ManagedVxlanSegment"
) -> dict[str, Any]:
    """A cleaned segment as vxlan_segment.gql / application.gql select it (routed PROD tenant by default)."""
    segment: dict[str, Any] = {
        "id": "seg-1",
        "typename": kind,
        "name": "c005-web-p",
        "routing_tag": routing_tag,
        "security_zone": {"name": "PROD-ZONE"},
    }
    if namespace_type:
        segment["gateway"] = {"ip_prefix": {"ip_namespace": {"name": "PROD", "namespace_type": namespace_type}}}
    return segment


class TestEnsureRoutingTag:
    """One flow for both kinds: stored tag first, then the pool, then an untracked upsert by node id."""

    def test_node_that_has_its_tag_writes_nothing(self) -> None:
        """Fast path: no pool lookup, no write, the stored value comes back."""
        client = _client()

        tag = asyncio.run(
            ensure_routing_tag(
                client,
                kind="OrganizationCustomer",
                node={"id": "c-1", "routing_tag": 9},
                pool_name=CUSTOMER_ROUTING_TAG_POOL,
            )
        )

        assert tag == 9
        client.get.assert_not_awaited()
        client.create.assert_not_awaited()

    def test_missing_tag_is_drawn_from_the_named_pool_and_saved_untracked_by_id(self) -> None:
        """The upsert carries the node id and the pool id, never a value; the drawn number is recorded."""
        client = _client(drawn=41)
        node: dict[str, Any] = {"id": "c-1", "name": "Drentec BV", "routing_tag": None}

        tag = asyncio.run(
            ensure_routing_tag(client, kind="OrganizationCustomer", node=node, pool_name=CUSTOMER_ROUTING_TAG_POOL)
        )

        assert tag == 41
        assert node["routing_tag"] == 41
        assert client.get.await_args.kwargs == {
            "kind": CoreNumberPool,
            "name__value": CUSTOMER_ROUTING_TAG_POOL,
            "raise_when_missing": False,
        }
        ((kind, data, consumer),) = client.consumers
        assert kind == "OrganizationCustomer"
        assert data == {"id": "c-1", "routing_tag": {"from_pool": {"id": "pool-1"}}}
        consumer.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_pool_not_loaded_raises_and_writes_nothing(self) -> None:
        """The run fails rather than leaving the node untagged; the message names the pool and the file."""
        client = _client(pool=False)

        with pytest.raises(LookupError, match=r"Customer-Routing-Tag.*19c_routing_tag_pools\.yml"):
            asyncio.run(
                ensure_routing_tag(
                    client, kind="OrganizationCustomer", node={"id": "c-1"}, pool_name=CUSTOMER_ROUTING_TAG_POOL
                )
            )

        client.create.assert_not_awaited()

    def test_node_without_id_raises(self) -> None:
        """A node that was never saved cannot be the consumer of a pool draw."""
        client = _client()

        with pytest.raises(ValueError, match="needs an id"):
            asyncio.run(
                ensure_routing_tag(
                    client, kind="OrganizationCustomer", node={"name": "x"}, pool_name=CUSTOMER_ROUTING_TAG_POOL
                )
            )

        client.get.assert_not_awaited()

    def test_failed_save_propagates(self) -> None:
        """A save the server refuses is not swallowed: the node would be silently untagged."""
        client = _client()

        async def _create(*, kind: str, data: dict[str, Any]) -> Any:
            consumer = MagicMock()
            consumer.save = AsyncMock(side_effect=RuntimeError("pool exhausted"))
            return consumer

        client.create = AsyncMock(side_effect=_create)

        with pytest.raises(RuntimeError, match="pool exhausted"):
            asyncio.run(
                ensure_routing_tag(
                    client, kind="OrganizationCustomer", node={"id": "c-1"}, pool_name=CUSTOMER_ROUTING_TAG_POOL
                )
            )


class TestEnsureSegmentRoutingTag:
    """Only a routed tenant segment is tagged, from the segment pool, as its own kind."""

    def test_routed_tenant_segment_draws_from_the_segment_pool_as_its_own_kind(self) -> None:
        client = _client(drawn=7)
        segment = _segment(kind="ManagedVlanSegment")

        assert asyncio.run(ensure_segment_routing_tag(client, segment=segment)) == 7

        assert client.get.await_args.kwargs["name__value"] == SEGMENT_ROUTING_TAG_POOL
        ((kind, data, _),) = client.consumers
        assert (kind, data["id"]) == ("ManagedVlanSegment", "seg-1")
        assert segment["routing_tag"] == 7

    def test_segment_that_has_its_tag_writes_nothing(self) -> None:
        client = _client()

        assert asyncio.run(ensure_segment_routing_tag(client, segment=_segment(routing_tag=3))) == 3

        client.get.assert_not_awaited()
        client.create.assert_not_awaited()

    @pytest.mark.parametrize(
        ("namespace_type", "kind", "zone"),
        [
            pytest.param(None, "ManagedVxlanSegment", "PROD-ZONE", id="no-gateway"),
            pytest.param("management", "ManagedVlanSegment", "PROD-ZONE", id="management-namespace"),
            pytest.param("prod", "ManagedVxlanSegment", "MANAGEMENT-ZONE", id="exchange-zone"),
            pytest.param("prod", "ManagedExternalSegment", "PROD-ZONE", id="external-segment"),
            pytest.param("prod", "CloudNetworkSegment", "PROD-ZONE", id="cloud-segment"),
        ],
    )
    def test_segment_that_is_not_a_routed_tenant_segment_is_never_tagged(
        self, namespace_type: str | None, kind: str, zone: str
    ) -> None:
        client = _client()
        segment = _segment(namespace_type=namespace_type, kind=kind)
        segment["security_zone"] = {"name": zone}

        assert asyncio.run(ensure_segment_routing_tag(client, segment=segment)) is None

        client.get.assert_not_awaited()
        client.create.assert_not_awaited()
        assert segment["routing_tag"] is None


def _generator(client: Any) -> Any:
    gen = CustomerRoutingTagGenerator.__new__(CustomerRoutingTagGenerator)
    gen.client = client
    gen.logger = MagicMock()
    return gen


def _payload(routing_tag: int | None) -> dict[str, Any]:
    """The customer_routing_tag query response in its raw GraphQL shape."""
    return {
        "OrganizationCustomer": {
            "edges": [
                {
                    "node": {
                        "id": "c-1",
                        "name": {"value": "Drentec BV"},
                        "routing_tag": {"value": routing_tag},
                    }
                }
            ]
        }
    }


class TestCustomerRoutingTagGenerator:
    """add_customer_routing_tag: a customer without a tag draws one, a customer with one causes zero writes."""

    def test_customer_without_tag_draws_from_the_customer_pool(self) -> None:
        client = _client(drawn=5)

        asyncio.run(_generator(client).generate(_payload(None)))

        assert client.get.await_args.kwargs["name__value"] == CUSTOMER_ROUTING_TAG_POOL
        ((kind, data, consumer),) = client.consumers
        assert (kind, data["id"]) == ("OrganizationCustomer", "c-1")
        consumer.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_rerun_over_a_tagged_customer_writes_nothing(self) -> None:
        """Idempotent: the stored tag is the fast path, no lookup and no mutation."""
        client = _client()

        asyncio.run(_generator(client).generate(_payload(5)))

        client.get.assert_not_awaited()
        client.create.assert_not_awaited()

    def test_pool_not_loaded_fails_the_run(self) -> None:
        client = _client(pool=False)

        with pytest.raises(GeneratorError, match="Customer-Routing-Tag"):
            asyncio.run(_generator(client).generate(_payload(None)))

        client.create.assert_not_awaited()

    def test_empty_response_fails_the_run(self) -> None:
        with pytest.raises(GeneratorError, match="No OrganizationCustomer data"):
            asyncio.run(_generator(_client()).generate({"OrganizationCustomer": {"edges": []}}))

    def test_query_selects_the_stored_tag(self, root_dir: Path) -> None:
        """The fast path reads the tag from the query; without it every run would write."""
        query = (root_dir / "queries/topology/add/customer_routing_tag.gql").read_text()
        assert "routing_tag { value }" in query
        assert "OrganizationCustomer(name__value: $name)" in query

    def test_application_query_selects_the_segment_tag(self, root_dir: Path) -> None:
        """SegmentSecurityFields feeds the application generator's segments to the same helper."""
        query = (root_dir / "queries/topology/add/application.gql").read_text()
        fragment = query.split("fragment SegmentSecurityFields", 1)[1].split("\nfragment ", 1)[0]
        assert "routing_tag { value }" in fragment
