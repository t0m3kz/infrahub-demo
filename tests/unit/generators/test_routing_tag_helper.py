"""Unit tests for generators/helpers/routing_tag.py (the specs), the routing tag step of the reconciler and the customer
routing tag generator."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.protocols import CoreNumberPool

from generators.customers.customer_routing_tag import CustomerRoutingTagGenerator
from generators.helpers.routing_tag import routing_tag_spec, segment_routing_tag_spec
from generators.model.errors import GeneratorError
from generators.plan import RoutingTagSpec, TopologyPlan
from generators.reconciler import PlanReconciler
from utils.limits import CUSTOMER_ROUTING_TAG_POOL, SEGMENT_ROUTING_TAG_POOL


def _selects_routing_tag(tree: dict[str, Any]) -> bool:
    """Whether a ``routing_tag`` field with a ``value`` is selected anywhere in the query tree."""
    if "value" in tree.get("routing_tag", {}):
        return True
    return any(_selects_routing_tag(subtree) for subtree in tree.values())


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
    *, routing_tag: int | None = None, vrf_type: str | None = "prod", kind: str = "ManagedVxlanSegment"
) -> dict[str, Any]:
    """A cleaned segment as vxlan_segment.gql / application.gql select it (routed PROD tenant by default)."""
    segment: dict[str, Any] = {
        "id": "seg-1",
        "typename": kind,
        "name": "c005-web-p",
        "routing_tag": routing_tag,
        "security_zone": {"name": "PROD", "vrf_type": vrf_type, "l3_vni": 60000},
    }
    if vrf_type:
        segment["gateway"] = {"ip_prefix": {"ip_namespace": {"name": "default"}}}
    return segment


class TestRoutingTagSpec:
    """A node with a tag has no spec; one without has a spec by node id, never a value."""

    def test_node_that_has_its_tag_has_no_spec(self) -> None:
        assert (
            routing_tag_spec(kind="OrganizationCustomer", node={"id": "c-1", "routing_tag": 9}, pool_name="P") is None
        )

    def test_missing_tag_is_a_spec_naming_the_node_kind_and_pool(self) -> None:
        spec = routing_tag_spec(kind="OrganizationCustomer", node={"id": "c-1", "routing_tag": None}, pool_name="P")

        assert spec == RoutingTagSpec(kind="OrganizationCustomer", node_id="c-1", pool_name="P")

    def test_node_without_id_raises(self) -> None:
        with pytest.raises(ValueError, match="needs an id"):
            routing_tag_spec(kind="OrganizationCustomer", node={"name": "x"}, pool_name="P")


class TestRoutingTagStep:
    """The reconciler draws the number: pool by name, untracked upsert by node id, recorded under the node id."""

    def test_missing_tag_is_drawn_from_the_named_pool_and_saved_untracked_by_id(self) -> None:
        client = _client(drawn=41)
        gen = _generator(client)

        result = asyncio.run(
            PlanReconciler(gen).apply(
                TopologyPlan(
                    root_id=None, specs=[RoutingTagSpec("OrganizationCustomer", "c-1", CUSTOMER_ROUTING_TAG_POOL)]
                )
            )
        )

        assert result.routing_tags == {"c-1": 41}
        assert client.get.await_args.kwargs == {
            "kind": CoreNumberPool,
            "name__value": CUSTOMER_ROUTING_TAG_POOL,
            "raise_when_missing": False,
        }
        ((kind, data, consumer),) = client.consumers
        assert kind == "OrganizationCustomer"
        assert data == {"id": "c-1", "routing_tag": {"from_pool": {"id": "pool-1"}}}
        consumer.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)

    def test_pool_not_loaded_fails_the_run_and_writes_nothing(self) -> None:
        client = _client(pool=False)

        with pytest.raises(GeneratorError, match=r"Customer-Routing-Tag.*19c_routing_tag_pools\.yml"):
            asyncio.run(
                PlanReconciler(_generator(client)).apply(
                    TopologyPlan(
                        root_id=None, specs=[RoutingTagSpec("OrganizationCustomer", "c-1", CUSTOMER_ROUTING_TAG_POOL)]
                    )
                )
            )

        client.create.assert_not_awaited()

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
                PlanReconciler(_generator(client)).apply(
                    TopologyPlan(root_id=None, specs=[RoutingTagSpec("OrganizationCustomer", "c-1", "P")])
                )
            )

    def test_the_plan_without_a_root_tracks_nothing(self) -> None:
        client = _client()
        client.group_context.related_node_ids = []

        asyncio.run(
            PlanReconciler(_generator(client)).apply(
                TopologyPlan(root_id=None, specs=[RoutingTagSpec("OrganizationCustomer", "c-1", "P")])
            )
        )

        assert client.group_context.related_node_ids == []


class TestSegmentRoutingTagSpec:
    """Only a routed tenant segment is tagged, from the segment pool, as its own kind."""

    def test_routed_tenant_segment_draws_from_the_segment_pool_as_its_own_kind(self) -> None:
        spec = segment_routing_tag_spec(_segment(kind="ManagedVxlanSegment"))

        assert spec == RoutingTagSpec("ManagedVxlanSegment", "seg-1", SEGMENT_ROUTING_TAG_POOL)

    def test_segment_that_has_its_tag_has_no_spec(self) -> None:
        assert segment_routing_tag_spec(_segment(routing_tag=3)) is None

    @pytest.mark.parametrize(
        ("vrf_type", "kind", "zone"),
        [
            pytest.param(None, "ManagedVxlanSegment", "PROD", id="no-gateway"),
            pytest.param("management", "ManagedVxlanSegment", "PROD", id="management-namespace"),
            pytest.param("prod", "ManagedVxlanSegment", "MANAGEMENT", id="exchange-zone"),
            pytest.param("prod", "CloudNetworkSegment", "PROD", id="cloud-segment"),
        ],
    )
    def test_segment_that_is_not_a_routed_tenant_segment_is_never_tagged(
        self, vrf_type: str | None, kind: str, zone: str
    ) -> None:
        segment = _segment(vrf_type=vrf_type, kind=kind)
        segment["security_zone"] = {"name": zone}

        assert segment_routing_tag_spec(segment) is None


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
