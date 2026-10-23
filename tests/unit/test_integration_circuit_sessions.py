"""The test_62 session tests (DCI, handoff, tunnel, group), run offline against a stack that behaves like the generator.

test_06, test_06b, test_06c and test_02a of tests/integration/test_62 read what add_virtual_circuit wrote: sessions,
addresses, prefixes, keys. That output exists only on a stack, so a typo in a query path or a rule that the generator's
real output would break shows up first in a 55-minute run. ``FakeStack`` answers the queries of those tests with the
shipped 30_all circuits and the sessions the generator plans for them (generators/topology/virtual_circuit.py): the DCI
pair of sessions on a /127 of the DCI pool, the handoff's one session on a /31 of its namespace pool, the party stubs'
ASNs of the data. The tests then run the real test methods: they must pass on that output, and they must fail, naming
the field, when one thing of it is wrong.

The mirror of the generator is the assumption: what the generator really writes is proven by its own unit tests
(test_virtual_circuit_session.py) and, on a stack, by the integration run.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.integration import test_62_all_demo_interconnects as interconnects
from tests.integration import test_helpers
from tests.integration.test_constants import (
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_HANDOFF_PARTY_ASNS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    HANDOFF_KIND,
    TUNNEL_KIND,
)
from tests.unit.simulators.circuit_inventory_payloads import inventory_payload


def _value(value: Any) -> dict[str, Any]:
    """A GraphQL attribute: ``{value: ...}``."""
    return {"value": value}


def _interface(
    device: str,
    name: str,
    *,
    address: str | None,
    namespace: str,
    description: str,
    mtu: int | None = None,
    role: str | None = None,
    parent: str | None = None,
) -> dict[str, Any]:
    """An interface node of SESSIONS_QUERY: a physical one with an MTU, or a virtual one with a role (and a parent)."""
    node: dict[str, Any] = {
        "name": _value(name),
        "device": {"node": {"name": _value(device)}},
        "description": _value(description),
        "status": _value("active"),
        "ip_address": {
            "node": {"address": _value(address), "ip_namespace": {"node": {"name": _value(namespace)}}}
            if address
            else None
        },
    }
    if mtu is not None:
        node["mtu"] = _value(mtu)
    if role is not None or parent is not None:
        node["role"] = _value(role)
        node["parent_interface"] = {"node": {"name": _value(parent)} if parent else None}
    return node


def _session(
    name: str,
    *,
    role: str,
    session_type: str,
    ttl: int,
    bfd: bool,
    extended: bool,
    maximum_routes: int | None,
    key: str,
    families: list[tuple[str, str]],
    processes: list[tuple[str, str, str, int]],
    interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """A ManagedBGPPeering node of SESSIONS_QUERY; processes are (device, name, process_role, asn)."""
    return {
        "name": _value(name),
        "status": _value("active"),
        "peering_role": _value(role),
        "session_type": _value(session_type),
        "ttl": _value(ttl),
        "bfd_enabled": _value(bfd),
        "send_extended_community": _value(extended),
        "maximum_routes": _value(maximum_routes),
        "password": {"node": {"name": _value(key)}},
        "address_families": {"edges": [{"node": {"afi": _value(afi), "safi": _value(safi)}} for afi, safi in families]},
        "bgp_processes": {
            "edges": [
                {
                    "node": {
                        "name": _value(process),
                        "process_role": _value(process_role),
                        "local_as": {"node": {"asn": _value(asn)}},
                        "capabilities": {"edges": [{"node": {"name": _value(device)}}]},
                    }
                }
                for device, process, process_role, asn in processes
            ]
        },
        "capabilities": {"edges": [{"node": node} for node in interfaces]},
    }


def _prefix(prefix: str, description: str, namespace: str) -> dict[str, Any]:
    """A technical IpamPrefix node of CIRCUIT_PREFIXES_QUERY."""
    return {
        "prefix": _value(prefix),
        "description": _value(description),
        "role": _value("technical"),
        "ip_namespace": {"node": {"name": _value(namespace)}},
    }


class FakeStack:
    """What the stack holds after the generators ran, and the answers it gives to the test_62 queries."""

    def __init__(self) -> None:
        """Plan the sessions of the shipped DCIs and handoffs, then derive the inventory and group members."""
        self.sessions: dict[str, dict[str, Any]] = {}
        self.prefixes: dict[str, dict[str, Any]] = {}
        self.group_members: list[dict[str, Any]] = []
        self.roles: dict[str, int] = {}
        self.legacy_addresses = 0
        self.pool = "10.255.128.0/20"
        handoff_addresses: dict[tuple[str, str], str] = {}
        self._dci()
        base = inventory_payload()
        self._handoffs(base, handoff_addresses)
        self.inventory = inventory_payload(addresses=handoff_addresses)
        self.group_members = [
            {"id": node["id"], "__typename": node["__typename"], "display_label": node["name"]["value"]}
            for node in (e["node"] for e in self.inventory["TopologyVirtualCircuit"]["edges"])
            if node["__typename"] != "TopologyTunnelCircuit"
        ]

    def _dci(self) -> None:
        """Per DCI: /127 ends (point-to-point IPv6 link prefix), ports at jumbo MTU 9216, two sessions, one key."""
        for index, (circuit, (_, ends)) in enumerate(sorted(ALL_DEMO_DCI_CIRCUITS.items())):
            edge = next(end for end in ends if end[0].startswith("eg-"))
            dc_end = next(end for end in ends if end != edge)
            addresses = {edge: f"fd00:2200::{2 * index:x}/127", dc_end: f"fd00:2200::{2 * index + 1:x}/127"}
            processes = [(d, f"{d}-bgp-overlay", "overlay", 4200000000 + n) for n, (d, _) in enumerate(ends)]
            key = f"{circuit}-dci-key"
            ports = [
                _interface(
                    device,
                    port,
                    address=addresses[(device, port)],
                    namespace="default",
                    description=f"DCI {circuit} to {peer[0]} {peer[1]}",
                    mtu=9216,
                )
                for (device, port), peer in ((ends[0], ends[1]), (ends[1], ends[0]))
            ]
            loopbacks = [
                _interface(
                    device,
                    "loopback1",
                    address=f"2001:db8:{n}::1/128",
                    namespace="default",
                    description="VTEP",
                    role="loopback-vtep",
                )
                for n, (device, _) in enumerate(ends)
            ]
            self.sessions[f"{circuit}-underlay"] = _session(
                f"{circuit}-underlay",
                role="dci-underlay",
                session_type="EBGP",
                ttl=1,
                bfd=True,
                extended=False,
                maximum_routes=16,
                key=key,
                families=[("ipv6", "unicast")],
                processes=processes,
                interfaces=ports,
            )
            self.sessions[f"{circuit}-evpn"] = _session(
                f"{circuit}-evpn",
                role="dci",
                session_type="EBGP_MULTIHOP",
                ttl=2,
                bfd=False,
                extended=True,
                maximum_routes=None,
                key=key,
                families=[("l2vpn", "evpn")],
                processes=processes,
                interfaces=loopbacks,
            )
            self.prefixes[f"DCI {circuit}"] = _prefix(f"fd00:2200::{2 * index:x}/127", f"DCI {circuit}", "default")

    def _handoffs(self, payload: dict[str, Any], addresses: dict[tuple[str, str], str]) -> None:
        """Per handoff: one session over a /31 of the namespace pool, our overlay end on the lower address."""
        nodes = {e["node"]["name"]["value"]: e["node"] for e in payload["TopologyVirtualCircuit"]["edges"]}
        handoffs = sorted(n for n, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["kind"] == HANDOFF_KIND)
        for index, name in enumerate(handoffs):
            expected = ALL_DEMO_VIRTUAL_CIRCUITS[name]
            namespace = str(expected["ip_namespace"])
            ends = list(expected["ends"])
            party = ALL_DEMO_HANDOFF_PARTY_ASNS.get(name, ("", 0))[0]
            devices = [device for device, _ in ends]
            process_roles = {device: "underlay" if device == party else "overlay" for device in devices}
            asns = {
                device: ALL_DEMO_HANDOFF_PARTY_ASNS[name][1] if device == party else 4200000100 + n
                for n, device in enumerate(devices)
            }
            lower = min(devices, key=lambda device: (process_roles[device] != "overlay", device))
            assigned = {end: f"10.255.128.{2 * index + (0 if end[0] == lower else 1)}/31" for end in ends}
            addresses.update(assigned)
            interfaces = [
                _interface(
                    device,
                    port,
                    address=assigned[(device, port)],
                    namespace=namespace,
                    description=f"Handoff {name} to {peer[0]} {peer[1]}",
                    role="uplink",
                    parent=port.rpartition(".")[0],
                )
                for (device, port), peer in ((ends[0], ends[1]), (ends[1], ends[0]))
            ]
            node = nodes[name]
            self.sessions[f"{name}-bgp"] = _session(
                f"{name}-bgp",
                role="regular",
                session_type="EBGP",
                ttl=1,
                bfd=True,
                extended=False,
                maximum_routes=node["maximum_routes"]["value"],
                key=node["password"]["node"]["name"]["value"],
                families=[("ipv4", "unicast")],
                processes=[(device, f"{device}-bgp", process_roles[device], asns[device]) for device in devices],
                interfaces=interfaces,
            )
            self.prefixes[f"Handoff {name}"] = _prefix(f"10.255.128.{2 * index}/31", f"Handoff {name}", namespace)

    # -- the client ---------------------------------------------------------------------------------------------------

    async def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None, branch_name: str | None = None
    ) -> dict[str, Any]:
        """Answer the queries of the test_62 session tests from the planned state; any other query is a test bug."""
        variables = variables or {}
        if query == test_helpers.QUERY_GET_INTERCONNECT_INVENTORY:
            return self.inventory
        if query == interconnects.SESSIONS_QUERY:
            nodes = [self.sessions[n] for n in variables["names"] if n in self.sessions]
            return {"ManagedBGPPeering": {"edges": [{"node": node} for node in nodes]}}
        if "peering_role__value" in query:
            return {"ManagedBGPPeering": {"count": self.roles[variables["role"]]}}
        if query == interconnects.CIRCUIT_PREFIXES_QUERY:
            nodes = [self.prefixes[d] for d in variables["descriptions"] if d in self.prefixes]
            return {"IpamPrefix": {"edges": [{"node": node} for node in nodes]}}
        if query == interconnects.EDGE_PAIR_POOL_QUERY:
            resource = {"node": {"prefix": _value(self.pool)}}
            node = {"name": _value(variables["name"]), "resources": {"edges": [resource]}}
            return {"CoreIPPrefixPool": {"edges": [{"node": node}]}}
        if query == interconnects.ADDRESS_COUNT_QUERY:
            return {"IpamIPAddress": {"count": self.legacy_addresses}}
        if query == interconnects.GROUP_MEMBERS_QUERY:
            members = [{"node": member} for member in self.group_members]
            return {"CoreGroup": {"edges": [{"node": {"members": {"edges": members}}}]}}
        raise AssertionError(f"unexpected query {query[:80]!r}")


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> FakeStack:
    """A fake stack with the DCI role counts the stack run expects: one underlay and one evpn session per DCI."""
    monkeypatch.setattr(test_helpers, "DATA_PROPAGATION_DELAY", 0)
    fake = FakeStack()
    fake.roles = {"dci": len(ALL_DEMO_DCI_CIRCUITS), "dci-underlay": len(ALL_DEMO_DCI_CIRCUITS)}
    return fake


def _client(stack: FakeStack) -> MagicMock:
    """A client whose ``execute_graphql`` is answered by the fake stack."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(side_effect=stack.execute_graphql)
    return client


