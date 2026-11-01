"""The provider-connection expectations of the integration run (test_67, test_62), held against the data and the planner.

tests/integration/all_demo_provider_connection_constants.py is typed by hand, as an oracle independent of the generator.
Nothing there runs without a stack, so a connection added to the data, a flavour edited in utils/provider_services.py or a
wrong end in the table would first show in a 55-minute run. Here the table is held against the YAML of
``08_interconnects/05_provider_connections``, the offline planner (generators/helpers/provider_connection.py, with the
flavour of the provider as the query payload gives it), and the judges of test_67 are run on canned payloads built from
the table itself: they must pass on it, and fail naming the field when one thing of it is wrong.
"""

from __future__ import annotations

import copy
import ipaddress
from typing import Any

import pytest

from generators.helpers.provider_connection import parse_connection, plan_links
from tasks import ALL_DEMO_DATA
from tests.integration import test_65_all_demo_idempotency as idempotency
from tests.integration import test_67_all_demo_provider_connections as provider_connections
from tests.integration.all_demo_circuit_constants import ALL_DEMO_HANDOFF_PARTY_ASNS, ALL_DEMO_VIRTUAL_CIRCUITS
from tests.integration.all_demo_provider_connection_constants import (
    ADDRESS_OUR_POOL,
    ADDRESS_PROVIDER,
    ALL_DEMO_CIRCUIT_OWNERS,
    ALL_DEMO_CONNECTION_CIRCUITS,
    ALL_DEMO_CONNECTION_HANDOFFS,
    ALL_DEMO_CONNECTION_LINKS,
    ALL_DEMO_CONNECTIONS,
)
from tests.integration.demo_data import ROOT, declared
from tests.integration.provider_connection_helpers import (
    carrier_membership_errors,
    connection_errors,
    link_errors,
    unowned_circuit_errors,
)
from tests.integration.routed_link_rules import END_PROVIDER, LinkFlavour, _assigned_errors, link_hosts
from tests.unit.test_all_demo_provider_connection_plans import _connections, _payload

_ALL = ROOT / ALL_DEMO_DATA
_ROWS = {row["name"]: row for row in _connections()}

#: The ASN of every party stub, from the providers' documentation of their own numbers.
_PARTY_ASNS = {
    "EXT-AWS-DX-FR2": 64512,
    "EXT-MEGAPORT-MCR1": 133937,
    "EXT-AZURE-ER-PA4": 12076,
    "EXT-AWS-VPN-EUC1": 64513,
    "EXT-ZSCALER-MUC1": 22616,
}


def _plan(row: dict[str, Any]) -> Any:
    """(flavour, links) the offline planner yields for a connection row."""
    return plan_links(parse_connection(_payload(row)))


def test_the_table_names_exactly_the_connections_of_the_data() -> None:
    """A connection added to the data is described in the table before the integration run is started."""
    assert set(ALL_DEMO_CONNECTIONS) == set(_ROWS)


@pytest.mark.parametrize("connection", sorted(ALL_DEMO_CONNECTIONS))
def test_the_table_row_states_what_the_data_says(connection: str) -> None:
    """Path type, service, namespace, VLAN, key, carriers, link ranges and the places of the connection."""
    expected, row = ALL_DEMO_CONNECTIONS[connection], _ROWS[connection]
    assert (expected["path_type"], expected["service"], expected["ip_namespace"], expected["vlan_id"]) == (
        row["path_type"],
        row["service"],
        row["ip_namespace"],
        row.get("vlan_id"),
    )
    assert expected["password"] == row.get("password")
    assert list(expected["carriers"]) == sorted(row["carrier_circuits"])
    assert sorted(expected["locations"]) == sorted([row["near"], row["far"]])
    assert sorted(link["link_prefix"] for link in expected["links"] if link["link_prefix"]) == sorted(
        prefix for prefix, _ in row.get("link_prefixes") or []
    )


