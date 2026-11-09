"""Where a rule from the internet into a customer segment is placed (transforms/helpers/firewall_contexts.py).

The rule is written once, in the destination segment's own policy (``source_zone`` INTERNET). Each firewall selects
its share by the same split as a rule to the internet: the Internet edge context holds it, the internal context of a
DC with an edge does not, and the serving context of a DC without an edge holds every rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from transforms.helpers.firewall_contexts import place_policies_in_contexts

SEGMENT_ID = "seg-b"


def _segment() -> dict[str, Any]:
    """A segment whose own policy holds an outbound internet rule, an east-west rule and an inbound internet rule."""
    policy_ref = {"id": "policy-b", "name": "c006-b", "enabled": True, "segment": {"id": SEGMENT_ID}}
    own = {"id": SEGMENT_ID, "security_zone": {"name": "PROD"}, "gateway": {"ip_prefix": {"prefix": "10.2.0.0/24"}}}
    rules = [
        {
            "id": "to-internet",
            "index": 10,
            "action": "permit",
            "source_segment": own,
            "destination_zone": {"name": "INTERNET"},
        },
        {
            "id": "east-west",
            "index": 20,
            "action": "permit",
            "source_segment": own,
            "destination_segment": {"id": "seg-c", "gateway": {"ip_prefix": {"prefix": "10.3.0.0/24"}}},
        },
        {
            "id": "from-internet",
            "index": 30,
            "action": "permit",
            "source_zone": {"name": "INTERNET"},
            "destination_segment": own,
        },
    ]
    inbound = [{**rules[2], "policy": policy_ref}]
    return {**own, "security_policy": {**policy_ref, "rules": rules}, "inbound_rules": inbound}


def _placed(**context: Any) -> list[str]:
    """The rule ids the context's table holds, in order."""
    ctx = {"id": "ctx", "segments": [_segment()], "edge_hostings": [], "edge_hosted": False, **context}
    _, by_context = place_policies_in_contexts([ctx])
    return [rule["id"] for policy in by_context["ctx"] for rule in policy["rules"]]


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"edge_hostings": [{"id": "dc-10"}]}, ["to-internet", "from-internet"]),
        ({"edge_hosted": True}, ["east-west"]),
        ({}, ["to-internet", "east-west", "from-internet"]),
    ],
    ids=["internet-edge-context", "internal-context-of-an-edge-hosted-dc", "context-of-a-dc-without-an-edge"],
)
def test_an_inbound_internet_rule_is_placed_with_the_outbound_ones(
    context: dict[str, Any], expected: list[str]
) -> None:
    """The edge holds both directions, the internal pair behind it neither, a DC without an edge every rule."""
    assert _placed(**context) == expected
