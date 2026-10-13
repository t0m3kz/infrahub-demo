"""Unit tests for the rule selector helpers in transforms/helpers/policy.py."""

from __future__ import annotations

from transforms.helpers.policy import rule_endpoint, rule_selector


def test_rule_selector_prefers_a_prefix_and_returns_its_namespace() -> None:
    """The first prefix wins over an IP address and carries its namespace name."""
    rule = {
        "destination_prefixes": [{"prefix": "10.40.0.0/16", "ip_namespace": {"name": "PROD"}}],
        "destination_ip_addresses": [{"address": "10.9.9.9/32"}],
    }

    assert rule_selector(rule, "destination") == ("10.40.0.0/16", "PROD")


def test_rule_selector_falls_back_to_an_ip_address() -> None:
    """Without a prefix the first IP address is the selector."""
    rule = {"source_ip_addresses": [{"address": "10.9.9.9/32", "ip_namespace": {"name": "NON-PROD"}}]}

    assert rule_selector(rule, "source") == ("10.9.9.9/32", "NON-PROD")


def test_rule_selector_namespace_is_none_when_not_queried() -> None:
    """A selector without ip_namespace yields a None namespace, never an error."""
    assert rule_selector({"destination_prefixes": [{"prefix": "10.40.0.0/16"}]}, "destination") == (
        "10.40.0.0/16",
        None,
    )


def test_rule_selector_is_none_without_a_selector() -> None:
    """No prefixes or addresses on that side: None."""
    assert (
        rule_selector({"destination_prefixes": [], "source_prefixes": [{"prefix": "10.0.0.0/8"}]}, "destination")
        is None
    )


def test_rule_endpoint_uses_the_selector_cidr_when_the_segment_has_no_prefix() -> None:
    """rule_endpoint is unchanged: segment prefix, else the selector's CIDR, else None."""
    selector_rule = {"destination_prefixes": [{"prefix": "10.40.0.0/16", "ip_namespace": {"name": "PROD"}}]}
    segment_rule = {"destination_segment": {"gateway": {"ip_prefix": {"prefix": "10.1.0.0/24"}}}, **selector_rule}

    assert rule_endpoint(selector_rule, "destination") == "10.40.0.0/16"
    assert rule_endpoint(segment_rule, "destination") == "10.1.0.0/24"
    assert rule_endpoint({}, "destination") is None