@pytest.mark.parametrize("connection", sorted(ALL_DEMO_CONNECTIONS))
def test_the_table_flavour_and_links_are_the_planners(connection: str) -> None:
    """The restated flavour (address source, length, lower end, TTL, BFD) and every link's name, ends and range are what
    plan_links yields from the data; a changed FLAVOURS row or a wrong end shows here, not in the integration run."""
    expected = ALL_DEMO_CONNECTIONS[connection]
    flavour, links = _plan(_ROWS[connection])
    assert (
        expected["address_source"],
        expected["prefix_length"],
        expected["lower_end"],
        expected["ttl"],
        expected["bfd"],
    ) == (
        flavour.address_source,
        flavour.prefix_length,
        flavour.lower_end,
        flavour.ttl or 1,
        flavour.bfd or expected["path_type"] == "dedicated",
    )
    assert [link["name"] for link in expected["links"]] == [link.name for link in links]
    for table, planned in zip(expected["links"], links, strict=True):
        near = {
            (e["device"], e["interface"]): e["parent"] for e in table["ends"] if e["device"] == planned.near.device_name
        }
        assert len(near) == 1
        ((interface, parent),) = [((d, i), p) for (d, i), p in near.items()]
        assert planned.near.port is not None and parent == planned.near.port.name
        assert {e["device"] for e in table["ends"]} == {planned.near.device_name, planned.far.device_name}
        if expected["path_type"] == "dedicated":
            assert planned.far.port is not None
            assert {e["interface"] for e in table["ends"]} == {
                f"{planned.near.port.name}.{expected['vlan_id']}",
                f"{planned.far.port.name}.{expected['vlan_id']}",
            }
        else:
            assert {e["interface"] for e in table["ends"]} == {f"Tunnel{planned.index}"}
        assert interface[0] == planned.near.device_name


def test_the_party_asns_are_the_declared_processes() -> None:
    """The ASN in the table of every link is the `local_as` of the ManagedBGP process the data declares on the stub (or
    the connection's `provider_as` for a stub the generator builds)."""
    declared_asns = {
        str(device): int(str(process.item["local_as"]).removeprefix("AS"))
        for process in declared("ManagedBGP", _ALL)
        for device in process.item.get("capabilities") or []
    }
    for _, (connection, link) in sorted(ALL_DEMO_CONNECTION_LINKS.items()):
        if link["party"] is None:
            assert connection == "PC-FR2-AMNE-PROD"
            continue
        device, asn = link["party"]
        if device not in declared_asns:  # a stub the generator builds: its process speaks the connection's provider_as
            declared_asns[device] = int(str(_ROWS[connection]["provider_as"]).removeprefix("AS"))
        assert declared_asns[device] == asn == _PARTY_ASNS[device]


def test_the_handoffs_test_62_lists_are_the_one_link_connections_on_our_pool() -> None:
    """ALL_DEMO_VIRTUAL_CIRCUITS holds the handoffs the offline simulator can build (one link, our pool); the rest of the
    connection circuits are test_67's alone, and the two sets never overlap."""
    listed = {n for n, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["circuit_type"] == "handoff"} & set(
        ALL_DEMO_CONNECTION_LINKS
    )
    on_our_pool = {
        link["name"]
        for connection, expected in ALL_DEMO_CONNECTIONS.items()
        for link in expected["links"]
        if expected["path_type"] == "dedicated"
        and expected["address_source"] == ADDRESS_OUR_POOL
        and len(expected["links"]) == 1
    }
    assert listed == on_our_pool
    assert ALL_DEMO_CONNECTION_CIRCUITS == set(ALL_DEMO_CONNECTION_LINKS) - listed
    assert ALL_DEMO_CONNECTION_CIRCUITS >= {"PC-PA4-AZURE-WEU-PRI-PROD-L1", "PC-PA4-AZURE-WEU-SEC-PROD-L1"}
    assert ALL_DEMO_CONNECTION_HANDOFFS == {
        n for n, (c, _) in ALL_DEMO_CONNECTION_LINKS.items() if c in _handoff_names()
    }
    assert listed <= set(ALL_DEMO_HANDOFF_PARTY_ASNS) | {"PC-FR2-AMNE-PROD-L1"}


def _handoff_names() -> set[str]:
    return {c for c, e in ALL_DEMO_CONNECTIONS.items() if e["path_type"] == "dedicated"}


