"""``ManagedNetworkSegment.ip_version``"""

from __future__ import annotations

import pytest

from tests.integration.demo_data import attribute, schema


@pytest.mark.parametrize("kind", ["ManagedNetworkSegment", "ManagedVxlanSegment", "CloudNetworkSegment"])
def test_ip_version_is_a_required_dropdown_defaulting_to_ipv4(kind: str) -> None:
    """Required with the default `ipv4`: an existing segment and a data file that says nothing stay IPv4."""
    definition = attribute(kind, "ip_version") or {}
    assert definition["kind"] == "Dropdown"
    assert definition["optional"] is False
    assert definition["default_value"] == "ipv4"


def test_ip_version_has_exactly_ipv4_and_ipv6() -> None:
    """No `dual`: a segment has one gateway of one family."""
    definition = attribute("ManagedNetworkSegment", "ip_version") or {}
    assert [choice["name"] for choice in definition["choices"]] == ["ipv4", "ipv6"]


def test_ip_version_is_declared_once_on_the_generic_with_an_explicit_weight() -> None:
    """The nodes inherit it (a node must not redefine a generic attribute); the weight is explicit and unique."""
    generic = schema()["ManagedNetworkSegment"]
    assert "ip_version" in generic.attributes
    for kind in ("ManagedVxlanSegment", "CloudNetworkSegment"):
        assert "ip_version" not in schema()[kind].attributes
    weights = [a.get("order_weight") for a in generic.attributes.values()]
    assert generic.attributes["ip_version"]["order_weight"] in weights
    assert len(weights) == len(set(weights))
