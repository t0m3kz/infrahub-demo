"""Unit tests for the rule selector helpers in transforms/helpers/policy.py."""

from __future__ import annotations

import pytest

from transforms.helpers.policy import (
    is_internet_destination,
    is_internet_rule,
    is_internet_source,
    rule_endpoint,
    rule_selector,
    rule_zone,
)
from utils.routing_domain import RoutingDomainError


def test_rule_selector_prefers_a_prefix_and_returns_the_rules_zone() -> None:
    """The first prefix wins over an IP address; its zone is the rule's own explicit zone, never the prefix's."""
    rule = {
        "destination_zone": {"name": "PROD"},
        "destination_prefixes": [
            {
                "prefix": "10.40.0.0/16",
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            }
        ],
        "destination_ip_addresses": [{"address": "10.9.9.9/32"}],
    }

    assert rule_selector(rule, "destination") == ("10.40.0.0/16", "PROD")


def test_rule_selector_falls_back_to_an_ip_address() -> None:
    """Without a prefix the first IP address is the selector."""
    rule = {
        "source_zone": {"name": "NONPROD"},
        "source_ip_addresses": [{"address": "10.9.9.9/32", "ip_namespace": {"name": "default"}}],
    }

    assert rule_selector(rule, "source") == ("10.9.9.9/32", "NONPROD")


def test_rule_selector_without_an_explicit_zone_fails_closed() -> None:
    """A selector whose rule names no zone has no routing authority: refused, not guessed from IPAM."""
    with pytest.raises(RoutingDomainError, match="missing security zone"):
        rule_selector({"destination_prefixes": [{"prefix": "10.40.0.0/16"}]}, "destination")


def test_rule_selector_is_none_without_a_selector() -> None:
    """No prefixes or addresses on that side: None."""
    assert (
        rule_selector({"destination_prefixes": [], "source_prefixes": [{"prefix": "10.0.0.0/8"}]}, "destination")
        is None
    )


def test_rule_endpoint_uses_the_selector_cidr_when_the_segment_has_no_prefix() -> None:
    """rule_endpoint is unchanged: segment prefix, else the selector's CIDR, else None."""
    selector_rule = {
        "destination_zone": {"name": "PROD"},
        "destination_prefixes": [
            {
                "prefix": "10.40.0.0/16",
                "ip_namespace": {"node": {"name": {"value": "default"}}},
            }
        ],
    }
    segment_rule = {"destination_segment": {"gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}}}, **selector_rule}

    assert rule_endpoint(selector_rule, "destination") == "10.40.0.0/16"
    assert rule_endpoint(segment_rule, "destination") == "10.1.0.0/24"
    assert rule_endpoint({}, "destination") is None


def test_rule_zone_of_the_destination_is_the_rules_own_destination_zone() -> None:
    """A rule to the internet has no destination segment: its zone is the rule's destination_zone."""
    rule = {"source_segment": {"security_zone": {"name": "PROD"}}, "destination_zone": {"name": "INTERNET"}}

    assert rule_zone(rule, "destination") == "INTERNET"
    assert rule_zone(rule, "source") == "PROD"


def test_rule_zone_falls_back_to_the_segments_zone_and_to_none() -> None:
    """Without a destination_zone the segment's security_zone decides; neither means no zone."""
    assert rule_zone({"destination_segment": {"security_zone": {"name": "PROD"}}}, "destination") == "PROD"
    assert rule_zone({"destination_zone": None, "destination_segment": None}, "destination") is None


def test_rule_endpoint_of_a_destination_zone_is_any() -> None:
    """The zone names no address: the endpoint stays None (any), which is what the firewall table renders."""
    assert rule_endpoint({"destination_zone": {"name": "INTERNET"}}, "destination") is None


def test_a_rule_from_the_internet_zone_is_an_internet_source() -> None:
    """``source_zone`` INTERNET and no source segment: inbound from outside, a source selector only narrows it."""
    inbound = {"source_zone": {"name": "INTERNET"}, "destination_segment": {"id": "seg-b"}}
    narrowed = {**inbound, "source_prefixes": [{"prefix": "203.0.113.0/24"}]}

    assert is_internet_source(inbound) and is_internet_source(narrowed)
    assert is_internet_rule(inbound) and not is_internet_destination(inbound)


def test_a_segment_source_or_another_zone_is_no_internet_source() -> None:
    """A rule from a segment, or from a tenant zone selector, never crosses the internet edge inbound."""
    from_segment = {"source_zone": {"name": "INTERNET"}, "source_segment": {"id": "seg-a"}}
    from_prod = {"source_zone": {"name": "PROD"}, "source_prefixes": [{"prefix": "10.1.0.0/24"}]}

    assert not is_internet_source(from_segment)
    assert not is_internet_source(from_prod) and not is_internet_rule(from_prod)


def test_a_rule_to_the_internet_is_an_internet_rule() -> None:
    """Both directions are internet rules: the edge context holds them all."""
    assert is_internet_rule({"source_segment": {"id": "seg-a"}, "destination_zone": {"name": "INTERNET"}})
