"""A cloud flow is a SecurityPolicyRule: what its application query and its endpoint address must carry."""

from __future__ import annotations

import re
from pathlib import Path

from utils.segment_addressing import segment_prefix


def _query(root_dir: Path) -> str:
    """The application query text without comment lines."""
    text = (root_dir / "queries" / "topology" / "add" / "application.gql").read_text()
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_target_segment_carries_the_fields_a_cloud_endpoint_needs(root_dir: Path) -> None:
    """The query returns typename, environment, cidr_block and the parent network as plain fields."""
    text = _query(root_dir)
    target = " ".join(text[text.index("target {") : text.index("health_checks")].split())
    for needle in ("...SegmentSecurityFields", "cidr_block { value }", "virtual_network {"):
        assert needle in target
    head = " ".join(text[: text.index("fragment AppComponentSecurityFields")].split())
    assert "__typename" in head
    assert "environment { value }" in head


def test_no_named_spread_inside_an_inline_fragment(root_dir: Path) -> None:
    """No named fragment spread inside the cloud inline fragments: keep the fields plain."""
    text = _query(root_dir)
    for match in re.finditer(r"\.\.\. on CloudNetworkSegment \{", text):
        depth, i = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[i], 0)
            i += 1
        assert "..." not in text[match.end() : i]


def test_cloud_segment_resolves_to_its_cidr_block() -> None:
    """A cloud endpoint has no gateway: its cidr_block is the address a rule matches."""
    assert segment_prefix({"typename": "CloudNetworkSegment", "cidr_block": "10.50.0.0/24"}) == "10.50.0.0/24"


def test_cloud_segment_without_a_cidr_resolves_to_nothing() -> None:
    """No gateway and no cidr_block: None, never a permit-any."""
    assert segment_prefix({"typename": "CloudNetworkSegment"}) is None