def _tests() -> interconnects.TestAllDemoInterconnects:
    """The real test_62 class, whose test methods run against the fake stack."""
    return interconnects.TestAllDemoInterconnects()


def _addresses(stack: FakeStack, session: str) -> list[dict[str, Any]]:
    """The interface nodes (ports or loopbacks) a session is addressed on."""
    return [e["node"] for e in stack.sessions[session]["capabilities"]["edges"]]


# -- DCI: two keyed sessions per Interconnect circuit -----------------------------------------------------------------------


async def test_the_dci_sessions_the_generator_plans_pass(stack: FakeStack) -> None:
    """The sessions the generator plans for every shipped DCI satisfy the real test_06 rules."""
    await _tests().test_06_verify_dci_sessions(_client(stack), "main")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s.sessions["ICX-DC10-EQXFR2-underlay"].update(ttl=_value(2)), "ICX-DC10-EQXFR2-underlay: ttl 2"),
        (
            lambda s: s.sessions["ICX-DC10-EQXFR2-evpn"].update(session_type=_value("EBGP")),
            "ICX-DC10-EQXFR2-evpn: session_type 'EBGP'",
        ),
        (
            lambda s: s.sessions["ICX-DC10-EQXFR2-evpn"].update(
                password={"node": {"name": _value("dc10-overlay-key")}}
            ),
            "key 'dc10-overlay-key', expected 'ICX-DC10-EQXFR2-dci-key'",
        ),
        (lambda s: _addresses(s, "ICX-DC10-EQXFR2-underlay")[0].update(mtu=_value(1500)), "MTU 1500, expected 9216"),
        (
            lambda s: s.sessions.pop("ICX-DC12-EQXPA4-B-evpn"),
            "DCI sessions",
        ),
        (
            lambda s: s.prefixes.pop("DCI ICX-DC11-EQXFR2"),
            "ICX-DC11-EQXFR2-underlay: 0 prefix object(s)",
        ),
        (lambda s: s.roles.update(dci=7), "7 session(s) with peering_role dci"),
    ],
)
async def test_a_dci_session_that_differs_from_the_plan_fails_naming_the_field(
    stack: FakeStack, mutate: Any, message: str
) -> None:
    """Each single deviation from the plan (ttl, type, key, MTU 1500 vs 9216, missing) fails naming its field."""
    mutate(stack)
    with pytest.raises(AssertionError, match=message.replace("(", r"\(").replace(")", r"\)")):
        await _tests().test_06_verify_dci_sessions(_client(stack), "main")


