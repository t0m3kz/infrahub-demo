"""queries/validation/provider_connections.gql selects the fields the check payload builders carry."""

from __future__ import annotations

import pytest

from tests.unit.provider_connections_check_helpers import EUC1, FR2, XC, account, carrier, connection, port, session
from tests.unit.test_circuit_query_shapes import _QUERIES, _payload_paths, _selected_paths

QUERY = _QUERIES / "validation" / "provider_connections.gql"


@pytest.mark.parametrize(
    ("root", "fixture"),
    [
        ("TopologyProviderConnection", connection(account=account())),
        ("TopologySessionCircuit", session("S1", link_prefix="10.0.0.0/31")),
        ("TopologyCarrierCircuit", carrier("XC", [FR2, EUC1], ["PC"])),
    ],
)
def test_fixtures_carry_only_selected_fields(root: str, fixture: dict) -> None:
    """A fixture path the query does not select is a field no real response carries."""
    unselected = _payload_paths(fixture) - _selected_paths(QUERY, root)
    assert not unselected, f"fixture fields the query does not select under {root}: {sorted(unselected)}"


def test_port_fixture_is_selected() -> None:
    """The session end port builder matches the SessionPort fragment."""
    unselected = _payload_paths(
        {"capabilities": {"edges": [{"node": port("d", "p.1", FR2, (XC,))}]}}
    ) - _selected_paths(QUERY, "TopologySessionCircuit")
    assert not unselected, sorted(unselected)