def test_every_circuit_has_a_derived_owner_entry_and_only_customers_own() -> None:
    """The owner table covers every virtual circuit and connection circuit; a hub connection of the operator's own
    account is unowned (a provider is no customer), a customer's account or SaaS connection names the customer."""
    assert set(ALL_DEMO_CIRCUIT_OWNERS) == set(ALL_DEMO_VIRTUAL_CIRCUITS) | set(ALL_DEMO_CONNECTION_LINKS)
    assert ALL_DEMO_CIRCUIT_OWNERS["PC-FR2-AWS-EUC1-PROD-L1"] is None
    assert ALL_DEMO_CIRCUIT_OWNERS["PC-FR2-AWS-EUC1-VPN-PROD-L2"] == "C001"
    assert ALL_DEMO_CIRCUIT_OWNERS["PC-PA4-AZURE-WEU-SEC-PROD-L1"] == "C004"
    assert all(owner is None or owner.startswith("C") for owner in ALL_DEMO_CIRCUIT_OWNERS.values())


# -- the premises of the disposable connection of test_67 ------------------------------------------------------------


def _disposable_row(**changes: Any) -> dict[str, Any]:
    """The Megaport connection row made into the disposable one (name, VLAN), with ``changes`` applied."""
    return {
        **_ROWS["PC-PA4-MEGAPORT-PROD"],
        "name": provider_connections.DISPOSABLE,
        "vlan_id": provider_connections.DISPOSABLE_VLAN,
        **changes,
    }


def test_the_disposable_connection_plans_a_handoff_on_the_vlan_that_no_data_uses() -> None:
    """test_67 test_04 creates it from the Megaport carrier: its two ends are `<port>.1399` of the carrier's ports, and
    VLAN 1399 is on no authored connection or sub-interface."""
    flavour, (link,) = _plan(_disposable_row())
    assert (flavour.address_source, flavour.prefix_length) == (ADDRESS_OUR_POOL, 31)
    assert link.name == provider_connections.DISPOSABLE_CIRCUIT and link.circuit_type == "handoff"
    assert (link.near.device_name, link.near.port.name) == provider_connections.DISPOSABLE_NEAR[1:]  # type: ignore[union-attr]
    assert (link.far.device_name, link.far.port.name) == provider_connections.DISPOSABLE_FAR[1:]  # type: ignore[union-attr]
    assert provider_connections.DISPOSABLE_VLAN not in {row.get("vlan_id") for row in _ROWS.values()}
    assert {device: name for device, name in provider_connections.DISPOSABLE_SUB_INTERFACES.items()} == {
        link.near.device_name: f"{link.near.port.name}.1399",  # type: ignore[union-attr]
        link.far.device_name: f"{link.far.port.name}.1399",  # type: ignore[union-attr]
    }
    assert provider_connections.DISPOSABLE_KEY == _ROWS["PC-PA4-MEGAPORT-PROD"]["password"]
    assert provider_connections.DISPOSABLE_AS == _ROWS["PC-PA4-MEGAPORT-PROD"]["provider_as"]


def test_the_disposable_connection_plans_a_vpn_over_the_pa4_transit_after_the_change() -> None:
    """With path_type internet_vpn and the Lumen transit as its carrier, the link is a vpn that leaves eg-pa401 by the
    transit's port and ends on the Megaport stub: the near tunnel's parent is the one test_04 expects."""
    row = _disposable_row(
        path_type="internet_vpn", vlan_id=None, carrier_circuits=[provider_connections.DISPOSABLE_VPN_CARRIER]
    )
    _, (link,) = _plan(row)
    assert link.circuit_type == "vpn"
    assert (link.near.device_name, link.near.port.name) == (  # type: ignore[union-attr]
        provider_connections.DISPOSABLE_NEAR[1],
        provider_connections.DISPOSABLE_VPN_PORT,
    )
    assert link.far.device_name == provider_connections.DISPOSABLE_FAR[1] and link.far.port is None


# -- the judges --------------------------------------------------------------------------------------------------------


