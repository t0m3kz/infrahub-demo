"""Unit tests for the pure planning of the load balancer VIP producer.

- generators/helpers/lb_vip.py: ports, serving contexts, leg prefix, identifiers
- queries/topology/add/application.gql: the serving context path and its re-read queries
- load_balanced_components_in: what a deployment moving to another serving
  context re-runs (add_customer_deployment_dc's relink fan-out)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.helpers.lb_vip import leg_prefix_id, member_identifier, serving_contexts, vip_identifier, vip_ports
from generators.lb_vip import load_balanced_components_in
from generators.protocols import AppComponent, DcimPhysicalDevice, DcimVirtualDevice
from tests.unit.lb_vip_helpers import _context, _instance

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
            {"instances": [_instance("vm-1", b), _instance("vm-2", a), _instance("vm-3", b)]}
        )

        assert errors == []
        assert list(contexts) == ["ctx-a", "ctx-b"]
        assert [instance["id"] for instance in contexts["ctx-b"][1]] == ["vm-1", "vm-3"]

    def test_instance_without_a_serving_context_is_an_error(self) -> None:
        contexts, errors = serving_contexts({"fqdn": "web", "instances": [_instance("vm-1", None)]})

        assert contexts == {}
        assert "vm-1" in errors[0]
        assert "serving load balancer context" in errors[0]

    def test_component_without_instances_is_an_error(self) -> None:
        assert serving_contexts({"fqdn": "web", "instances": []})[1] == ["web is load_balanced but has no instances"]


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


def test_application_query_reads_the_serving_context_of_instances_and_targets(root_dir: Path) -> None:
    """Both device kinds carry the path; the dependency targets read it through the same fragment."""
    query = (root_dir / "queries/topology/add/application.gql").read_text()
    fragment = query.split("fragment AppInstanceFields", 1)[1].split("fragment AppComponentSecurityFields", 1)[0]
    assert fragment.count("serving_loadbalancer_context") == 2
    assert fragment.count("vip_prefix { node { id ip_namespace { node { id } } } }") == 2
    assert fragment.count("ip_address { node { ip_prefix { node { id } } } }") == 2
    target = query.split("target {", 1)[1]
    assert "load_balanced { value }" in target
    assert "instances { edges { node { ...AppInstanceFields } } }" in target
    segment = query.split("fragment SegmentSecurityFields", 1)[1].split("fragment AppInstanceFields", 1)[0]
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
    """The components a relinked deployment re-runs: load_balanced, with an instance deployed to it."""

    @staticmethod
    def _client(devices: dict[str, list[str]], component_ids: list[str]) -> MagicMock:
        """``devices``: device ids by kind name, as filters(kind=..., deployment__ids=...) returns them."""

        async def filters(*, kind: Any, **kwargs: Any) -> list[MagicMock]:
            if kind is AppComponent:
                return [MagicMock(id=component_id) for component_id in component_ids]
            return [MagicMock(id=device_id) for device_id in devices.get(kind.__name__, [])]

        client = MagicMock()
        client.filters = AsyncMock(side_effect=filters)
        return client

    def test_components_running_on_the_deployment_devices(self) -> None:
        """Physical and virtual devices of the deployment, then the load_balanced components on them; sorted, unique."""
        client = self._client(
            {"DcimPhysicalDevice": ["srv-2"], "DcimVirtualDevice": ["vm-1"]}, ["comp-b", "comp-a", "comp-b"]
        )

        assert asyncio.run(load_balanced_components_in(client, "dep-1")) == ["comp-a", "comp-b"]

        calls = [call.kwargs for call in client.filters.await_args_list]
        assert calls == [
            {"kind": DcimPhysicalDevice, "deployment__ids": ["dep-1"]},
            {"kind": DcimVirtualDevice, "deployment__ids": ["dep-1"]},
            {"kind": AppComponent, "instances__ids": ["srv-2", "vm-1"], "load_balanced__value": True},
        ]

    def test_deployment_without_devices_reads_no_components(self) -> None:
        client = self._client({}, ["comp-a"])

        assert asyncio.run(load_balanced_components_in(client, "dep-1")) == []

        assert all(call.kwargs["kind"] is not AppComponent for call in client.filters.await_args_list)
