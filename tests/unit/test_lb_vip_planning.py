"""Unit tests for the pure planning of the load balancer VIP producer.

- generators/helpers/lb_vip.py: ports, serving contexts, leg prefix, identifiers
- queries/topology/add/application.gql: the serving context path and its re-read queries
- load_balanced_components_in: what a deployment moving to another serving
  context re-runs (add_customer_deployment_dc's relink fan-out)
- instance_footprint: the context of an instance is its segment's footprint in the instance's data center
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.lb_vip import (
    instance_dc_id,
    instance_footprint,
    leg_prefix_id,
    member_identifier,
    segment_footprints,
    serving_contexts,
    vip_identifier,
    vip_ports,
)
from generators.lb_vip import load_balanced_components_in
from generators.protocols import AppComponent, ManagedVlanSegment, ManagedVxlanSegment
from tests.unit.lb_vip_helpers import _context, _instance, _with_footprints

# ===========================================================================
# Pure planning
# ===========================================================================


class TestVipPorts:
    def test_single_ports_each_get_a_vip(self) -> None:
        ports, errors = vip_ports({"fqdn": "web", "ports": ["tcp/443", "udp/53", "tcp/443"]})

        assert ports == [("tcp", 443, None), ("udp", 53, None)]
        assert errors == []

    def test_range_is_refused(self) -> None:
        ports, errors = vip_ports({"fqdn": "web", "ports": ["tcp/443", "udp/30000-30010"]})

        assert ports == [("tcp", 443, None)]
        assert len(errors) == 1
        assert "udp/30000-30010" in errors[0]

    def test_unparsable_port_is_an_error_not_a_guess(self) -> None:
        ports, errors = vip_ports({"fqdn": "web", "ports": ["garbage"]})

        assert ports == []
        assert "garbage" in errors[0]

    def test_no_port_is_an_error(self) -> None:
        assert vip_ports({"fqdn": "web", "ports": []})[1] == ["web is load_balanced but lists no port"]


class TestServingContexts:
    def test_instances_are_grouped_by_context(self) -> None:
        a, b = _context("ctx-a"), _context("ctx-b")
        contexts, errors = serving_contexts(
            _with_footprints({"instances": [_instance("vm-1", b), _instance("vm-2", a), _instance("vm-3", b)]})
        )

        assert errors == []
        assert list(contexts) == ["ctx-a", "ctx-b"]
        assert [instance["id"] for instance in contexts["ctx-b"][1]] == ["vm-1", "vm-3"]

    def test_instance_without_a_serving_context_is_an_error(self) -> None:
        contexts, errors = serving_contexts(_with_footprints({"fqdn": "web", "instances": [_instance("vm-1", None)]}))

        assert contexts == {}
        assert "vm-1" in errors[0]
        assert "serving load balancer context" in errors[0]

    def test_instance_in_a_data_center_the_segment_is_not_activated_in_is_an_error(self) -> None:
        component = _with_footprints(
            {"fqdn": "web", "instances": [_instance("vm-1", _context()), _instance("vm-2", None, footprint=False)]}
        )

        contexts, errors = serving_contexts(component)

        assert list(contexts) == ["ctx-1"]
        assert len(errors) == 1 and "vm-2" in errors[0]

    def test_shared_physical_host_is_served_by_the_footprint_of_its_segment_in_its_data_center(self) -> None:
        """A host deployed to the DC (or a pod of it) names no customer: the component's segment does."""
        context = _context("ctx-dc10")
        footprint = {"id": "fp-1", "typename": "TopologyCustomerDC", "parent": {"id": "dc-10"}}
        component = {
            "fqdn": "web",
            "network_segment": {"customer_deployments": [{**footprint, "serving_loadbalancer_context": context}]},
            "instances": [
                {
                    "id": "srv-1",
                    "typename": "DcimPhysicalDevice",
                    "deployment": {"id": "dc-10", "typename": "TopologyDataCenter"},
                },
                {
                    "id": "srv-2",
                    "typename": "DcimPhysicalDevice",
                    "deployment": {"id": "pod-1", "typename": "TopologyPod", "parent": {"id": "dc-10"}},
                },
            ],
        }

        contexts, errors = serving_contexts(component)

        assert errors == []
        assert [instance["id"] for instance in contexts["ctx-dc10"][1]] == ["srv-1", "srv-2"]

    def test_component_without_instances_is_an_error(self) -> None:
        assert serving_contexts({"fqdn": "web", "instances": []})[1] == ["web is load_balanced but has no instances"]