def _row_for(connection: str) -> dict[str, Any]:
    """A fetch_connections entry built from the table."""
    expected = ALL_DEMO_CONNECTIONS[connection]
    return {
        "path_type": expected["path_type"],
        "service": expected["service"],
        "status": "active",
        "vlan_id": expected["vlan_id"],
        "ip_namespace": {"name": expected["ip_namespace"]},
        "password": {"name": expected["password"]} if expected["password"] else None,
        "owner": None,
        "account": None,
        "owner_org": expected["owner"],
        "account_owner_org": None,
        "carriers": sorted(expected["carriers"]),
        "sessions": sorted(link["name"] for link in expected["links"]),
        "link_prefix_list": sorted(link["link_prefix"] for link in expected["links"] if link["link_prefix"]),
    }


def _circuit_for(connection: str, number: int = 0) -> dict[str, Any]:
    """An inventory entry (circuit_helpers.fetch_interconnect_inventory) built from the table."""
    expected = ALL_DEMO_CONNECTIONS[connection]
    link = expected["links"][number]
    return {
        "name": link["name"],
        "circuit_type": "handoff" if expected["path_type"] == "dedicated" else "vpn",
        "connection": connection,
        "ends": sorted((e["device"], e["interface"]) for e in link["ends"]),
        "end_nodes": [
            {"device": e["device"], "interface": e["interface"], "parent_name": e["parent"]} for e in link["ends"]
        ],
        "locations": sorted(expected["locations"]),
        "ip_namespace": expected["ip_namespace"],
        "password": expected["password"],
        "link_prefix": link["link_prefix"],
        "owner_org": expected["owner"],
        "carrier": sorted(link["carrier"]),
    }


@pytest.mark.parametrize("connection", sorted(ALL_DEMO_CONNECTIONS))
def test_the_judges_accept_what_the_table_describes(connection: str) -> None:
    """A connection row and its circuits built from the table raise no finding."""
    expected = ALL_DEMO_CONNECTIONS[connection]
    assert connection_errors(_row_for(connection), connection, expected) == []
    for number, link in enumerate(expected["links"]):
        assert link_errors(_circuit_for(connection, number), expected, link, connection) == []


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda r: r.update(path_type="internet_vpn"), "path_type"),
        (lambda r: r.update(vlan_id=1001), "vlan_id"),
        (lambda r: r.update(sessions=[]), "session_circuits"),
        (lambda r: r.update(carriers=["XC-OTHER"]), "carrier_circuits"),
        (lambda r: r.update(password=None), "password"),
        (lambda r: r.update(link_prefix_list=["10.0.0.0/30"]), "link_prefixes"),
        (lambda r: r.update(owner_org="C999"), "derived owner"),
    ],
)
def test_a_connection_row_that_differs_is_named(change: Any, field: str) -> None:
    """Each wrong attribute of a connection is reported by name."""
    row = _row_for("PC-FR2-AWS-EUC1-PROD")
    change(row)
    errors = connection_errors(row, "PC-FR2-AWS-EUC1-PROD", ALL_DEMO_CONNECTIONS["PC-FR2-AWS-EUC1-PROD"])
    assert any(field in error for error in errors), errors


def test_a_missing_connection_and_an_owner_with_an_account_are_reported() -> None:
    """No row is one finding; naming both an account and an owner breaks PC5."""
    expected = ALL_DEMO_CONNECTIONS["PC-PA4-AZURE-WEU-PRI-PROD"]
    assert connection_errors(None, "PC-PA4-AZURE-WEU-PRI-PROD", expected) == [
        "PC-PA4-AZURE-WEU-PRI-PROD: no such connection on the branch"
    ]
    row = _row_for("PC-PA4-AZURE-WEU-PRI-PROD")
    row.update(account={"name": "a"}, owner={"id": "x"}, account_owner_org="C004", owner_org="C004")
    assert any("owner is derived from the account" in e for e in connection_errors(row, "x", expected))


