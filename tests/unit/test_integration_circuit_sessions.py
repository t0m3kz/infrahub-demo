"""The test_62 session tests (DCI, handoff, tunnel, group), run offline against a stack that behaves like the generator.

test_06, test_06b, test_06c and test_02a of tests/integration/test_62 read what add_virtual_circuit wrote: sessions,
addresses, prefixes, keys. That output exists only on a stack, so a typo in a query path or a rule that the generator's
real output would break shows up first in a 55-minute run. ``FakeStack`` answers the queries of those tests with what
the REAL generator (generators/topology/virtual_circuit.py) writes when it runs over the shipped 30_all circuits: its
planners decide the pool, the address family, the peerings (ttl, route limit, role, key) and which end takes the lower
address, its write path addresses the ports and creates the peerings. Only the server is simulated: it hands out the
next /31 (/127) of the bootstrap pool, keeps what the generator saves, and resolves ids when a test query reads it back.
What the generator reads (the circuits' ports and carriers, the devices' BGP processes with their ASNs, their
loopbacks) is the state of the shipped data, not a decision of the generator, and is set up here.

The tests then run the real test methods: they must pass on that output, and they must fail, naming the field, when
one thing of it is wrong. A drift guard changes what a planner decides and shows the same methods then fail, so the
fake cannot keep a rule the generator no longer has.

What the generator really writes is proven by its own unit tests (test_virtual_circuit_session.py) and, on a stack, by
the integration run.
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.topology import virtual_circuit
from generators.topology.virtual_circuit import SessionEnd, SessionPlan, VirtualCircuitSessionGenerator
from tests.integration import circuit_helpers, interconnect_queries, query_files
from tests.integration import test_62_all_demo_interconnects as interconnects
from tests.integration import test_62c_all_demo_circuit_sessions as session_steps
from tests.integration.all_demo_circuit_constants import (
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_HANDOFF_PARTY_ASNS,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    HANDOFF_KIND,
    TUNNEL_KIND,
)
from tests.integration.demo_data import BOOTSTRAP, attribute, declared, name
from tests.unit.simulators.circuit_fakes import FakeClient, session_generator
from tests.unit.simulators.circuit_inventory_payloads import inventory_payload

_FIRST_GENERATED_ASN = 4200000000
# The role of a handoff sub-interface in its device's template (the generator keeps it).
_SUBINTERFACE_ROLE = "uplink"
# The loopbacks every device carries: a plain one and the VTEP one the EVPN session must prefer.
_LOOPBACKS = (("loopback0", "loopback"), ("loopback1", "loopback-vtep"))
_BOOTSTRAP_POOL_FILES = ("20_dci_pools.yml", "26_handoff_pools.yml")


def _value(value: Any) -> dict[str, Any]:
    """A GraphQL attribute: ``{value: ...}``."""
    return {"value": value}


def _interface(
    device: str,
    name: str,
    *,
    address: str | None,
    namespace: str | None,
    description: str,
    status: str,
    mtu: int | None = None,
    role: str | None = None,
    parent: str | None = None,
) -> dict[str, Any]:
    """An interface node of SESSIONS_QUERY: a physical one with an MTU, or a virtual one with a role (and a parent)."""
    node: dict[str, Any] = {
        "name": _value(name),
        "device": {"node": {"name": _value(device)}},
        "description": _value(description),
        "status": _value(status),
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
    status: str,
    ttl: int,
    bfd: bool,
    extended: bool,
    maximum_routes: int | None,
    key: str | None,
    families: list[tuple[str, str]],
    processes: list[tuple[str, str, str, int]],
    interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """A ManagedBGPPeering node of SESSIONS_QUERY; processes are (device, name, process_role, asn)."""
    return {
        "name": _value(name),
        "status": _value(status),
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


def _prefix(prefix: str, description: str, role: str, namespace: str) -> dict[str, Any]:
    """An IpamPrefix node of CIRCUIT_PREFIXES_QUERY."""
    return {
        "prefix": _value(prefix),
        "description": _value(description),
        "role": _value(role),
        "ip_namespace": {"node": {"name": _value(namespace)}},
    }


# -- the state the generator reads: the shipped data, with the ids the generator is given -------------------------------------


@dataclass(frozen=True)
class _Template:
    """An interface of a device as its template (the shipped data) has it; the generator addresses it, never creates it."""

    device: str
    name: str
    role: str | None = None
    parent: str | None = None
    # A loopback's own address; a session port is addressed by the generator.
    address: str | None = None


@dataclass
class _Devices:
    """The BGP processes, loopbacks and port templates the generator reads, keyed by the ids it is given.

    The ASN of a process is data: a declared party stub has the ASN of the data, every other process its own.
    """

    party_asns: dict[str, int] = field(default_factory=lambda: dict(ALL_DEMO_HANDOFF_PARTY_ASNS.values()))
    processes: dict[str, tuple[str, str, str, int]] = field(default_factory=dict)
    interfaces: dict[str, _Template] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    _devices: list[str] = field(default_factory=list)

    def process(self, device: str, index: int, role: str) -> dict[str, Any]:
        """A ManagedBGP of a device as virtual_circuit.gql selects it; its ASN is registered under its id."""
        process_id = f"proc-{device}-{index}"
        if process_id not in self.processes:
            asn = self.party_asns.get(device, _FIRST_GENERATED_ASN + len(self.processes))
            self.processes[process_id] = (device, f"{device}-bgp-{role}", role, asn)
        return {
            "id": process_id,
            "__typename": "ManagedBGP",
            "name": _value(self.processes[process_id][1]),
            "process_role": _value(role),
        }

    def loopbacks(self, device: str) -> list[dict[str, Any]]:
        """The loopbacks virtual_circuit.gql selects on a device, one IPv6 /128 each, registered under their ids."""
        if device not in self._devices:
            self._devices.append(device)
        nodes = []
        for index, (loopback, role) in enumerate(_LOOPBACKS):
            address = f"2001:db8:{self._devices.index(device):x}::{index + 1}/128"
            loopback_id = f"lo-{device}-{loopback}"
            self.interfaces[loopback_id] = _Template(device, loopback, role=role, address=address)
            nodes.append(
                {
                    "id": loopback_id,
                    "__typename": "DcimVirtualInterface",
                    "name": _value(loopback),
                    "role": _value(role),
                    "ip_address": {"node": {"address": _value(address)}},
                }
            )
        return nodes

    def port(self, port: dict[str, Any], device: str) -> dict[str, Any]:
        """A session end as virtual_circuit.gql selects it, from the inventory's port of the same circuit."""
        name = port["name"]["value"]
        parent = name.rpartition(".")[0] if port["__typename"] == "DcimVirtualInterface" else None
        self.interfaces[port["id"]] = _Template(
            device, name, role=_SUBINTERFACE_ROLE if parent else None, parent=parent
        )
        node = port["device"]["node"]
        deployment = dict(node["deployment"]["node"])
        if deployment["__typename"] == "TopologyDataCenter":
            deployment["underlay_protocol"] = _value(_underlay_protocol())
        selected: dict[str, Any] = {
            key: port[key]
            for key in ("id", "__typename", "name", "capabilities", "cable", "parent_interface")
            if key in port
        }
        selected["device"] = {
            "node": {
                "id": node["id"],
                "name": node["name"],
                "deployment": {"node": deployment},
                "capabilities": {
                    "edges": [
                        {"node": self.process(device, index, process["node"]["process_role"]["value"])}
                        for index, process in enumerate(node["capabilities"]["edges"])
                    ]
                },
                "interfaces": {"edges": [{"node": loopback} for loopback in self.loopbacks(device)]},
            }
        }
        return selected

    def circuit(self, circuit: dict[str, Any]) -> dict[str, Any]:
        """The virtual_circuit.gql response for one circuit of the inventory; a tunnel is only named, like the query."""
        selected: dict[str, Any] = {
            key: circuit[key]
            for key in ("id", "__typename", "name", "ip_namespace", "maximum_routes")
            if key in circuit
        }
        selected["status"] = _value("active")
        if "password" in circuit:
            key_name = circuit["password"]["node"]["name"]["value"]
            self.keys[f"key-{key_name}"] = key_name
            selected["password"] = {"node": {"id": f"key-{key_name}"}}
        selected["capabilities"] = {
            "edges": [
                {"node": self.port(edge["node"], edge["node"]["device"]["node"]["name"]["value"])}
                for edge in circuit["capabilities"]["edges"]
            ]
        }
        return {"TopologyVirtualCircuit": {"edges": [{"node": selected}]}}


