"""PR5 guards: a cloud flow is a SecurityPolicyRule and nothing generates sg-{app} any more."""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from generators.topology.application_security import AppApplicationGenerator
from utils.segment_addressing import segment_prefix


def _query(root_dir: Path) -> str:
    """The application query text without comment lines."""
    text = (root_dir / "queries" / "topology" / "add" / "application.gql").read_text()
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def test_cloud_security_mixin_module_is_removed() -> None:
    """The cloud-specific rule path no longer exists."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("generators.cloud_security")


def test_generator_has_no_cloud_rule_methods() -> None:
    """Neither the mixin's helpers nor an sg-{app} creator are reachable from the generator."""
    for name in ("_create_cloud_rule", "_get_or_create_sg"):
        assert not hasattr(AppApplicationGenerator, name)


def test_generators_never_name_an_sg_after_an_app() -> None:
    """No generator source builds an sg-{app} name: a cloud flow is a SecurityPolicyRule."""
    root = Path(__file__).resolve().parents[2] / "generators"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name != "protocols.py" and re.search(r'f"sg-\{', path.read_text())
    ]
    assert offenders == []


def test_target_segment_carries_the_fields_a_cloud_endpoint_needs(root_dir: Path) -> None:
    """The query returns typename, environment, cidr_block and the parent network as plain fields."""
    text = _query(root_dir)
    target = text[text.index("target {") : text.index("health_checks")]
    for needle in ("...SegmentSecurityFields", "cidr_block { value }", "virtual_network {"):
        assert needle in target
    assert "__typename" in text[: text.index("fragment AppComponentSecurityFields")]
    assert "environment { value }" in text[: text.index("fragment AppComponentSecurityFields")]


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