@pytest.mark.parametrize(
    ("change", "field"),
    [
        (lambda c: c.update(circuit_type="vpn"), "circuit_type"),
        (lambda c: c.update(connection="PC-OTHER"), "connection"),
        (lambda c: c.update(ends=[("a", "b"), ("c", "d")]), "ends"),
        (lambda c: c.update(locations=["FR2"]), "locations"),
        (lambda c: c.update(link_prefix="10.0.0.0/30"), "link_prefix"),
        (lambda c: c.update(owner_org="C001"), "owner"),
        (lambda c: c.update(carrier=[]), "carrier"),
        (lambda c: c["end_nodes"][0].update(parent_name="Ethernet9/9"), "rides"),
    ],
)
def test_a_circuit_that_differs_from_its_link_is_named(change: Any, field: str) -> None:
    """Each wrong attribute of a generated circuit is reported by name; a missing circuit is one finding."""
    expected = ALL_DEMO_CONNECTIONS["PC-FR2-AWS-EUC1-PROD"]
    (link,) = expected["links"]
    circuit = _circuit_for("PC-FR2-AWS-EUC1-PROD")
    change(circuit)
    assert any(field in e for e in link_errors(circuit, expected, link, "PC-FR2-AWS-EUC1-PROD")), field
    assert link_errors(None, expected, link, "PC-FR2-AWS-EUC1-PROD") == [f"{link['name']}: no session circuit"]


def test_a_stray_handoff_or_vpn_circuit_is_reported() -> None:
    """A generated circuit no connection plans, and a handoff with no connection the data does not declare."""
    virtual = [
        {"name": "PC-X-L9", "circuit_type": "vpn", "connection": "PC-X"},
        {"name": "HANDOFF-ORPHAN", "circuit_type": "handoff", "connection": None},
        {"name": "HANDOFF-FR2-SDWAN-GW1-PROD", "circuit_type": "handoff", "connection": None},
        {"name": "ICX-1", "circuit_type": "dci", "connection": None},
    ]
    errors = unowned_circuit_errors(virtual, set(ALL_DEMO_CONNECTION_LINKS), {"HANDOFF-FR2-SDWAN-GW1-PROD"})
    assert len(errors) == 2
    assert any(e.startswith("PC-X-L9: belongs to PC-X") for e in errors)
    assert any(e.startswith("HANDOFF-ORPHAN: a handoff circuit with no connection") for e in errors)


def test_a_carrier_must_list_the_connections_that_list_it() -> None:
    """XC-EQXFR2-AWS-EUC1 is listed by two connections: it must list both back, and nothing else lists any."""
    physical = [
        {"circuit_id": "XC-EQXFR2-AWS-EUC1", "connections": ["PC-FR2-AWS-EUC1-PROD", "PC-AMNE-AWS-EUC1-PROD"]},
        {"circuit_id": "FABRIC-AMNE02-AWS-EUC1", "connections": ["PC-AMNE-AWS-EUC1-PROD"]},
        {"circuit_id": "DF-DC10-EQXFR2", "connections": []},
    ]
    only = {k: v for k, v in ALL_DEMO_CONNECTIONS.items() if k in ("PC-FR2-AWS-EUC1-PROD", "PC-AMNE-AWS-EUC1-PROD")}
    assert carrier_membership_errors(physical, only) == []
    physical[0]["connections"] = ["PC-FR2-AWS-EUC1-PROD"]
    physical[1]["connections"] = []
    physical[2]["connections"] = ["PC-STRAY"]
    errors = carrier_membership_errors(physical, only)
    assert any("XC-EQXFR2-AWS-EUC1: provider_connections" in e for e in errors)
    assert any("DF-DC10-EQXFR2: names a connection" in e for e in errors)


# -- the shared session rules -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prefix", "lower", "upper"),
    [
        ("10.0.0.0/31", "10.0.0.0", "10.0.0.1"),
        ("10.0.0.0/30", "10.0.0.1", "10.0.0.2"),
        ("10.0.0.8/29", "10.0.0.9", "10.0.0.10"),
    ],
)
def test_the_link_hosts_follow_the_prefix_length(prefix: str, lower: str, upper: str) -> None:
    """A /31 has no network address (RFC 3021); a /30 or /29 keeps it: the same offsets as utils.provider_services."""
    assert link_hosts(ipaddress.ip_network(prefix)) == (ipaddress.ip_address(lower), ipaddress.ip_address(upper))