def _underlay_protocol() -> str:
    """The underlay_protocol of the 30_all data centers: none declares one, so the schema default applies."""
    default = (attribute("TopologyDataCenter", "underlay_protocol") or {}).get("default_value")
    assert isinstance(default, str)
    return default


# -- the server ---------------------------------------------------------------------------------------------------------------


def _bootstrap_pools() -> dict[str, tuple[str, str]]:
    """The pools of data/bootstrap the generator allocates from: name -> (resource prefix, namespace)."""
    return {
        name(pool): (str(pool.item["resources"]["data"][0]["prefix"]), str(pool.item["ip_namespace"]))
        for file in _BOOTSTRAP_POOL_FILES
        for pool in declared("CoreIPPrefixPool", BOOTSTRAP / file)
    }


class _ServerClient(FakeClient):
    """The recording client of the generator tests, plus the server's part: the next free /31 (/127) of a pool.

    It also remembers what a test query needs to resolve later: the address families by id, the prefixes allocated.
    """

    def __init__(self) -> None:
        """Pools from data/bootstrap; allocations are idempotent per identifier, as on the server."""
        super().__init__(pools=False)
        self.resources = _bootstrap_pools()
        self.pools = {pool: SimpleNamespace(id=f"pool-{pool}", pool_name=pool) for pool in self.resources}
        self.families: dict[str, tuple[str, str]] = {}
        self.allocated: Counter[str] = Counter()
        self.prefix_data: dict[str, tuple[str, str, str, str]] = {}

    async def filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        """As FakeClient; an address family is remembered under the id it is given."""
        found = await super().filters(kind, **kwargs)
        if "afi__value" in kwargs:
            self.families[found[0].id] = (kwargs["afi__value"], kwargs["safi__value"])
        return found

    async def allocate_next_ip_prefix(self, **kwargs: Any) -> MagicMock:
        """The next free subnet of the pool's resource, the same one again for the same identifier."""
        self.allocations.append(kwargs)
        identifier = kwargs["identifier"]
        prefix = self.prefixes.get(identifier)
        if prefix is None:
            pool_name = kwargs["resource_pool"].pool_name
            resource, namespace = self.resources[pool_name]
            network = ipaddress.ip_network(resource)
            size = 2 ** (network.max_prefixlen - kwargs["prefix_length"])
            subnet = ipaddress.ip_network(f"{network[self.allocated[pool_name] * size]}/{kwargs['prefix_length']}")
            self.allocated[pool_name] += 1
            prefix = MagicMock(id=f"prefix-{identifier}")
            prefix.prefix = MagicMock(value=str(subnet))
            prefix.ip_namespace = SimpleNamespace(id=f"ns-{namespace}", name=namespace)
            self.prefixes[identifier] = prefix
            data = kwargs["data"]
            self.prefix_data[prefix.id] = (str(subnet), data["description"], data["role"], namespace)
        self.writes.append(("allocate", prefix.id, kwargs))
        return prefix