async def test_a_legacy_dci_session_per_fibre_fails(stack: FakeStack) -> None:
    """A DCI-DF-* session per dark fibre, the model before circuits carried the sessions, must be gone."""
    stack.sessions["DCI-DF-DC10-EQXFR2"] = {
        **stack.sessions["ICX-DC10-EQXFR2-underlay"],
        "name": _value("DCI-DF-DC10-EQXFR2"),
    }
    with pytest.raises(AssertionError, match=r"legacy session\(s\) \['DCI-DF-DC10-EQXFR2'\] still exist"):
        await _tests().test_06_verify_dci_sessions(_client(stack), "main")


async def test_a_dci_edge_with_the_higher_address_fails(stack: FakeStack) -> None:
    """The facility end takes the lower address: swap the two ports' addresses."""
    ports = _addresses(stack, "ICX-DC10-EQXFR2-underlay")
    first, second = (p["ip_address"]["node"]["address"] for p in ports)
    first["value"], second["value"] = second["value"], first["value"]
    with pytest.raises(AssertionError, match="the facility end takes the lower address"):
        await _tests().test_06_verify_dci_sessions(_client(stack), "main")


async def test_a_dci_evpn_session_on_the_multisite_vip_fails(stack: FakeStack) -> None:
    """The EVPN session sources from the VTEP loopback, never the multisite VIP shared by the gateways of a site."""
    _addresses(stack, "ICX-DC10-EQXFR2-evpn")[0]["role"] = _value("multisite-vip")
    with pytest.raises(AssertionError, match="expected the VTEP loopback"):
        await _tests().test_06_verify_dci_sessions(_client(stack), "main")


