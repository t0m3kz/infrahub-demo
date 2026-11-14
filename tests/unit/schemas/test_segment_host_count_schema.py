"""host_count on a network segment"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators import protocols
from generators.pools import PoolMixin
from tests.integration.demo_data import ROOT, attribute

_QUERY = " ".join((ROOT / "queries" / "topology" / "add" / "vxlan_segment.gql").read_text().split())
_PROTOCOLS = Path(protocols.__file__).read_text()


@pytest.mark.parametrize("kind", ["ManagedNetworkSegment", "ManagedVxlanSegment", "CloudNetworkSegment"])
def test_host_count_is_an_optional_number_of_the_generic(kind: str) -> None:
    """Optional (no host_count is a /24), a Number, at least one host, declared once on the generic."""
    definition = attribute(kind, "host_count") or {}
    assert definition["kind"] == "Number" and definition["optional"] is True
    assert definition["parameters"]["min_value"] == 1
    assert "default_value" not in definition


def test_the_generator_query_reads_what_the_addressing_needs() -> None:
    """host_count, the owner's org_id, each footprint's kind and the design's block of a DC and a colocation one."""
    assert "host_count { value }" in _QUERY
    assert "ip_version { value }" in _QUERY
    assert "owner { node { id org_id { value } } }" in _QUERY
    assert "id __typename name { value }" in _QUERY
    for kind in ("TopologyCustomerDC", "TopologyCustomerColocation"):
        fragment = _QUERY.split(f"... on {kind} {{", 1)[1]
        # The design's block and segment lengths of both families (see generators/topology/segment_addressing.py).
        design = " ".join(fragment.lstrip().split("parent {", 1)[0].split())
        assert design == (
            "design { node { id name { value } address_prefix_length { value } segment_prefix_length { value } "
            "ipv6_prefix_length { value } ipv6_segment_prefix_length { value } } }"
        )
    assert _QUERY.count("__typename") == 2  # the segment and each footprint, once


@pytest.mark.parametrize(
    "declaration",
    [
        "class TopologyCustomerTemplateCloud(TopologyCustomerTemplate):\n    max_virtual_networks: Integer",
        "class TopologyCustomerTemplateColo(TopologyCustomerTemplate, TopologyCustomerTemplateSizing):",
        "    address_prefix_length: Integer\n    dedicated_firewall: Boolean",
        "    design: RelationshipAttribute[TopologyCustomerTemplateCloud]",
        "    design: RelationshipAttribute[TopologyCustomerTemplateColo]",
        "    design: RelationshipAttribute[TopologyCustomerTemplateDC]",
        "    host_count: IntegerOptional",
    ],
)
def test_the_protocols_name_the_new_classes_and_fields(declaration: str) -> None:
    """generators/protocols.py is hand-kept: the new template kinds, the generic's fields, the three designs."""
    assert declaration in _PROTOCOLS


def test_max_segments_moved_up_to_the_generic_in_the_protocols() -> None:
    """max_segments is the TopologyCustomerTemplate generic's, no template class repeats it."""
    generic = _PROTOCOLS.split("class TopologyCustomerTemplate(CoreNode):", 1)[1].split("\n\n\n", 1)[0]
    assert "max_segments: Integer" in generic
    nodes = re.findall(
        r"class TopologyCustomerTemplate\w+\(TopologyCustomerTemplate(?:, \w+)?\):\n((?:    .*\n)+)", _PROTOCOLS
    )
    assert len(nodes) == 3 and not any("max_segments" in body for body in nodes)


def test_a_sliced_block_can_carry_its_member_type_and_pool_flag() -> None:
    """member_type and is_pool reach the allocation of the slice; without them the call is what it was."""
    gen: Any = PoolMixin.__new__(PoolMixin)
    gen.client = MagicMock()
    gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock())
    pool = AsyncMock()
    gen.client.create = AsyncMock(return_value=pool)
    gen.logger = MagicMock()
    gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock())

    common: dict[str, Any] = {
        "pool_name": "c1-p-dc-ipv4",
        "parent_pool_name": "T",
        "prefix_length": 23,
        "role": "customer",
    }
    asyncio.run(gen.ensure_sliced_pool(**common, kind="prefix", member_type="prefix", is_pool=True))
    asyncio.run(gen.ensure_sliced_pool(**common, kind="prefix"))

    with_flags, without = (call.kwargs for call in gen.client.allocate_next_ip_prefix.await_args_list)
    assert with_flags["member_type"] == "prefix" and with_flags["data"] == {"role": "customer", "is_pool": True}
    assert "member_type" not in without and without["data"] == {"role": "customer"}


def test_the_component_generator_reads_the_same_addressing_inputs_and_shares_the_implementation() -> None:
    """add_app_component_segment addresses the segment too (before its reconcile): one implementation, the same
    inputs in its query, the gateway's prefix included (a changed host_count is detected from it)."""
    from generators.topology.app_instance_segment import AppInstanceSegmentGenerator
    from generators.topology.segment import VxlanSegmentGenerator
    from generators.topology.segment_addressing import SegmentAddressingMixin

    query = " ".join((ROOT / "queries" / "topology" / "add" / "app_component_segment.gql").read_text().split())
    assert "host_count { value }" in query and "owner { node { id org_id { value } } }" in query
    assert "ip_prefix { node { id prefix { value } } }" in query
    assert "... on ManagedVxlanSegment { customer_deployments { edges { node { id __typename name { value } " in query
    for generator in (AppInstanceSegmentGenerator, VxlanSegmentGenerator):
        assert generator.ensure_segment_addressing is SegmentAddressingMixin.ensure_segment_addressing
