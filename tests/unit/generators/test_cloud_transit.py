"""A ``transit`` cloud footprint is attached to the hub of its region and environment (generators/cloud/cloud_hub.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from generators.cloud.cloud_hub import ROUTE_TABLE_KEY, TransitNetwork, plan_transit
from generators.customers.customer_cloud import CustomerDeploymentCloudGenerator
from generators.model.errors import GeneratorError
from generators.plan import AddEdgesSpec, CustomerSliceSpec, EdgesSpec, NodeRef, NodeSpec, TopologyPlan
from generators.reconciler import PlanReconciler, ReconcileResult
from tests.unit.cloud_range_helpers import CloudClient, attachment, network
from tests.unit.segment_customer_pool_helpers import make_gen
from utils.data_cleaning import clean_data
from utils.transit_hub import TransitHub, hub_candidates

FOOTPRINT = "C003-P-VAULTEX-AWS-EU-CENTRAL-1"


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _hub(
    name: str = "tgw-euc1", *, region: str = "region-1", environment: str = "p", links: bool = True
) -> dict[str, Any]:
    """A raw ``CloudGateway`` node of the customer_cloud query: a hub with its link towards us (a peer gateway)."""
    attachments = (
        [
            {
                "node": {
                    "id": f"{name}-link",
                    "name": _v(f"{name}-hybrid"),
                    "peer_gateway": {"node": {"id": "dxgw"}},
                    "associated_route_table": {"node": {"id": f"{name}-rt-onprem"}},
                }
            }
        ]
        if links
        else []
    )
    return {
        "id": f"gw-{name}",
        "name": _v(name),
        "gateway_type": _v("hub"),
        "region": {"node": {"id": region}},
        "account": {"node": {"id": "acc-hub", "environment": _v(environment)}},
        "attachments": {"edges": attachments},
    }


def _payload(connection: str = "transit", hubs: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    """The raw customer_cloud response for C003's footprint in eu-central-1."""
    footprint = {
        "id": "cloud-1",
        "name": _v(FOOTPRINT),
        "environment": _v("p"),
        "connection": _v(connection),
        "owner": {"node": {"id": "org-c003", "org_id": _v("C003")}},
        "parent": {"node": {"id": "region-1", "name": _v("eu-central-1")}},
        "account": {"node": {"id": "acc-1", "name": _v("vaultex-aws")}},
        "design": {"node": {"name": _v("S"), "address_prefix_length": _v(28)}},
    }
    return {
        "TopologyCustomerCloud": {"edges": [{"node": footprint}]},
        "CloudGateway": {"edges": [{"node": hub} for hub in hubs]},
    }


def _run(payload: dict[str, Any], client: CloudClient) -> tuple[Any, list[TopologyPlan]]:
    """Run the generator; the plan is captured instead of written (each slice answers a fake prefix)."""
    gen = make_gen(client, CustomerDeploymentCloudGenerator)
    plans: list[TopologyPlan] = []

    async def apply(plan: TopologyPlan) -> ReconcileResult:
        plans.append(plan)
        result = ReconcileResult()
        for spec in plan.of(CustomerSliceSpec):
            result.prefixes[spec.identifier] = SimpleNamespace(prefix=SimpleNamespace(value="10.0.0.0/29"))
        return result

    gen.apply_plan = apply
    asyncio.run(gen.generate(payload))
    return gen, plans


def _nodes(plan: TopologyPlan) -> dict[str, NodeSpec]:
    return {spec.key: spec for spec in plan.of(NodeSpec)}