class TestSegmentFootprints:
    def test_a_vxlan_segment_lists_its_footprints_by_data_center_and_a_vlan_one_has_the_one(self) -> None:
        dc10 = {"id": "fp-10", "typename": "TopologyCustomerDC", "parent": {"id": "dc-10"}}
        dc12 = {"id": "fp-12", "typename": "TopologyCustomerDC", "parent": {"id": "dc-12"}}

        assert segment_footprints({"customer_deployments": [dc10, dc12]}) == {"dc-10": dc10, "dc-12": dc12}
        assert segment_footprints({"customer_deployment": dc10}) == {"dc-10": dc10}

    def test_colocation_office_and_parentless_footprints_serve_no_context(self) -> None:
        colocation = {"id": "fp-1", "typename": "TopologyCustomerColocation", "parent": {"id": "fr"}}
        parentless = {"id": "fp-2", "typename": "TopologyCustomerDC"}

        assert segment_footprints({"customer_deployments": [colocation, parentless]}) == {}
        assert segment_footprints({}) == {}

    def test_instance_data_center_is_the_host_dc_the_pods_parent_or_the_footprints_parent(self) -> None:
        assert instance_dc_id({"deployment": {"id": "dc-10", "typename": "TopologyDataCenter"}}) == "dc-10"
        pod = {"id": "pod-1", "typename": "TopologyPod", "parent": {"id": "dc-10"}}
        assert instance_dc_id({"deployment": pod}) == "dc-10"
        footprint = {"id": "fp-1", "typename": "TopologyCustomerDC", "parent": {"id": "dc-10"}}
        assert instance_dc_id({"deployment": footprint}) == "dc-10"
        assert instance_dc_id({}) is None

    def test_an_instance_without_a_data_center_has_no_footprint(self) -> None:
        component = {"network_segment": {"customer_deployments": [{"id": "fp", "typename": "TopologyCustomerDC"}]}}

        assert instance_footprint(component, {"id": "vm-1"}) is None


class TestLegPrefix:
    def test_prefix_of_the_member_addresses(self) -> None:
        assert leg_prefix_id(_context(leg="leg-9")) == "leg-9"

    def test_unwired_context_has_none(self) -> None:
        assert leg_prefix_id(_context(leg=None)) is None

    def test_two_prefixes_are_ambiguous(self) -> None:
        context = _context(leg="leg-1")
        context["capabilities"].append({"ip_address": {"ip_prefix": {"id": "leg-2"}}})

        assert leg_prefix_id(context) is None


def test_identifiers_follow_the_contract() -> None:
    """VIP /32: {component_id}-{protocol}-{port}-vip; a member is per (instance, segment)."""
    assert vip_identifier("comp-web", ("tcp", 443, None)) == "comp-web-tcp-443-vip"
    assert member_identifier("vm-1", "seg-web") == "vm-1-seg-web-lb-member"


# ===========================================================================
# Query
# ===========================================================================


