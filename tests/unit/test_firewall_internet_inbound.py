"""Where a rule from the internet into a customer segment is placed (transforms/helpers/firewall_contexts.py).

The rule is written once, in the destination segment's own policy (``source_zone`` INTERNET). Each firewall selects
its share by the same split as a rule to the internet, decided per footprint by the contexts it lists
(utils/firewall_split.py): the Internet edge context of a footprint that also has an internal one holds it, that
internal context does not, and a footprint's only context holds every rule.
"""

from __future__ import annotations

from typing import Any

import pytest

from transforms.helpers.firewall_contexts import get_firewall_contexts, place_policies_in_contexts
from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE, INTERNAL_FIREWALL_ROLE
from utils.firewall_split import RuleShare, internet_contexts, rule_share, steering_contexts

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


def _context(context_id: str, role: str) -> dict[str, Any]:
    """A context as a footprint lists it: its members' role."""
    return {"id": context_id, "shared_devices": [{"role": role}, {"role": role}]}


INTERNAL = _context("ctx-internal", INTERNAL_FIREWALL_ROLE)
EDGE = _context("ctx-edge", EXTERNAL_FIREWALL_ROLE)


def _footprint(name: str, segment: dict[str, Any], contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """A served footprint (cleaned): its segments and the contexts it lists."""
    return {"id": f"fp-{name}", "name": name, "network_segments": [segment], "serving_firewall_contexts": contexts}


def _table(context: dict[str, Any], footprints: list[dict[str, Any]]) -> list[str]:
    """The rule ids the table of ``context`` holds, in order, when it serves ``footprints``."""
    cap = {**context, "typename": "ManagedFirewallContext", "served_deployments": footprints}
    device = {"shared_capabilities": [cap], "interfaces": []}
    _, by_context = place_policies_in_contexts(get_firewall_contexts(device))
    return [rule["id"] for policy in by_context[context["id"]] for rule in policy["rules"]]


@pytest.mark.parametrize(
    ("context", "listed", "expected"),
    [
        (EDGE, [INTERNAL, EDGE], ["to-internet", "from-internet"]),
        (INTERNAL, [INTERNAL, EDGE], ["east-west"]),
        (INTERNAL, [INTERNAL], ["to-internet", "east-west", "from-internet"]),
        (EDGE, [EDGE], ["to-internet", "east-west", "from-internet"]),
    ],
    ids=["edge-of-a-footprint-with-both", "internal-of-a-footprint-with-both", "internal-only", "external-only"],
)
def test_an_inbound_internet_rule_is_placed_with_the_outbound_ones(
    context: dict[str, Any], listed: list[dict[str, Any]], expected: list[str]
) -> None:
    """The edge holds both directions, the internal pair behind it neither, a single context every rule."""
    assert _table(context, [_footprint("C006-P-DC10", _segment(), listed)]) == expected


def test_one_shared_internal_context_splits_per_footprint() -> None:
    """A shared internal context serves a footprint with an edge and one without: each keeps its own split."""
    with_edge = _footprint("C006-P-DC10", _segment(), [INTERNAL, EDGE])
    other = _segment()
    other_rules = other["security_policy"]["rules"]
    renamed = {rule["id"]: f"{rule['id']}-c007" for rule in other_rules}
    policy = {**other["security_policy"], "id": "policy-c", "name": "c007-c"}
    other = {
        **other,
        "id": "seg-c7",
        "security_policy": {**policy, "rules": [{**rule, "id": renamed[rule["id"]]} for rule in other_rules]},
        "inbound_rules": [{**rule, "id": renamed[rule["id"]], "policy": policy} for rule in other["inbound_rules"]],
    }
    without_edge = _footprint("C007-P-DC10", other, [INTERNAL])
    assert _table(INTERNAL, [with_edge, without_edge]) == [
        "east-west",
        "to-internet-c007",
        "east-west-c007",
        "from-internet-c007",
    ]


@pytest.mark.parametrize(
    ("external", "listed", "share"),
    [
        (True, [INTERNAL, EDGE], RuleShare.INTERNET),
        (False, [INTERNAL, EDGE], RuleShare.NON_INTERNET),
        (False, [INTERNAL], RuleShare.ALL),
        (True, [EDGE], RuleShare.ALL),
        (False, [], RuleShare.ALL),
    ],
)
def test_rule_share_is_the_table_of_the_split(external: bool, listed: list[dict[str, Any]], share: RuleShare) -> None:
    """The share a context of one role holds of a footprint's rules follows from the footprint's list alone."""
    assert rule_share(external, listed) is share


def test_internet_and_steering_contexts() -> None:
    """The internet rules go to the edge when listed, the PBR to the internal context when listed."""
    assert internet_contexts([INTERNAL, EDGE]) == [EDGE]
    assert internet_contexts([INTERNAL]) == [INTERNAL]
    assert steering_contexts([INTERNAL, EDGE]) == [INTERNAL]
    assert steering_contexts([EDGE]) == [EDGE]
