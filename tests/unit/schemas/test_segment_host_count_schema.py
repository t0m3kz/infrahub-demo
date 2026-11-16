"""host_count on a network segment"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators import protocols
from generators.ipam.pools import ensure_sliced_pool
from tests.integration.demo_data import attribute

_PROTOCOLS = Path(protocols.__file__).read_text()


@pytest.mark.parametrize("kind", ["ManagedNetworkSegment", "ManagedVxlanSegment", "CloudNetworkSegment"])
def test_host_count_is_an_optional_number_of_the_generic(kind: str) -> None:
    """Optional (no host_count is a /24), a Number, at least one host, declared once on the generic."""
    definition = attribute(kind, "host_count") or {}
    assert definition["kind"] == "Number" and definition["optional"] is True
    assert definition["parameters"]["min_value"] == 1
    assert "default_value" not in definition


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
    client = MagicMock()
    client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock())
    client.create = AsyncMock(return_value=AsyncMock())

    common: dict[str, Any] = {
        "pool_name": "c1-p-dc-ipv4",
        "prefix_length": 23,
        "role": "customer",
    }
    for extra in ({"member_type": "prefix", "is_pool": True}, {}):
        asyncio.run(ensure_sliced_pool(client, MagicMock(), MagicMock(), **common, kind="prefix", **extra))

    with_flags, without = (call.kwargs for call in client.allocate_next_ip_prefix.await_args_list)
    assert with_flags["member_type"] == "prefix" and with_flags["data"] == {"role": "customer", "is_pool": True}
    assert "member_type" not in without and without["data"] == {"role": "customer"}


def test_the_component_generator_shares_the_addressing_implementation() -> None:
    """add_app_component_segment addresses the segment too (before its reconcile): one implementation."""
    from generators.applications.app_instance_segment import AppInstanceSegmentGenerator
    from generators.segments.segment import VxlanSegmentGenerator
    from generators.segments.segment_addressing import ensure_segment_addressing

    for generator in (AppInstanceSegmentGenerator, VxlanSegmentGenerator):
        assert generator.ensure_segment_addressing is ensure_segment_addressing