class FakeStack:
    """What the stack holds after the generator ran over the 30_all circuits, and its answers to the test_62 queries."""

    def __init__(self) -> None:
        """Empty; ``build`` runs the generator."""
        self.sessions: dict[str, dict[str, Any]] = {}
        self.prefixes: dict[str, dict[str, Any]] = {}
        self.group_members: list[dict[str, Any]] = []
        self.roles: dict[str, int] = {}
        self.pool_resources: dict[str, list[str]] = {}
        self.inventory: dict[str, Any] = {}

    @classmethod
    async def build(cls) -> FakeStack:
        """Run add_virtual_circuit on every virtual circuit of the data, then read back what it wrote."""
        stack = cls()
        client = _ServerClient()
        devices = _Devices()
        circuits = [e["node"] for e in inventory_payload()["TopologyVirtualCircuit"]["edges"]]
        for circuit in circuits:
            await session_generator(client).generate(devices.circuit(circuit))
        stack._read_back(client, devices)
        stack.pool_resources = {pool: [resource] for pool, (resource, _) in client.resources.items()}
        # The group of add_virtual_circuit holds the kinds it plans for, which the generator itself names.
        stack.group_members = [
            {"id": node["id"], "__typename": node["__typename"], "display_label": node["name"]["value"]}
            for node in (e["node"] for e in stack.inventory["TopologyVirtualCircuit"]["edges"])
            if node["__typename"] in VirtualCircuitSessionGenerator._PLANNERS
        ]
        return stack

    def _read_back(self, client: _ServerClient, devices: _Devices) -> None:
        """Resolve what the generator saved into the nodes the test_62 queries return."""
        addresses = {
            node.id: (data["address"], data["ip_namespace"].name) for data, node in client.created_of("IpamIPAddress")
        }
        keys = {**devices.keys, **{node.id: data["name"] for data, node in client.created_of("RoutingPassword")}}
        nodes: dict[str, dict[str, Any]] = {}
        assigned: dict[tuple[str, str], str] = {}
        for interface_id, template in devices.interfaces.items():
            state = client.interfaces.get(interface_id)
            if state is None:  # not written by the generator: a loopback of the device template
                nodes[interface_id] = _interface(
                    template.device,
                    template.name,
                    address=template.address,
                    namespace="default",
                    description="VTEP",
                    status="active",
                    role=template.role,
                )
                continue
            address, namespace = addresses.get(state.ip_address.id, (None, None))
            if address:
                assigned[(template.device, template.name)] = address
            nodes[interface_id] = _interface(
                template.device,
                template.name,
                address=address,
                namespace=namespace,
                description=state.description.value,
                status=state.status.value,
                mtu=None if template.parent else state.mtu.value,
                role=template.role if template.parent else None,
                parent=template.parent,
            )
        for data, _ in client.created_of("ManagedBGPPeering"):
            self.sessions[data["name"]] = _session(
                data["name"],
                role=data["peering_role"],
                session_type=data["session_type"],
                status=data["status"],
                ttl=data["ttl"],
                bfd=data["bfd_enabled"],
                extended=data["send_extended_community"],
                maximum_routes=data.get("maximum_routes"),
                key=keys[data["password"]["id"]] if "password" in data else None,
                families=[client.families[family["id"]] for family in data["address_families"]],
                processes=[devices.processes[process["id"]] for process in data["bgp_processes"]],
                interfaces=[nodes[interface["id"]] for interface in data["capabilities"]],
            )
        self.roles = dict(Counter(session["peering_role"]["value"] for session in self.sessions.values()))
        self.prefixes = {
            description: _prefix(prefix, description, role, namespace)
            for prefix, description, role, namespace in client.prefix_data.values()
        }
        self.inventory = inventory_payload(addresses=assigned)

    # -- the client ---------------------------------------------------------------------------------------------------

    async def execute_graphql(
        self, query: str, variables: dict[str, Any] | None = None, branch_name: str | None = None
    ) -> dict[str, Any]:
        """Answer the queries of the test_62 session tests from the generated state; any other query is a test bug."""
        variables = variables or {}
        if query == query_files.QUERY_GET_INTERCONNECT_INVENTORY:
            return self.inventory
        if query == interconnect_queries.SESSIONS_QUERY:
            nodes = [self.sessions[n] for n in variables["names"] if n in self.sessions]
            return {"ManagedBGPPeering": {"edges": [{"node": node} for node in nodes]}}
        if "peering_role__value" in query:
            return {"ManagedBGPPeering": {"count": self.roles.get(variables["role"], 0)}}
        if query == interconnect_queries.CIRCUIT_PREFIXES_QUERY:
            nodes = [self.prefixes[d] for d in variables["descriptions"] if d in self.prefixes]
            return {"IpamPrefix": {"edges": [{"node": node} for node in nodes]}}
        if query == interconnect_queries.EDGE_PAIR_POOL_QUERY:
            resources = [
                {"node": {"prefix": _value(prefix)}} for prefix in self.pool_resources.get(variables["name"], [])
            ]
            node = {"name": _value(variables["name"]), "resources": {"edges": resources}}
            return {"CoreIPPrefixPool": {"edges": [{"node": node}] if resources else []}}
        if query == interconnect_queries.GROUP_MEMBERS_QUERY:
            members = [{"node": member} for member in self.group_members]
            return {"CoreGroup": {"edges": [{"node": {"members": {"edges": members}}}]}}
        raise AssertionError(f"unexpected query {query[:80]!r}")