class TestTransitBoarding:
    """Attachments and the footprint's route table on the hub, in the plan of the routed ranges."""

    def test_one_attachment_per_network_and_the_footprints_route_table(self) -> None:
        client = CloudClient([network("n1", "vpc-a"), network("n2", "vpc-b")], [])

        _gen, (plan,) = _run(_payload(hubs=(_hub(),)), client)

        nodes = _nodes(plan)
        table = nodes[ROUTE_TABLE_KEY]
        assert table.tracked and table.kind == "CloudGatewayRouteTable"
        assert table.data["name"] == f"rt-{FOOTPRINT.lower()}" and table.data["gateway"] == "gw-tgw-euc1"
        attachments = [spec for spec in plan.of(NodeSpec) if spec.kind == "CloudGatewayAttachment"]
        assert [spec.data["name"] for spec in attachments] == ["tgw-euc1-vpc-a", "tgw-euc1-vpc-b"]
        first = attachments[0]
        assert first.tracked
        assert (first.data["gateway"], first.data["virtual_network"], first.data["owner_account"]) == (
            "gw-tgw-euc1",
            "n1",
            "acc-1",
        )
        assert first.data["associated_route_table"] == NodeRef(ROUTE_TABLE_KEY)
        assert first.data["propagates_to_route_tables"] == [NodeRef(ROUTE_TABLE_KEY), "tgw-euc1-rt-onprem"]
        assert "allowed_prefixes" not in first.data
        plan.validate()

    def test_the_hub_link_propagates_into_the_footprints_table(self) -> None:
        client = CloudClient([network("n1", "vpc-a")], [])

        _gen, (plan,) = _run(_payload(hubs=(_hub(),)), client)

        (edge,) = plan.of(EdgesSpec)
        assert (edge.node, edge.relation, edge.peers) == (
            "tgw-euc1-link",
            "propagates_to_route_tables",
            (NodeRef(ROUTE_TABLE_KEY),),
        )

    def test_each_generated_attachment_gets_its_networks_slice(self) -> None:
        """The one writer of allowed_prefixes: an AddEdgesSpec on the planned attachment, after it is saved."""
        client = CloudClient([network("n1", "vpc-a")], [])

        _gen, (plan,) = _run(_payload(hubs=(_hub(),)), client)

        assert [spec.identifier for spec in plan.of(CustomerSliceSpec)] == ["vaultex-aws/vpc-a"]
        assert [(e.node_id, e.peer_prefix) for e in plan.of(AddEdgesSpec)] == [
            (NodeRef("transit-attachment-vpc-a"), "vaultex-aws/vpc-a")
        ]

    def test_a_generated_attachment_found_bare_on_a_rerun_gets_one_edge_only(self) -> None:
        """The read finds the generated attachment without its prefix: the edge goes through the plan key, once."""
        bare = attachment("a1", "n1")
        bare.name.value = "tgw-euc1-vpc-a"
        client = CloudClient([network("n1", "vpc-a")], [bare, attachment("a2", "n1")])

        _gen, (plan,) = _run(_payload(hubs=(_hub(),)), client)

        assert [e.node_id for e in plan.of(AddEdgesSpec)] == ["a2", NodeRef("transit-attachment-vpc-a")]

    def test_a_rerun_plans_the_same_objects(self) -> None:
        client = CloudClient([network("n1", "vpc-a")], [])

        _gen, (first,) = _run(_payload(hubs=(_hub(),)), client)
        _gen, (second,) = _run(_payload(hubs=(_hub(),)), client)

        assert first.specs == second.specs and first.root_id == second.root_id == "cloud-1"

    @pytest.mark.parametrize("connection", ["dedicated", "vpn"])
    def test_a_footprint_that_is_not_transit_plans_nothing_on_the_hub(self, connection: str) -> None:
        """No tracked attachment or table: the group cleanup removes what an earlier transit run created."""
        client = CloudClient([network("n1", "vpc-a")], [])

        _gen, (plan,) = _run(_payload(connection, hubs=(_hub(),)), client)

        assert plan.of(NodeSpec) == [] and plan.of(EdgesSpec) == [] and plan.of(AddEdgesSpec) == []
        assert plan.root_id == "cloud-1"

    def test_no_hub_fails_before_any_read_or_write(self) -> None:
        client = CloudClient([network("n1", "vpc-a")], [])

        with pytest.raises(GeneratorError, match="no hub gateway in region 'eu-central-1' and environment 'p'"):
            _run(_payload(hubs=(_hub(region="region-2"), _hub("tgw-n", environment="n"))), client)

        assert client.queries == []

    def test_two_hubs_fail_naming_both(self) -> None:
        client = CloudClient([], [])

        with pytest.raises(GeneratorError, match="2 hub gateways .*'tgw-a', 'tgw-b'"):
            _run(_payload(hubs=(_hub("tgw-b"), _hub("tgw-a"))), client)


class TestHubCandidates:
    """The hub of a footprint: region and account environment, from the cleaned query."""

    def test_a_hub_without_a_link_has_no_links(self) -> None:
        cleaned = clean_data(_payload(hubs=(_hub(links=False),)))

        (hub,) = hub_candidates(cleaned["TopologyCustomerCloud"][0], cleaned["CloudGateway"])

        assert hub.links == ()

    def test_a_plan_without_links_adds_no_edge(self) -> None:
        plan = TopologyPlan(root_id="cloud-1")

        plan_transit(plan, {"id": "cloud-1", "name": "X"}, TransitHub("gw", "tgw", ()), [TransitNetwork("n", "v")])

        assert plan.of(EdgesSpec) == []
        assert _nodes(plan)["transit-attachment-v"].data["propagates_to_route_tables"] == [NodeRef(ROUTE_TABLE_KEY)]


class TestSliceEdgeOnAPlannedNode:
    """The edges step resolves a NodeRef to the node the nodes step saved."""

    def test_the_node_ref_is_resolved(self) -> None:
        gen = MagicMock()
        reconciler = PlanReconciler(gen)
        result = ReconcileResult()
        result.nodes["att"] = SimpleNamespace(id="att-id")
        result.prefixes["slice"] = SimpleNamespace(id="prefix-id")
        plan = TopologyPlan(root_id=None)
        plan.add(AddEdgesSpec(node_id=NodeRef("att"), relation="allowed_prefixes", peer_prefix="slice"))

        with patch("generators.reconciler.add_edges", AsyncMock()) as add:
            asyncio.run(reconciler._add_edges(plan, result))

        add.assert_awaited_once_with(gen.client, node_id="att-id", relation="allowed_prefixes", peer_ids=["prefix-id"])

    def test_a_ref_to_a_node_the_plan_does_not_save_is_refused(self) -> None:
        plan = TopologyPlan(root_id=None)
        plan.add(CustomerSliceSpec(identifier="s", pool="p", global_pool="g", block_length=28, length=29))
        plan.add(AddEdgesSpec(node_id=NodeRef("ghost"), relation="allowed_prefixes", peer_prefix="s"))

        with pytest.raises(ValueError, match="ghost"):
            plan.validate()
