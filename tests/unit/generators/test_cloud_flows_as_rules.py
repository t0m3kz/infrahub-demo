"""A cloud flow is a SecurityPolicyRule: what its application query and its endpoint address must carry."""

from __future__ import annotations

from utils.segment_addressing import segment_prefix


def test_cloud_segment_resolves_to_its_cidr_block() -> None:
    """A cloud endpoint has no gateway: its cidr_block is the address a rule matches."""
    assert segment_prefix({"typename": "CloudNetworkSegment", "cidr_block": "10.50.0.0/24"}, "ipv4") == "10.50.0.0/24"


def test_cloud_segment_without_a_cidr_resolves_to_nothing() -> None:
    """No gateway and no cidr_block: None, never a permit-any."""
    assert segment_prefix({"typename": "CloudNetworkSegment"}, "ipv4") is None