@pytest.fixture(autouse=True)
def _no_propagation_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test methods wait DATA_PROPAGATION_DELAY for a stack to settle; the fake is settled."""
    monkeypatch.setattr(circuit_helpers, "DATA_PROPAGATION_DELAY", 0)


@pytest.fixture
async def stack() -> FakeStack:
    """The stack after add_virtual_circuit ran over every virtual circuit of the 30_all data."""
    return await FakeStack.build()


def _client(stack: FakeStack) -> MagicMock:
    """A client whose ``execute_graphql`` is answered by the fake stack."""
    client = MagicMock()
    client.execute_graphql = AsyncMock(side_effect=stack.execute_graphql)
    return client


def _tests() -> session_steps.TestAllDemoInterconnects:
    """The real test_62c class, whose test methods run against the fake stack."""
    return session_steps.TestAllDemoInterconnects()


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
        (lambda s: _addresses(s, "ICX-DC10-EQXFR2-underlay")[0].update(mtu=_value(1500)), "MTU 1500, expected 9214"),
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
    """Each single deviation from the plan (ttl, type, key, MTU 1500 vs 9214, missing) fails naming its field."""
    mutate(stack)
    with pytest.raises(AssertionError, match=message.replace("(", r"\(").replace(")", r"\)")):
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
        (lambda s: s.pool_resources.update({"Handoff-PROD-IPv4": ["10.0.0.0/20"]}), "is not a /31 from"),
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
    """Each single deviation from the plan (ttl, route limit, missing, pool) fails naming its field."""
    mutate(stack)
    with pytest.raises(AssertionError, match=message.replace("(", r"\(").replace(")", r"\)")):
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
    await interconnects.TestAllDemoInterconnects().test_02a_verify_virtual_circuits_group(_client(stack), "main")
    assert len(stack.group_members) == len(ALL_DEMO_DCI_CIRCUITS) + sum(
        1 for e in ALL_DEMO_VIRTUAL_CIRCUITS.values() if e["kind"] == HANDOFF_KIND
    )


async def test_a_tunnel_in_the_group_or_a_circuit_missing_from_it_fails(stack: FakeStack) -> None:
    """A tunnel in the group would raise inside the generator, and a session circuit left out would never run."""
    nodes = [e["node"] for e in stack.inventory["TopologyVirtualCircuit"]["edges"]]
    tunnel = next(n for n in nodes if n["__typename"] == TUNNEL_KIND)
    stack.group_members.append({"id": tunnel["id"], "__typename": TUNNEL_KIND, "display_label": "tunnel"})
    with pytest.raises(AssertionError, match="a tunnel circuit is in the group"):
        await interconnects.TestAllDemoInterconnects().test_02a_verify_virtual_circuits_group(_client(stack), "main")
    stack.group_members = stack.group_members[1:-1]
    with pytest.raises(AssertionError, match="not in the group"):
        await interconnects.TestAllDemoInterconnects().test_02a_verify_virtual_circuits_group(_client(stack), "main")


# -- Drift guard: the fake follows the generator --------------------------------------------------------------------------------


def _drift_planner(monkeypatch: pytest.MonkeyPatch, kind: str, rewrite: Callable[[SessionPlan], SessionPlan]) -> None:
    """Make the generator's planner for ``kind`` decide differently: ``rewrite`` is applied to the plan it returns."""
    planner = VirtualCircuitSessionGenerator._PLANNERS[kind]

    def drifted(
        gen: VirtualCircuitSessionGenerator, circuit: dict[str, Any], name: str, ends: list[SessionEnd]
    ) -> SessionPlan:
        return rewrite(planner(gen, circuit, name, ends))

    monkeypatch.setitem(VirtualCircuitSessionGenerator._PLANNERS, kind, drifted)


