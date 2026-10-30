"""The data premise of test_66 test_03: both Network Edge routers of AM-NE are PROD handoff ends.

test_03 decommissions ``HANDOFF-AMNE-AWS-EUC1-PROD`` and expects eg-am-ne02 to leave the AM metro's PROD VRF instance
while the instance stays for eg-am-ne01. That needs a PROD handoff on each router, and the sole-demand device on
exactly one: a second PROD circuit on eg-am-ne02 would keep it a member, none on eg-am-ne01 would leave the instance
with its last member.
"""

from __future__ import annotations

from tasks import ALL_DEMO_DATA
from tests.integration.demo_data import ROOT, Declared, declared_session, name
from tests.integration.test_66_all_demo_circuit_lifecycle import (
    SOLE_DEMAND_DEVICE,
    SOLE_DEMAND_HANDOFF,
    SOLE_DEMAND_PEER,
)

_ALL = ROOT / ALL_DEMO_DATA
_NAMESPACE = "PROD"


def _prod_handoffs_of(device: str) -> list[Declared]:
    """The declared handoff circuits of the PROD namespace one of whose ends is a port of ``device``."""
    return [
        circuit
        for circuit in declared_session("handoff", _ALL)
        if circuit.item["ip_namespace"] == _NAMESPACE and device in {end[0] for end in circuit.item["capabilities"]}
    ]


def test_the_peer_router_is_an_end_of_a_prod_handoff() -> None:
    """eg-am-ne01 (the router that keeps the instance) has a PROD handoff of its own."""
    assert [name(c) for c in _prod_handoffs_of(SOLE_DEMAND_PEER)] == ["HANDOFF-FR2-AMNE-PROD"]


def test_the_sole_demand_router_has_exactly_the_one_prod_handoff_under_test() -> None:
    """eg-am-ne02's only PROD handoff is the one test_03 decommissions, so nothing else keeps it a member."""
    assert [name(c) for c in _prod_handoffs_of(SOLE_DEMAND_DEVICE)] == [SOLE_DEMAND_HANDOFF]


def test_the_two_routers_are_not_ends_of_a_common_circuit() -> None:
    """Different circuits: the peer's membership does not depend on the circuit that is decommissioned."""
    ours = {name(c) for c in _prod_handoffs_of(SOLE_DEMAND_DEVICE)}
    theirs = {name(c) for c in _prod_handoffs_of(SOLE_DEMAND_PEER)}
    assert not ours & theirs
