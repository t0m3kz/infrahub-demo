"""The circuit inventory parser and the test_62 circuit rules, offline over the shipped 30_all data.

tests/integration/test_62 asserts, on a live stack, the virtual-circuit expectations of test_constants and the
per-kind placement rules. Nothing there can run without Infrahub, so a rule that cannot hold on the shipped data (or an
inventory field the parser drops) would first show on a stack run. Here the same data is shaped like the response of
``tests/integration/queries/get_interconnect_inventory.gql`` (the simulator of the placement check, completed with what
the inventory query selects on top: tests/unit/simulators/circuit_inventory_payloads.py), parsed by the real ``fetch_interconnect_inventory`` and run through the real test_62
functions: ``_expectation_errors`` and ``_virtual_placement_errors`` must report nothing. The tests that break the data on
purpose show the proof is not vacuous.

Not covered: what a generator writes at run time (addresses, sessions, prefixes), which exists only on a stack.
"""

from __future__ import annotations

import copy
import ipaddress
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphql import parse

from tests.integration import test_62_all_demo_interconnects as interconnects
from tests.integration import test_helpers
from tests.integration.test_constants import (
    ALL_DEMO_HANDOFF_PARTY_ASNS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    HANDOFF_KIND,
    INTERCONNECT_KIND,
    TUNNEL_KIND,
)
from tests.integration.test_helpers import TOPOLOGY_SNAPSHOT_KINDS, fetch_interconnect_inventory, snapshot_query
from tests.unit.simulators.circuit_inventory_payloads import inventory_payload

_QUERIES = Path(__file__).parents[1] / "integration" / "queries"


async def inventory(
    monkeypatch: pytest.MonkeyPatch, without_cables: bool = False, without_circuits: tuple[str, ...] = ()
) -> dict[str, list[dict[str, Any]]]:
    """The real fetch_interconnect_inventory over a mocked client answering with the shipped data."""
    monkeypatch.setattr(test_helpers, "DATA_PROPAGATION_DELAY", 0)
    client = MagicMock()
    client.execute_graphql = AsyncMock(return_value=inventory_payload(without_cables, without_circuits))
    return await fetch_interconnect_inventory(client=client, branch="main")


def _placement(data: dict[str, list[dict[str, Any]]]) -> list[str]:
    return interconnects.TestAllDemoInterconnects._virtual_placement_errors(data["virtual"], data["physical"])


def _circuit(data: dict[str, list[dict[str, Any]]], circuit_name: str) -> dict[str, Any]:
    return next(circuit for circuit in data["virtual"] if circuit["name"] == circuit_name)


# -- the shipped data satisfies the expectations and the rules ----------------------------------------------------------


