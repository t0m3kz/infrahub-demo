"""transforms/helpers/bgp_policy.py: one neutral policy per session, the templates only render it."""

from __future__ import annotations

from typing import Any

from transforms.helpers.bgp import add_template_fields
from transforms.helpers.bgp_policy import DCI_IN_MAP, FABRIC_OUT_MAP, compose_policy


def test_a_session_without_policy_attributes_attaches_nothing() -> None:
    """No local_pref, med or shared flag: no own map and nothing to name on the neighbour."""
    policy = compose_policy({"name": "u1"})
    assert policy == {"inbound": {"own": None}, "outbound": {"own": None}, "attach": []}


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
