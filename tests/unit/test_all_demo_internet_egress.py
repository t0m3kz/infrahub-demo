"""Offline data tests: the internet rules of 30_all name footprints the internet_egress check can judge.

checks/internet_egress.py follows an enabled rule with ``destination_zone: INTERNET-ZONE`` from its source segment to
the segment's DC and colocation footprints. The firewall pair, legs, exchange, ports and SNAT address of those
footprints come from the generators, so only the pieces the data declares are asserted: the rule names the internet
zone, and its source segment is activated in a declared DC or colocation footprint (not a cloud or office one, which
the check skips).
"""

from __future__ import annotations

import pytest

from tasks import ALL_DEMO_DATA
from tests.integration.demo_data import ROOT, declared, name
from utils.exchange_transit import INTERNET_ZONE

_ALL = ROOT / ALL_DEMO_DATA
_DC_RULE = ("nordix-prod-to-internet-https", "c006-nordix-prod-p", "TopologyCustomerDC", "C006-P-DC11")
_COLOCATION_RULE = ("nordix-prod-to-saas-zscaler", "c001-nordix-prod-p", "TopologyCustomerColocation", "C001-P-FR")


def _enabled_internet_rules() -> dict[str, str]:
    """Rule name -> source segment of every enabled 30_all rule that names the internet zone."""
    return {
        name(rule): rule.item["source_segment"]
        for rule in declared("SecurityPolicyRule", _ALL)
        if rule.item.get("destination_zone") == INTERNET_ZONE
        and not rule.item.get("disabled")
        and (rule.enclosing is None or rule.enclosing.item.get("enabled", True))
    }


def _footprints_of(segment: str) -> list[str]:
    """The ``customer_deployments`` of the declared VXLAN segment ``segment``."""
    (found,) = [item for item in declared("ManagedVxlanSegment", _ALL) if name(item) == segment]
    return list(found.item["customer_deployments"])


def test_the_demo_has_enabled_internet_rules() -> None:
    """The demo exercises the check: the DC11 and the FR colocation internet rules are there and enabled."""
    rules = _enabled_internet_rules()
    assert rules[_DC_RULE[0]] == _DC_RULE[1]
    assert rules[_COLOCATION_RULE[0]] == _COLOCATION_RULE[1]


@pytest.mark.parametrize(("rule", "segment", "kind", "footprint"), [_DC_RULE, _COLOCATION_RULE])
def test_source_segment_is_activated_in_a_declared_on_prem_footprint(
    rule: str, segment: str, kind: str, footprint: str
) -> None:
    """The rule's segment is on a declared footprint of an on-prem kind: the check has something to judge."""
    assert _enabled_internet_rules()[rule] == segment
    assert footprint in _footprints_of(segment)
    assert footprint in {name(item) for item in declared(kind, _ALL)}


def test_every_enabled_internet_rule_has_a_source_segment_on_an_on_prem_footprint() -> None:
    """A demo rule on a cloud or office footprint would pass silently (a deliberate gap): none exists in 30_all."""
    on_prem = {
        name(item) for kind in ("TopologyCustomerDC", "TopologyCustomerColocation") for item in declared(kind, _ALL)
    }
    for rule, segment in _enabled_internet_rules().items():
        assert set(_footprints_of(segment)) <= on_prem, rule