async def test_the_shipped_data_matches_the_virtual_circuit_expectations(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kind, owner, locations, ends, derived carrier, namespace and cloud endpoint of all 30_all circuits."""
    data = await inventory(monkeypatch)
    assert {c["name"] for c in data["virtual"]} == set(ALL_DEMO_VIRTUAL_CIRCUITS)
    assert interconnects._expectation_errors(data["virtual"]) == []


async def test_the_shipped_data_passes_every_per_kind_placement_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules test_62 asserts on the stack hold on the data the stack is loaded with."""
    assert _placement(await inventory(monkeypatch)) == []


async def test_every_kind_is_in_the_inventory_with_its_own_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser keeps what only one kind has: a handoff's namespace, key, limit and VIF VLAN."""
    data = await inventory(monkeypatch)
    kinds = {c["kind"] for c in data["virtual"]}
    assert kinds == {TUNNEL_KIND, INTERCONNECT_KIND, HANDOFF_KIND}
    aws = _circuit(data, "HANDOFF-FR2-AWS-EUC1-PROD")
    assert (aws["ip_namespace"], aws["cloud_endpoint"], aws["cloud_endpoint_vlan"]) == (
        "PROD",
        "vif-transit-hub-euc1",
        1000,
    )
    assert aws["maximum_routes"] == 100
    assert aws["password"] == "eqx-fr2-aws-vif-key"
    assert [end["parent_name"] for end in aws["end_nodes"]] == ["Ethernet1/13", "et-0/0/0"]
    # a sub-interface rides its parent's carrier, a cable-carried handoff has no circuit but a cable
    assert [c["circuit_id"] for end in aws["end_nodes"] for c in end["carriers"]] == ["XC-EQXFR2-AWS-EUC1"] * 2
    sdwan = _circuit(data, "HANDOFF-FR2-SDWAN-GW1-PROD")
    assert sdwan["carrier"] == [] and all(end["cabled"] for end in sdwan["end_nodes"])
    for tunnel in (c for c in data["virtual"] if c["kind"] == TUNNEL_KIND):
        assert tunnel["ip_namespace"] is None and tunnel["cloud_endpoint"] is None and tunnel["password"] is None


# -- breaking the data on purpose ---------------------------------------------------------------------------------------


async def test_without_the_internet_underlay_a_tunnel_rides_no_internet_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping INET-C001-OFFICE leaves the C001 tunnel's edge uplink on no circuit: the carrier differs and the
    tunnel rule reports it."""
    data = await inventory(monkeypatch, without_circuits=("INET-C001-OFFICE",))
    assert any(
        "C001-OFFICE-SDWAN-FR2" in e and "rides no live internet physical circuit" in e for e in _placement(data)
    )
    assert any("C001-OFFICE-SDWAN-FR2: carrier" in e for e in interconnects._expectation_errors(data["virtual"]))


async def test_without_the_fibre_a_dci_is_on_no_physical_circuit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping a dark fibre leaves one DCI end carried by nothing."""
    data = await inventory(monkeypatch, without_circuits=("DF-DC10-EQXFR2",))
    assert any("ICX-DC10-EQXFR2" in e and "are on no live physical circuit" in e for e in _placement(data))


async def test_without_the_cables_the_cable_carried_handoffs_have_no_carrier(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SD-WAN gateway handoff and the Fabric hop sit in one cage: carried by a cable, not a circuit."""
    errors = _placement(await inventory(monkeypatch, without_cables=True))
    carrierless = {e.split(":")[0] for e in errors if "no end is on a live physical circuit or a cable" in e}
    assert carrierless == {"HANDOFF-FR2-SDWAN-GW1-PROD", "HANDOFF-FR2-AMNE-PROD"}


async def test_a_handoff_whose_vlans_differ_or_miss_the_vif_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    data = await inventory(monkeypatch)
    aws = _circuit(data, "HANDOFF-FR2-AWS-EUC1-PROD")
    aws["cloud_endpoint_vlan"] = 4000
    assert any("states vlan_id 4000" in e for e in _placement(data))
    aws["end_nodes"][0]["interface"] = "Ethernet1/13.1001"
    assert any("different VLANs" in e for e in _placement(data))
    aws["end_nodes"][0]["interface"] = "Ethernet1/13"
    assert any("is not a VLAN sub-interface" in e for e in _placement(data))


async def test_a_handoff_placed_elsewhere_than_its_ends_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    data = await inventory(monkeypatch)
    megaport = _circuit(data, "HANDOFF-PA4-MEGAPORT-PROD")
    megaport["location_nodes"] = megaport["location_nodes"][:1]
    assert any("are not where its ends are deployed" in e for e in _placement(data))


async def test_an_owned_dci_a_foreign_tunnel_owner_and_a_wrong_gateway_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await inventory(monkeypatch)
    _circuit(data, "ICX-DC10-EQXFR2")["owner_id"] = "org-x"
    tunnel = _circuit(data, "C001-OFFICE-SDWAN-FR2")
    tunnel["owner_id"] = "org-x"
    tunnel["end_devices"][0]["deployment_id"] = "elsewhere"
    tunnel["end_devices"][1]["deployment_id"] = "elsewhere"
    errors = _placement(data)
    assert any("ICX-DC10-EQXFR2: owned by" in e and "shared platform" in e for e in errors)
    assert any("C001-OFFICE-SDWAN-FR2: owned by" in e and "is an office of that owner" in e for e in errors)
    assert any("C001-OFFICE-SDWAN-FR2: gateway" in e and "none of its locations" in e for e in errors)


async def test_a_dci_that_is_not_pure_fibre_or_not_contiguous_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    data = await inventory(monkeypatch)
    dci = _circuit(data, "ICX-DC10-EQXFR2")
    dci["end_nodes"][0]["id"] = "other-port"
    assert any("a DCI is pure fibre" in e for e in _placement(data))
    physical = next(c for c in data["physical"] if c["circuit_id"] == "DF-DC10-EQXFR2")
    dci["end_nodes"][0]["id"] = next(iter(physical["end_ids"]))
    # a carrier between two other places: neither contiguous nor the DCI's own locations
    physical["location_nodes"] = [
        dict(physical["location_nodes"][0], id="x"),
        dict(physical["location_nodes"][1], id="y"),
    ]
    assert any("is not one contiguous path" in e for e in _placement(data))


async def test_two_session_circuits_on_one_port_and_prod_meeting_non_prod_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = await inventory(monkeypatch)
    first = _circuit(data, "ICX-DC10-EQXFR2")
    second = _circuit(data, "ICX-DC11-EQXFR2")
    second["end_nodes"][1] = dict(second["end_nodes"][1], id=first["end_nodes"][1]["id"])
    assert any("one port carries one interconnect or handoff session" in e for e in _placement(data))
    handoff = _circuit(data, "HANDOFF-FR2-AWS-EUC1-PROD")
    handoff["ip_namespace"] = "NON-PROD"
    assert any("different environments" in e for e in _placement(data))


# -- helpers --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("hops", "start", "finish", "expected"),
    [
        ([["a", "b"]], "a", "b", True),
        ([["a", "b"]], "b", "a", True),
        ([["b", "c"], ["a", "b"]], "a", "c", True),  # in some order
        ([["a", "b"], ["c", "d"]], "a", "d", False),  # a gap
        ([["a", "b"], ["b", "c"], ["b", "d"]], "a", "c", False),  # every hop must be used
        ([["a", "b"]], "a", "c", False),
    ],
)
def test_contiguous_walks_every_order_of_the_hops(
    hops: list[list[str]], start: str, finish: str, expected: bool
) -> None:
    assert interconnects._contiguous(hops, start, finish) is expected


@pytest.mark.parametrize(
    ("interface", "vlan"),
    [("Ethernet1/13.1000", 1000), ("eth0.1900", 1900), ("Ethernet1/13", None), ("et-0/0/0.x", None)],
)
def test_vlan_is_the_numeric_suffix_of_a_sub_interface(interface: str, vlan: int | None) -> None:
    assert interconnects._vlan(interface) == vlan


def _session(**changes: Any) -> dict[str, Any]:
    """A SESSIONS_QUERY node for the handoff session of the AWS VIF."""
    session: dict[str, Any] = {
        "name": {"value": "HANDOFF-X-bgp"},
        "status": {"value": "active"},
        "peering_role": {"value": "regular"},
        "session_type": {"value": "EBGP"},
        "ttl": {"value": 1},
        "bfd_enabled": {"value": True},
        "send_extended_community": {"value": False},
        "maximum_routes": {"value": 100},
        "password": {"node": {"name": {"value": "key"}}},
        "address_families": {"edges": [{"node": {"afi": {"value": "ipv4"}, "safi": {"value": "unicast"}}}]},
    }
    return {**session, **changes}


_EXPECTED_SESSION: dict[str, Any] = {
    "status": "active",
    "peering_role": "regular",
    "session_type": "EBGP",
    "ttl": 1,
    "bfd_enabled": True,
    "send_extended_community": False,
    "maximum_routes": 100,
    "key": "key",
    "families": [("ipv4", "unicast")],
}


def test_session_errors_name_every_knob_that_differs() -> None:
    assert interconnects._session_errors(_session(), _EXPECTED_SESSION) == []
    errors = interconnects._session_errors(
        _session(
            ttl={"value": 2},
            bfd_enabled={"value": False},
            password={"node": None},
            maximum_routes={"value": None},
            address_families={"edges": []},
        ),
        _EXPECTED_SESSION,
    )
    assert [e.split(" ")[1] for e in errors] == ["ttl", "bfd_enabled", "maximum_routes", "key", "families"]


def _prefix(prefix: str, role: str = "technical") -> dict[str, Any]:
    return {"prefix": {"value": prefix}, "role": {"value": role}}


def test_p2p_errors_hold_a_pair_of_addresses_to_its_pool_and_its_one_prefix() -> None:
    pools = [ipaddress.ip_network("10.255.128.0/20")]
    good = interconnects._p2p_errors(
        "h", ["10.255.128.0/31", "10.255.128.1/31"], 31, pools, [_prefix("10.255.128.0/31")]
    )
    assert good == []
    assert any(
        "not one P2P" in e
        for e in interconnects._p2p_errors("h", ["10.255.128.0/31", "10.255.128.4/31"], 31, pools, None)
    )
    assert any(
        "is not a /31" in e
        for e in interconnects._p2p_errors("h", ["10.0.0.0/31", "10.0.0.1/31"], 31, pools, [_prefix("10.0.0.0/31")])
    )
    assert any(
        "0 prefix object(s)" in e
        for e in interconnects._p2p_errors("h", ["10.255.128.0/31", "10.255.128.1/31"], 31, pools, None)
    )
    assert any(
        "has role customer" in e
        for e in interconnects._p2p_errors(
            "h", ["10.255.128.0/31", "10.255.128.1/31"], 31, pools, [_prefix("10.255.128.0/31", "customer")]
        )
    )
    assert any(
        "2 prefix object(s)" in e
        for e in interconnects._p2p_errors(
            "h", ["10.255.128.0/31", "10.255.128.1/31"], 31, pools, [_prefix("10.255.128.0/31")] * 2
        )
    )


def test_every_handoff_party_asn_is_a_declared_process() -> None:
    """The party of a handoff is the one end that has a declared process; the Network Edge pair has none."""
    handoffs = {n for n, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["kind"] == HANDOFF_KIND}
    assert set(ALL_DEMO_HANDOFF_PARTY_ASNS) == handoffs - {"HANDOFF-FR2-AMNE-PROD"}
    assert {party for party, _ in ALL_DEMO_HANDOFF_PARTY_ASNS.values()} == {
        "EXT-AWS-DX-FR2",
        "EQX-FR2-SDWAN-GW1",
        "EXT-MEGAPORT-MCR1",
    }


# -- the GraphQL the stack run sends --------------------------------------------------------------------------------------


_QUERIES_UNDER_TEST: dict[str, str] = {
    "sessions": interconnects.SESSIONS_QUERY,
    "circuit_prefixes": interconnects.CIRCUIT_PREFIXES_QUERY,
    "group_members": interconnects.GROUP_MEMBERS_QUERY,
    "address_count": interconnects.ADDRESS_COUNT_QUERY,
    "interconnect_inventory": (_QUERIES / "get_interconnect_inventory.gql").read_text(),
    **{f"snapshot_{kind}": snapshot_query(kind, selection, 0) for kind, selection in TOPOLOGY_SNAPSHOT_KINDS.items()},
}


@pytest.mark.parametrize("query", _QUERIES_UNDER_TEST.values(), ids=_QUERIES_UNDER_TEST.keys())
def test_every_query_is_valid_graphql_syntax(query: str) -> None:
    """A syntax error in a query only shows on a stack; parsing proves the strings and fragments are well formed."""
    assert parse(query).definitions


def test_the_snapshot_covers_the_addresses_sessions_and_keys_the_generator_writes() -> None:
    """A rerun that moved an interface's address, a session's knobs or a key's secret must be a diff."""
    interface = TOPOLOGY_SNAPSHOT_KINDS["DcimInterface"]
    assert interface.count("ip_address { node { id address { value } } }") == 3
    peering = TOPOLOGY_SNAPSHOT_KINDS["ManagedBGPPeering"]
    for field in ("peering_role", "ttl", "bfd_enabled", "send_extended_community", "maximum_routes", "password"):
        assert field in peering
    assert "password { value }" in TOPOLOGY_SNAPSHOT_KINDS["RoutingPassword"]
    circuits = TOPOLOGY_SNAPSHOT_KINDS["TopologyVirtualCircuit"]
    assert "tunnel_id" not in circuits and "physical_circuits" not in circuits
    assert all(f"... on {kind} {{ locations" in circuits for kind in (TUNNEL_KIND, INTERCONNECT_KIND, HANDOFF_KIND))


def test_the_payload_builder_does_not_share_state_between_calls() -> None:
    """inventory_payload mutates what build_payloads returns: a second call must still be complete."""
    first = copy.deepcopy(inventory_payload())
    assert inventory_payload() == first