# -- Handoff: one session per circuit, from our pool ------------------------------------------------------------------------


async def test_the_handoff_sessions_the_generator_plans_pass(stack: FakeStack) -> None:
    """The sessions the generator plans for every shipped handoff satisfy the real test_06b rules."""
    await _tests().test_06b_verify_handoff_sessions(_client(stack), "main")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda s: s.sessions["HANDOFF-FR2-AWS-EUC1-PROD-bgp"].update(ttl=_value(255)), "ttl 255"),
        (
            lambda s: s.sessions["HANDOFF-FR2-AWS-EUC1-PROD-bgp"].update(maximum_routes=_value(None)),
            "maximum_routes None, expected 100",
        ),
        (lambda s: s.sessions.pop("HANDOFF-PA4-MEGAPORT-PROD-bgp"), "no HANDOFF-PA4-MEGAPORT-PROD-bgp session"),
        (lambda s: s.prefixes.pop("Handoff HANDOFF-FR2-SDWAN-GW1-PROD"), "0 prefix object(s)"),
        (lambda s: setattr(s, "legacy_addresses", 2), "2 authored handoff address"),
        (lambda s: setattr(s, "pool", "10.0.0.0/20"), "is not a /31 from"),
        (
            lambda s: _addresses(s, "HANDOFF-FR2-AWS-EUC1-PROD-bgp")[0]["ip_address"]["node"]["ip_namespace"]["node"][
                "name"
            ].update(value="NON-PROD"),
            "is addressed in 'NON-PROD', expected 'PROD'",
        ),
    ],
)
async def test_a_handoff_session_that_differs_from_the_plan_fails_naming_the_field(
    stack: FakeStack, mutate: Any, message: str
) -> None:
    """Each single deviation from the plan (ttl, route limit, missing, legacy, pool) fails naming its field."""
    mutate(stack)
    with pytest.raises(AssertionError, match=message.replace("(", r"\(").replace(")", r"\)")):
        await _tests().test_06b_verify_handoff_sessions(_client(stack), "main")