def _peerings_with(**changes: Any) -> Callable[[SessionPlan], SessionPlan]:
    """A plan rewrite that sets ``changes`` (ttl, ...) on every peering of the plan."""
    return lambda plan: replace(plan, peerings=tuple(replace(peering, **changes) for peering in plan.peerings))


def _ends_swapped(plan: SessionPlan) -> SessionPlan:
    """A plan rewrite that gives the lower address to the other end."""
    return replace(plan, first=plan.second, second=plan.first)


def _drift_constant(name: str, value: Any) -> Callable[[pytest.MonkeyPatch], None]:
    """A drift that changes a planner constant of generators/topology/virtual_circuit.py."""
    return lambda monkeypatch: monkeypatch.setattr(virtual_circuit, name, value)


def _drift_plan(kind: str, rewrite: Callable[[SessionPlan], SessionPlan]) -> Callable[[pytest.MonkeyPatch], None]:
    """A drift that changes what the planner of ``kind`` returns."""
    return lambda monkeypatch: _drift_planner(monkeypatch, kind, rewrite)


@pytest.mark.parametrize(
    ("drift", "method", "message"),
    [
        pytest.param(
            _drift_constant("_DCI_UNDERLAY_MAXIMUM_ROUTES", 32),
            "test_06_verify_dci_sessions",
            "maximum_routes 32, expected 16",
            id="dci-maximum-routes",
        ),
        pytest.param(
            _drift_constant("_DCI_MTU", 1500), "test_06_verify_dci_sessions", "MTU 1500, expected 9214", id="dci-mtu"
        ),
        pytest.param(
            _drift_plan(virtual_circuit.INTERCONNECT_CIRCUIT, _peerings_with(ttl=64)),
            "test_06_verify_dci_sessions",
            "ttl 64",
            id="dci-ttl",
        ),
        pytest.param(
            _drift_plan(virtual_circuit.INTERCONNECT_CIRCUIT, _ends_swapped),
            "test_06_verify_dci_sessions",
            "the facility end takes the lower address",
            id="dci-lower-address",
        ),
        pytest.param(
            _drift_plan(virtual_circuit.HANDOFF_CIRCUIT, _peerings_with(ttl=255)),
            "test_06b_verify_handoff_sessions",
            "ttl 255",
            id="handoff-ttl",
        ),
        pytest.param(
            _drift_plan(virtual_circuit.HANDOFF_CIRCUIT, _peerings_with(maximum_routes=7)),
            "test_06b_verify_handoff_sessions",
            "maximum_routes 7",
            id="handoff-maximum-routes",
        ),
        pytest.param(
            _drift_plan(virtual_circuit.HANDOFF_CIRCUIT, _ends_swapped),
            "test_06b_verify_handoff_sessions",
            "not the lower address of the /31",
            id="handoff-lower-address",
        ),
        pytest.param(
            lambda monkeypatch: monkeypatch.setattr(
                virtual_circuit, "handoff_pool_name", lambda namespace: "Handoff-NON-PROD-IPv4"
            ),
            "test_06b_verify_handoff_sessions",
            "is not a /31 from",
            id="handoff-pool",
        ),
    ],
)
async def test_a_changed_planner_decision_reaches_the_test_62_methods(
    monkeypatch: pytest.MonkeyPatch, drift: Callable[[pytest.MonkeyPatch], None], method: str, message: str
) -> None:
    """The fake is the generator's output, not a copy of its rules: when a planner decides differently, test_62 fails.

    Each case first passes on the unchanged generator (the tests above), then rebuilds the stack with one planner
    constant or decision changed and expects the real test method to name the field.
    """
    drift(monkeypatch)
    stack = await FakeStack.build()
    with pytest.raises(AssertionError, match=message):
        await getattr(_tests(), method)(_client(stack), "main")
