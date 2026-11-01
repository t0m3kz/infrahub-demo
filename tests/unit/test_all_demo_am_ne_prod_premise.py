"""The data premise of test_66 test_03: both Network Edge routers of AM-NE are ends of a PROD provider connection.

test_03 decommissions the session circuit of ``PC-AMNE-AWS-EUC1-PROD`` and expects eg-am-ne02 to leave the AM metro's PROD VRF instance
while the instance stays for eg-am-ne01. That needs a PROD connection on each router, and the sole-demand device on
exactly one: a second PROD connection on eg-am-ne02 would keep it a member, none on eg-am-ne01 would leave the instance
with its last member.
"""

from __future__ import annotations

from tasks import ALL_DEMO_DATA
from tests.integration.demo_data import ROOT, Declared, declared, name
from tests.integration.test_66_all_demo_circuit_lifecycle import SOLE_DEMAND_DEVICE, SOLE_DEMAND_PEER

_ALL = ROOT / ALL_DEMO_DATA
_NAMESPACE = "PROD"


def _prod_connections_of(device: str) -> list[Declared]:
    """The PROD provider connections whose carrier (the one that ends on the connection's near port) ends on a port of
    ``device``."""
    carriers = [c.item["capabilities"] for c in declared("TopologyCarrierCircuit", _ALL)]
    return [
        connection
        for connection in declared("TopologyProviderConnection", _ALL)
        if connection.item["ip_namespace"] == _NAMESPACE
        and any(
            list(connection.item["near_port"]) in ends and device in {end_device for end_device, _ in ends}
            for ends in carriers
        )
    ]


def test_the_peer_router_is_an_end_of_a_prod_connection() -> None:
    """eg-am-ne01 (the router that keeps the instance) has a PROD connection of its own."""
    assert [name(c) for c in _prod_connections_of(SOLE_DEMAND_PEER)] == ["PC-FR2-AMNE-PROD"]


def test_the_sole_demand_router_has_exactly_the_one_prod_connection_under_test() -> None:
    """eg-am-ne02's only PROD connection is the one test_03 decommissions, so nothing else keeps it a member."""
    assert [name(c) for c in _prod_connections_of(SOLE_DEMAND_DEVICE)] == ["PC-AMNE-AWS-EUC1-PROD"]


def test_the_two_routers_are_not_ends_of_a_common_connection() -> None:
    """Different connections: the peer's membership does not depend on the circuit that is decommissioned."""
    ours = {name(c) for c in _prod_connections_of(SOLE_DEMAND_DEVICE)}
    theirs = {name(c) for c in _prod_connections_of(SOLE_DEMAND_PEER)}
    assert not ours & theirs