async def test_a_legacy_authored_session_still_present_fails(stack: FakeStack) -> None:
    """A hand-authored VRF-* session that predates the generated one must be gone."""
    stack.sessions["VRF-PROD-EQXFR2-SDWAN"] = stack.sessions["HANDOFF-FR2-SDWAN-GW1-PROD-bgp"]
    stack.sessions["VRF-PROD-EQXFR2-SDWAN"]["name"] = _value("VRF-PROD-EQXFR2-SDWAN")
    with pytest.raises(AssertionError, match=r"authored session\(s\) \['VRF-PROD-EQXFR2-SDWAN'\] still exist"):
        await _tests().test_06b_verify_handoff_sessions(_client(stack), "main")


async def test_a_handoff_with_the_remote_asn_of_the_data_changed_fails(stack: FakeStack) -> None:
    """The party's ASN is the data's (AS64512 for AWS DX); AS 64999 is also private but not the declared one."""
    for edge in stack.sessions["HANDOFF-FR2-AWS-EUC1-PROD-bgp"]["bgp_processes"]["edges"]:
        if edge["node"]["capabilities"]["edges"][0]["node"]["name"]["value"] == "EXT-AWS-DX-FR2":
            edge["node"]["local_as"]["node"]["asn"]["value"] = 64999
    with pytest.raises(AssertionError, match=r"party EXT-AWS-DX-FR2 is in AS 64999, expected the data's AS64512"):
        await _tests().test_06b_verify_handoff_sessions(_client(stack), "main")