def test_application_query_reads_the_serving_context_from_the_segment_and_the_data_center_from_the_instance(
    root_dir: Path,
) -> None:
    """The context sits on the segment's footprints (the dependency targets read it through the same fragment); an
    instance only names its data center, whatever its device kind."""
    query = (root_dir / "queries/topology/add/application.gql").read_text()
    instance = query.split("fragment AppInstanceFields", 1)[1].split("fragment AppComponentSecurityFields", 1)[0]
    assert "serving_loadbalancer_context" not in instance
    physical = instance.split("... on DcimPhysicalDevice", 1)[1].split("... on DcimVirtualDevice", 1)[0]
    virtual = instance.split("... on DcimVirtualDevice", 1)[1].split("... on CloudInstance", 1)[0]
    assert "... on TopologyPod { parent { node { id __typename } } }" in physical
    assert "... on TopologyCustomerDC { parent { node { id __typename } } }" in virtual
    segment = query.split("fragment SegmentSecurityFields", 1)[1].split("# An instance and the data center", 1)[0]
    assert "... on ManagedVxlanSegment {\n    customer_deployments {" in segment
    assert "... on ManagedVlanSegment {\n    customer_deployment {" in segment
    assert segment.count("serving_loadbalancer_context") == 2
    assert segment.count("vip_prefix { node { id ip_namespace { node { id } } } }") == 2
    assert segment.count("ip_address { node { ip_prefix { node { id } } } }") == 2
    assert segment.count("parent { node { id __typename } }") == 2
    target = query.split("target {", 1)[1]
    assert "load_balanced { value }" in target
    assert "instances { edges { node { ...AppInstanceFields } } }" in target
    assert "ip_prefix {\n        node {\n          id" in segment


def test_re_read_queries_select_what_the_application_query_does(root_dir: Path) -> None:
    """A re-read replaces the payload's dict: it must carry every field the producer reads from it."""
    application = (root_dir / "queries/topology/add/application.gql").read_text()
    contexts = (root_dir / "queries/topology/add/lb_serving_contexts.gql").read_text()
    gateways = " ".join((root_dir / "queries/topology/add/lb_segment_gateways.gql").read_text().split())
    for line in (
        "vip_prefix { node { id ip_namespace { node { id } } } }",
        "edges { node { __typename ... on DcimVirtualInterface { ip_address { node { ip_prefix { node { id } } } } } } }",
    ):
        assert line in application
        assert line in contexts
    assert "ManagedVxlanSegment(ids: $ids)" in gateways
    assert "prefix { value } ip_namespace { node { id name { value } namespace_type { value } } }" in gateways


class TestLoadBalancedComponentsIn:
    """The components a relinked deployment re-runs: load_balanced, on a segment activated in it."""

    @staticmethod
    def _client(vxlan: list[str], vlan: list[str], component_ids: list[str]) -> MagicMock:
        """``vxlan`` / ``vlan``: the segment ids filters(...customer_deployment(s)__ids=...) returns."""

        async def filters(*, kind: Any, **kwargs: Any) -> list[MagicMock]:
            if kind is AppComponent:
                return [MagicMock(id=component_id) for component_id in component_ids]
            return [MagicMock(id=segment_id) for segment_id in (vxlan if kind is ManagedVxlanSegment else vlan)]

        client = MagicMock()
        client.filters = AsyncMock(side_effect=filters)
        return client

    def test_components_on_the_segments_of_the_deployment(self) -> None:
        """VXLAN and VLAN segments of the footprint, then the load_balanced components on them; sorted, unique. The
        instances are not read: a shared physical host and a virtual machine are served the same way."""
        client = self._client(["seg-2"], ["seg-1"], ["comp-b", "comp-a", "comp-b"])

        assert asyncio.run(load_balanced_components_in(client, "dep-1")) == ["comp-a", "comp-b"]

        calls = [call.kwargs for call in client.filters.await_args_list]
        assert {"kind": ManagedVxlanSegment, "customer_deployments__ids": ["dep-1"]} in calls
        assert {"kind": ManagedVlanSegment, "customer_deployment__ids": ["dep-1"]} in calls
        assert calls[-1] == {
            "kind": AppComponent,
            "network_segment__ids": ["seg-1", "seg-2"],
            "load_balanced__value": True,
        }

    def test_deployment_without_segments_reads_no_components(self) -> None:
        client = self._client([], [], ["comp-a"])

        assert asyncio.run(load_balanced_components_in(client, "dep-1")) == []

        assert all(call.kwargs["kind"] is not AppComponent for call in client.filters.await_args_list)
