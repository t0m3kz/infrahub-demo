"""transforms/helpers/bgp_policy.py: one neutral policy per session, the templates only render it."""

from __future__ import annotations

from typing import Any

from transforms.helpers.bgp import add_template_fields
from transforms.helpers.bgp_policy import DCI_IN_MAP, FABRIC_OUT_MAP, compose_policy
from utils.bgp_expectation import DCI_UNDERLAY, UNDERLAY, Expectation, FamilyPolicy


def test_a_session_without_policy_attributes_attaches_nothing() -> None:
    """No local_pref, med or shared flag: no own map and nothing to name on the neighbour."""
    policy = compose_policy({"name": "u1"})
    assert policy == {
        "inbound": {"own": None},
        "outbound": {"own": None},
        "attach": [],
        "max": {},
        "max_total": None,
        "enforced": False,
    }


def test_local_pref_and_med_become_own_maps_attached_in_then_out() -> None:
    """The peering's local_pref is an inbound map and its MED an outbound one, named after the session."""
    policy = compose_policy({"name": "peer-a", "local_pref": 200, "med": 50})
    assert policy["inbound"]["own"] == {"name": "peer-a-LOCAL-PREF-IN", "local_pref": 200}
    assert policy["outbound"]["own"] == {"name": "peer-a-MED-OUT", "med": 50}
    assert [(a["map"], a["direction"], a["own"]) for a in policy["attach"]] == [
        ("peer-a-LOCAL-PREF-IN", "in", True),
        ("peer-a-MED-OUT", "out", True),
    ]


def test_a_border_gateway_pair_session_attaches_the_shared_maps_after_its_own() -> None:
    """export_local_only and dci_inbound name maps another template defines (own False), after the own ones."""
    policy = compose_policy({"name": "d1", "med": 7, "export_local_only": True, "dci_inbound": True})
    assert [(a["map"], a["direction"], a["own"]) for a in policy["attach"]] == [
        ("d1-MED-OUT", "out", True),
        (FABRIC_OUT_MAP, "out", False),
        (DCI_IN_MAP, "in", False),
    ]


def test_add_template_fields_composes_the_policy_of_every_session() -> None:
    """The one place every vendor template's inputs are added also composes the policy."""
    session: dict[str, Any] = {"name": "a", "remote_ip": {"address": "10.0.0.2/31"}, "local_pref": 150}
    config: dict[str, Any] = {
        "router_id": {"address": "10.0.0.1/32"},
        "sessions": [session],
    }
    add_template_fields(config)
    assert session["policy"]["inbound"]["own"]["name"] == "a-LOCAL-PREF-IN"


def _expectation(klass: str, **limits: int | None) -> Expectation:
    """An expectation whose families carry only the given maximum-prefix."""
    return Expectation(klass, {family: FamilyPolicy(None, None, max_prefix=limit) for family, limit in limits.items()})


def test_a_staged_limit_only_warns() -> None:
    """Without routing_filter_enforce the derived limit is warning-only, whatever the class."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=256), enforce=False)
    assert policy["max"] == {"ipv4": {"limit": 256, "warning_only": True}}
    assert policy["enforced"] is False


def test_an_enforced_limit_is_live() -> None:
    """routing_filter_enforce true: the same limit no longer warns only."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=256), enforce=True)
    assert policy["max"]["ipv4"]["warning_only"] is False
    assert policy["enforced"] is True


def test_the_total_sums_the_unicast_families_and_leaves_evpn_out() -> None:
    """A vendor whose command is per neighbour gets IPv4 + IPv6; the EVPN limit is its own."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=10, ipv6=5, evpn=1000), enforce=True)
    assert policy["max_total"] == {"limit": 15, "warning_only": False}
    assert policy["max"]["evpn"]["limit"] == 1000


def test_a_family_without_a_derived_limit_renders_none() -> None:
    """None means not derivable: no entry, and no total when no unicast family has one."""
    policy = compose_policy({"name": "u"}, _expectation(UNDERLAY, ipv4=None), enforce=True)
    assert policy["max"] == {}
    assert policy["max_total"] is None


def test_an_expectation_leaves_the_own_maps_and_shared_maps_alone() -> None:
    """The limit is added to the policy; the local-pref map and the shared maps are composed as before."""
    session = {"name": "d1", "local_pref": 150, "export_local_only": True}
    policy = compose_policy(session, _expectation(DCI_UNDERLAY, ipv6=4), enforce=True)
    assert [a["map"] for a in policy["attach"]] == ["d1-LOCAL-PREF-IN", FABRIC_OUT_MAP]