async def test_a_handoff_whose_lower_address_is_on_the_party_end_fails(stack: FakeStack) -> None:
    """Our end (the overlay process) takes the lower address of the /31: swap them."""
    ports = _addresses(stack, "HANDOFF-FR2-SDWAN-GW1-PROD-bgp")
    addresses = [p["ip_address"]["node"]["address"] for p in ports]
    addresses[0]["value"], addresses[1]["value"] = addresses[1]["value"], addresses[0]["value"]
    # the inventory (circuit side) carries the same swap, so only the lower-end rule is left to fail
    nodes = {e["node"]["name"]["value"]: e["node"] for e in stack.inventory["TopologyVirtualCircuit"]["edges"]}
    circuit_ports = [e["node"] for e in nodes["HANDOFF-FR2-SDWAN-GW1-PROD"]["capabilities"]["edges"]]
    swapped = {p["name"]["value"]: p["ip_address"]["node"]["address"] for p in circuit_ports}
    names = list(swapped)
    swapped[names[0]]["value"], swapped[names[1]]["value"] = swapped[names[1]]["value"], swapped[names[0]]["value"]
    with pytest.raises(AssertionError, match="not the lower address of the /31"):
        await _tests().test_06b_verify_handoff_sessions(_client(stack), "main")


# -- Tunnel and group ---------------------------------------------------------------------------------------------------------


async def test_tunnels_without_a_session_pass(stack: FakeStack) -> None:
    """No session exists for any tunnel circuit, which is what test_06c asserts."""
    await _tests().test_06c_verify_tunnels_have_no_session(_client(stack), "main")


@pytest.mark.parametrize("suffix", ["", "-bgp", "-underlay", "-evpn"])
async def test_a_tunnel_with_a_session_of_any_name_fails(stack: FakeStack, suffix: str) -> None:
    """A session named after a tunnel, bare or with any generated suffix, fails: tunnel routing is SD-WAN's."""
    name = f"C001-OFFICE-SDWAN-FR2{suffix}"
    stack.sessions[name] = stack.sessions["HANDOFF-FR2-SDWAN-GW1-PROD-bgp"]
    stack.sessions[name]["name"] = _value(name)
    with pytest.raises(AssertionError, match="a tunnel has none"):
        await _tests().test_06c_verify_tunnels_have_no_session(_client(stack), "main")


async def test_the_group_holds_exactly_the_interconnects_and_handoffs(stack: FakeStack) -> None:
    """The virtual-circuits group has the DCIs and handoffs, the circuits add_virtual_circuit runs on, and no tunnel."""
    await _tests().test_02a_verify_virtual_circuits_group(_client(stack), "main")
    assert len(stack.group_members) == len(ALL_DEMO_DCI_CIRCUITS) + sum(
        1 for e in ALL_DEMO_VIRTUAL_CIRCUITS.values() if e["kind"] == HANDOFF_KIND
    )


async def test_a_tunnel_in_the_group_or_a_circuit_missing_from_it_fails(stack: FakeStack) -> None:
    """A tunnel in the group would raise inside the generator, and a session circuit left out would never run."""
    nodes = [e["node"] for e in stack.inventory["TopologyVirtualCircuit"]["edges"]]
    tunnel = next(n for n in nodes if n["__typename"] == TUNNEL_KIND)
    stack.group_members.append({"id": tunnel["id"], "__typename": TUNNEL_KIND, "display_label": "tunnel"})
    with pytest.raises(AssertionError, match="a tunnel circuit is in the group"):
        await _tests().test_02a_verify_virtual_circuits_group(_client(stack), "main")
    stack.group_members = stack.group_members[1:-1]
    with pytest.raises(AssertionError, match="not in the group"):
        await _tests().test_02a_verify_virtual_circuits_group(_client(stack), "main")