def test_a_provider_assigned_pair_must_be_the_two_hosts_of_the_assigned_range() -> None:
    """The VPN tunnel range 169.254.10.0/30: hosts .1 and .2 pass; the network address, another range, an unaddressed
    end and a range of the wrong length are each reported."""
    flavour = LinkFlavour(30, END_PROVIDER, 1, False, True)
    circuit = {"link_prefix": "169.254.10.0/30"}
    assert _assigned_errors("s", circuit, flavour, ["169.254.10.1/30", "169.254.10.2/30"]) == []
    assert _assigned_errors("s", circuit, flavour, ["169.254.10.0/30", "169.254.10.2/30"])
    assert _assigned_errors("s", circuit, flavour, ["169.254.10.5/30", "169.254.10.6/30"])
    assert _assigned_errors("s", circuit, flavour, ["169.254.10.1/30", None])
    assert any(
        "not a /29" in e
        for e in _assigned_errors(
            "s", circuit, LinkFlavour(29, END_PROVIDER, 1, False, True), ["169.254.10.1/30", "169.254.10.2/30"]
        )
    )
    assert _assigned_errors("s", {"link_prefix": None}, flavour, ["169.254.10.1/30", "169.254.10.2/30"]) == [
        "s: a provider-assigned link has no link_prefix on its circuit"
    ]


def test_the_provider_assigned_rows_of_the_table_have_a_range_for_every_link() -> None:
    """Exactly the provider-assigned connections carry authored ranges, one per link."""
    for connection, expected in ALL_DEMO_CONNECTIONS.items():
        ranges = [link["link_prefix"] for link in expected["links"]]
        if expected["address_source"] == ADDRESS_PROVIDER:
            assert all(ranges), connection
        else:
            assert not any(ranges), connection


# -- registration -------------------------------------------------------------------------------------------------------


def test_test_67_follows_test_66_and_runs_last_in_the_30_all_task() -> None:
    """test_67 depends on test_66's last test and the 30_all invoke task lists it after test_66."""
    source = (ROOT / "tests/integration/test_67_all_demo_provider_connections.py").read_text()
    assert '"all_demo_mlag_remove_readd"' in source
    lifecycle = (ROOT / "tests/integration/test_66_all_demo_circuit_lifecycle.py").read_text()
    assert 'name="all_demo_mlag_remove_readd"' in lifecycle
    tasks = (ROOT / "tasks.py").read_text()
    assert tasks.index("test_66_all_demo_circuit_lifecycle.py") < tasks.index(
        "test_67_all_demo_provider_connections.py"
    )


def test_the_idempotency_run_reruns_the_connection_generator_before_add_virtual_circuit() -> None:
    """add_provider_connection writes the circuits add_virtual_circuit addresses, so its rerun precedes it."""
    order = list(idempotency.TOPOLOGY_RERUN_GENERATORS)
    assert idempotency.TOPOLOGY_RERUN_GENERATORS["add_provider_connection"] == "provider_connections"
    assert order.index("add_circuit") < order.index("add_provider_connection") < order.index("add_virtual_circuit")


def test_the_rerun_snapshot_covers_the_connection_and_its_outputs() -> None:
    """The kinds test_03 snapshots all have a selection in snapshot_helpers; the connection selects its circuits."""
    from tests.integration.snapshot_helpers import TOPOLOGY_SNAPSHOT_KINDS

    assert set(provider_connections.CONNECTION_SNAPSHOT_KINDS) <= set(TOPOLOGY_SNAPSHOT_KINDS)
    selection = TOPOLOGY_SNAPSHOT_KINDS["TopologyProviderConnection"]
    for field in ("vlan_id { value }", "session_circuits", "carrier_circuits", "link_prefixes"):
        assert field in selection
    assert "connection { node { id } }" in TOPOLOGY_SNAPSHOT_KINDS["TopologySessionCircuit"]
    assert "link_prefix { node { id } }" in TOPOLOGY_SNAPSHOT_KINDS["TopologySessionCircuit"]


def test_the_judges_do_not_mutate_the_table() -> None:
    """The constants are shared by every module: the judges and the builders above leave them as they were."""
    before = copy.deepcopy(ALL_DEMO_CONNECTIONS)
    connection_errors(
        _row_for("PC-FR2-AWS-EUC1-PROD"), "PC-FR2-AWS-EUC1-PROD", ALL_DEMO_CONNECTIONS["PC-FR2-AWS-EUC1-PROD"]
    )
    assert ALL_DEMO_CONNECTIONS == before
