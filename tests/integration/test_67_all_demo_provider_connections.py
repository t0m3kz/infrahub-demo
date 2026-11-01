"""Integration test — the provider connections of 30_all, what add_provider_connection builds and how it converges.

A TopologyProviderConnection (data/demos/30_all/08_interconnects/05_provider_connections) is one object that says "this
facility of ours is connected to that provider facility, over carriers or the internet". The generator turns it into one
TopologySessionCircuit per link of its provider's flavour (utils/provider_services.py), named `<connection>-L<n>`:
`handoff` (the `<port>.<vlan>` sub-interfaces on the two ends of the carrier) for a dedicated connection, `vpn` (a
`Tunnel<n>` pair) for an internet_vpn one; add_virtual_circuit then addresses the link and builds its eBGP session.

  * test_01 asserts the connections as the data states them (all_demo_provider_connection_constants): the link count the
    flavour gives, each circuit's ends and parent interfaces, locations, namespace, key, provider-assigned range, derived
    owner (a circuit has no owner of its own) and derived carrier, the carriers listing their connections back, the
    groups, and that no other handoff/vpn circuit exists.
  * test_02 asserts the session of every link against its flavour (routed_link_rules): the /31 or /30 from our pool or
    the range the provider assigned, which end takes the lower address, the TTL, the key, the party's ASN.
  * test_03 reruns the generator over every connection: not one object of the connection, its circuits, interfaces,
    addresses, prefixes and sessions may change (same ids).
  * test_04 adds a DISPOSABLE connection (a second VLAN on the Megaport cross-connect), then changes its path_type to
    internet_vpn with an internet carrier in one save: the old handoff end interfaces are cleaned by the next run, the
    link is a vpn on `Tunnel<n>` ends, and the connection holds exactly one circuit.
  * test_05 deletes the disposable connection: its session circuits go with it (cascade), its carriers survive, and every
    other connection and circuit is as test_01 found it.

It runs AFTER test_66 because it adds and deletes objects on the branch the other modules read. What a delete leaves
behind (the link interfaces, the peering and the addresses add_virtual_circuit built) is the generator's documented
leftover: the test removes the interfaces and the peering itself at the end so the branch is as it found it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from tasks import ALL_DEMO_BRANCH
from utils.data_cleaning import clean_data
from utils.limits import GROUP_PROVIDER_CONNECTIONS, GROUP_VIRTUAL_CIRCUITS

from .all_demo_circuit_constants import ALL_DEMO_VIRTUAL_CIRCUITS, HANDOFF_TYPE
from .all_demo_provider_connection_constants import (
    ADDRESS_PROVIDER,
    ALL_DEMO_CONNECTION_CIRCUITS,
    ALL_DEMO_CONNECTION_LINKS,
    ALL_DEMO_CONNECTIONS,
)
from .circuit_helpers import fetch_interconnect_inventory
from .conftest import TestInfrahubDockerWithClient
from .interconnect_queries import (
    CIRCUIT_PREFIXES_QUERY,
    EDGE_PAIR_POOL_QUERY,
    GROUP_MEMBERS_QUERY,
    SESSIONS_QUERY,
)
from .interconnect_rules import _prefixes_by_description, _sessions_by_name
from .provider_connection_helpers import (
    carrier_membership_errors,
    connection_errors,
    fetch_connections,
    fetch_interface_ids,
    link_errors,
    unowned_circuit_errors,
)
from .routed_link_rules import (
    DEFAULT_LINK_FLAVOUR,
    LABEL_HANDOFF,
    LABEL_VPN,
    LinkFlavour,
    routed_session_errors,
)
from .snapshot_helpers import diff_snapshots, snapshot_topology
from .wait_helpers import wait_for_condition
from .workflow_helpers import run_generator, verify_no_failed_tasks, wait_for_tasks_completion

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

SCENARIO_NAME = "Scenario 30_all: Provider Connections"

GENERATOR = "add_provider_connection"

# add_provider_connection writes the circuits, add_virtual_circuit (fired by their created rule) the addresses and the
# sessions: two task hops queued behind whatever else the workers run, as in test_66.
MAX_ATTEMPTS = 30
POLL_INTERVAL = 10

# The kinds a connection run reads or writes (snapshot_helpers.TOPOLOGY_SNAPSHOT_KINDS): a rerun must not move any.
CONNECTION_SNAPSHOT_KINDS = (
    "TopologyProviderConnection",
    "TopologySessionCircuit",
    "TopologyCarrierCircuit",
    "DcimInterface",
    "IpamIPAddress",
    "IpamPrefix",
    "ManagedBGPPeering",
    "ManagedBGP",
    "ManagedVRF",
    "RoutingPassword",
)

# The disposable connection: a second VLAN on the Megaport cross-connect of PC-PA4-MEGAPORT-PROD (so it needs no free
# port), then an internet VPN over the PA4 Lumen transit. Both legs end on devices of the real Megaport connection,
# whose own objects must not move.
DISPOSABLE = "PC-TEST67-MEGAPORT-PROD"
DISPOSABLE_VLAN = 1399
DISPOSABLE_CIRCUIT = f"{DISPOSABLE}-L1"
DISPOSABLE_NEAR = ("PA4", "eg-pa401", "Ethernet1/1")
DISPOSABLE_FAR = ("MCR-PAR1", "EXT-MEGAPORT-MCR1", "et-0/0/0")
DISPOSABLE_ASN = 133937
DISPOSABLE_NAMESPACE = "PROD"
DISPOSABLE_KEY = "eqx-pa4-megaport-key"
DISPOSABLE_AS = "AS133937"
DISPOSABLE_DEDICATED_CARRIER = "XC-EQXPA4-MEGAPORT"
DISPOSABLE_VPN_CARRIER = "INET-LUMEN-EQXPA4"
DISPOSABLE_VPN_PORT = "Ethernet1/14"
DISPOSABLE_SUB_INTERFACES = {
    DISPOSABLE_NEAR[1]: f"{DISPOSABLE_NEAR[2]}.{DISPOSABLE_VLAN}",
    DISPOSABLE_FAR[1]: f"{DISPOSABLE_FAR[2]}.{DISPOSABLE_VLAN}",
}


def _flavour(connection: str) -> tuple[LinkFlavour, str]:
    """The restated flavour of a connection (all_demo_provider_connection_constants) and the label of its description."""
    expected = ALL_DEMO_CONNECTIONS[connection]
    flavour = LinkFlavour(
        expected["prefix_length"],
        expected["lower_end"],
        expected["ttl"],
        expected["bfd"],
        expected["address_source"] == ADDRESS_PROVIDER,
    )
    return flavour, LABEL_HANDOFF if expected["path_type"] == "dedicated" else LABEL_VPN


async def _poll(description: str, judge: Callable[[], Awaitable[list[str]]], max_attempts: int = MAX_ATTEMPTS) -> None:
    """Poll until ``judge`` finds nothing wrong; a timeout fails with what is still wrong."""
    last: list[list[str]] = []

    async def _check() -> tuple[bool, None]:
        errors = await judge()
        last[:] = [errors]
        if errors:
            logging.info("%s: not yet: %s", description, "; ".join(errors))
        return not errors, None

    try:
        await wait_for_condition(
            _check, max_attempts=max_attempts, poll_interval=POLL_INTERVAL, description=description
        )
    except TimeoutError as exc:
        raise AssertionError(f"{exc}\n  - " + "\n  - ".join(last[0] if last else [])) from exc


async def _id(client: InfrahubClient, branch: str, kind: str, **filters: Any) -> str:
    """The id of the one ``kind`` node matching ``filters``."""
    return (await client.get(kind=kind, branch=branch, **filters)).id


async def _converge(client: InfrahubClient, branch: str, connection_id: str) -> None:
    """Let the triggered runs drain, run add_provider_connection once more on the connection, let its fan-out drain."""
    await wait_for_tasks_completion(client, branch)
    outcome = await run_generator(client=client, generator_name=GENERATOR, node_ids=[connection_id], branch=branch)
    assert outcome["success"], f"{GENERATOR} failed: {outcome}"
    await wait_for_tasks_completion(client, branch)


class TestAllDemoProviderConnections(TestInfrahubDockerWithClient):
    """Every connection of 30_all yields its planned circuits and sessions, converges, and cleans up after itself."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    # ------------------------------------------------------------------
    # What the generator built
    # ------------------------------------------------------------------

    @pytest.mark.order(430)
    @pytest.mark.dependency(
        scope="session", name="all_demo_provider_connections", depends=["all_demo_mlag_remove_readd"]
    )
    @pytest.mark.asyncio
    async def test_01_connections_and_their_circuits(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Each connection holds the circuits its flavour plans, each with the ends, interfaces, namespace, key, range,
        derived owner and carrier the table says; the carriers list their connections back."""
        logging.info("=== %s - Step 1: Connections and circuits ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        await wait_for_tasks_completion(client, branch)

        connections = await fetch_connections(client, branch)
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        circuits = {circuit["name"]: circuit for circuit in inventory["virtual"]}

        errors: list[str] = []
        missing = sorted(set(ALL_DEMO_CONNECTIONS) - set(connections))
        unexpected = sorted(set(connections) - set(ALL_DEMO_CONNECTIONS))
        if missing or unexpected:
            errors.append(f"connections {sorted(connections)}: missing {missing}, unexpected {unexpected}")
        for name, expected in sorted(ALL_DEMO_CONNECTIONS.items()):
            errors += connection_errors(connections.get(name), name, expected)
            for link in expected["links"]:
                errors += link_errors(circuits.get(link["name"]), expected, link, name)
        errors += unowned_circuit_errors(
            inventory["virtual"],
            set(ALL_DEMO_CONNECTION_LINKS),
            {n for n, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["circuit_type"] == HANDOFF_TYPE}
            - set(ALL_DEMO_CONNECTION_LINKS),
        )
        errors += carrier_membership_errors(inventory["physical"], ALL_DEMO_CONNECTIONS)

        for group, expected_ids in (
            (GROUP_PROVIDER_CONNECTIONS, {c["id"] for c in connections.values()}),
            (GROUP_VIRTUAL_CIRCUITS, {circuits[n]["id"] for n in ALL_DEMO_CONNECTION_LINKS if n in circuits}),
        ):
            members = clean_data(
                await client.execute_graphql(query=GROUP_MEMBERS_QUERY, variables={"name": group}, branch_name=branch)
            )["CoreGroup"]
            held = {str(member["id"]) for member in (members[0]["members"] if members else [])}
            if not expected_ids <= held:
                errors.append(f"group {group}: lacks {sorted(expected_ids - held)}")
        assert ALL_DEMO_CONNECTION_CIRCUITS <= set(ALL_DEMO_CONNECTION_LINKS)

        assert not errors, f"30_all provider connections are wrong on branch '{branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Connections verified: %d, session circuits %d", len(connections), len(ALL_DEMO_CONNECTION_LINKS))

    @pytest.mark.order(431)
    @pytest.mark.dependency(
        scope="session", name="all_demo_provider_sessions", depends=["all_demo_provider_connections"]
    )
    @pytest.mark.asyncio
    async def test_02_sessions_follow_the_flavour(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """One `<link>-bgp` per link, addressed as its flavour says: a /31 or /30 from `Handoff-<namespace>-IPv4` (our
        pool), or the two hosts of the range the provider assigned (no prefix of ours is allocated); the flavour's end
        takes the lower address; TTL 1, or 2 (multihop) for Zscaler; the party in the declared ASN."""
        logging.info("=== %s - Step 2: Sessions ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        circuits = {circuit["name"]: circuit for circuit in inventory["virtual"]}
        names = sorted(ALL_DEMO_CONNECTION_LINKS)
        sessions = _sessions_by_name(
            await client.execute_graphql(
                query=SESSIONS_QUERY, variables={"names": [f"{n}-bgp" for n in names]}, branch_name=branch
            )
        )
        prefixes = _prefixes_by_description(
            await client.execute_graphql(
                query=CIRCUIT_PREFIXES_QUERY,
                variables={"descriptions": [f"{label} {n}" for n in names for label in (LABEL_HANDOFF, LABEL_VPN)]},
                branch_name=branch,
            )
        )
        pools = await self._pools(client, branch, {c["ip_namespace"] for c in circuits.values() if c["ip_namespace"]})

        errors: list[str] = []
        for name in names:
            connection, link = ALL_DEMO_CONNECTION_LINKS[name]
            circuit = circuits.get(name)
            if circuit is None:
                errors.append(f"{name}: no session circuit")
                continue
            flavour, label = _flavour(connection)
            errors += routed_session_errors(
                name,
                circuit,
                sessions.get(f"{name}-bgp"),
                label=label,
                flavour=flavour,
                pools=pools.get(circuit["ip_namespace"] or "", []),
                prefixes=prefixes.get(f"{label} {name}"),
                party=link["party"],
            )
            if flavour.provider_assigned and any(prefixes.get(f"{lb} {name}") for lb in (LABEL_HANDOFF, LABEL_VPN)):
                errors.append(f"{name}: the provider assigned the range, yet a prefix of ours was allocated for it")
        per_connection = {
            connection: [s for s in sessions if s.startswith(f"{connection}-L")] for connection in ALL_DEMO_CONNECTIONS
        }
        errors += [
            f"{connection}: {len(found)} session(s) {sorted(found)}, expected {len(ALL_DEMO_CONNECTIONS[connection]['links'])}"
            for connection, found in sorted(per_connection.items())
            if len(found) != len(ALL_DEMO_CONNECTIONS[connection]["links"])
        ]

        assert not errors, f"30_all provider connection sessions are wrong on branch '{branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Connection sessions verified: %d", len(names))

    @staticmethod
    async def _pools(client: InfrahubClient, branch: str, namespaces: set[str]) -> dict[str, list[Any]]:
        """Namespace -> the networks of its `Handoff-<namespace>-IPv4` pool (empty when the pool is missing)."""
        import ipaddress

        pools: dict[str, list[Any]] = {}
        for namespace in sorted(namespaces):
            result = await client.execute_graphql(
                query=EDGE_PAIR_POOL_QUERY, variables={"name": f"Handoff-{namespace}-IPv4"}, branch_name=branch
            )
            pools[namespace] = [
                ipaddress.IPv4Network(resource["node"]["prefix"]["value"])
                for edge in result["CoreIPPrefixPool"]["edges"]
                for resource in edge["node"]["resources"]["edges"]
                if resource["node"].get("prefix")
            ]
        return pools

    # ------------------------------------------------------------------
    # Convergence
    # ------------------------------------------------------------------

    @pytest.mark.order(432)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_rerun", depends=["all_demo_provider_sessions"])
    @pytest.mark.asyncio
    async def test_03_rerun_is_a_no_op(self, async_client_main: InfrahubClient, scenario_branch: str) -> None:
        """Running add_provider_connection again over every connection changes not one object it reads or writes: the
        connections, circuits, interfaces, addresses, prefixes, sessions and VRF instances keep their ids and values
        (a rerun that renumbered a VLAN, re-created a circuit or an interface, or re-allocated a link is a diff)."""
        logging.info("=== %s - Step 3: Rerun ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        await wait_for_tasks_completion(client, branch)
        before, labels = await snapshot_topology(client, branch, CONNECTION_SNAPSHOT_KINDS)
        assert len(before["TopologyProviderConnection"]) == len(ALL_DEMO_CONNECTIONS)
        ids = sorted(before["TopologyProviderConnection"])

        outcome = await run_generator(client=client, generator_name=GENERATOR, node_ids=ids, branch=branch)
        assert outcome["success"], f"{GENERATOR} failed: {outcome}"
        await wait_for_tasks_completion(client, branch)

        after, labels_after = await snapshot_topology(client, branch, CONNECTION_SNAPSHOT_KINDS)
        changes = diff_snapshots(before, after, {**labels_after, **labels})
        assert not changes, f"a rerun of {GENERATOR} changed the branch:\n  - " + "\n  - ".join(changes[:50])
        await verify_no_failed_tasks(client=client, branch=branch)

    # ------------------------------------------------------------------
    # A disposable connection: create, change the path type, delete
    # ------------------------------------------------------------------

    @pytest.mark.order(433)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_disposable", depends=["all_demo_provider_rerun"])
    @pytest.mark.asyncio
    async def test_04_changing_the_path_type_cleans_the_old_outputs(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """A new dedicated connection gets its handoff on `<port>.1399` ends and a session; changed to internet_vpn
        (over the PA4 Lumen transit) in one save, the next run replaces the handoff by a vpn on `Tunnel<n>` ends,
        deletes the sub-interfaces it no longer reaches and keeps exactly one circuit."""
        logging.info("=== %s - Step 4: Disposable connection, path type change ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        await wait_for_tasks_completion(client, branch)
        sub_devices = list(DISPOSABLE_SUB_INTERFACES)
        sub_names = sorted(DISPOSABLE_SUB_INTERFACES.values())
        assert not await fetch_interface_ids(client, branch, sub_devices, sub_names), "VLAN 1399 is already in use"

        data: dict[str, Any] = {
            "name": DISPOSABLE,
            "description": "test_67 disposable connection: deleted by the test",
            "path_type": "dedicated",
            "service": "private",
            "status": "active",
            "bandwidth": 1000,
            "vlan_id": DISPOSABLE_VLAN,
            "near": {"id": await _id(client, branch, "TopologyColocationZone", name__value=DISPOSABLE_NEAR[0])},
            "far": {"id": await _id(client, branch, "TopologyColocationZone", name__value=DISPOSABLE_FAR[0])},
            "ip_namespace": {"id": await _id(client, branch, "IpamNamespace", name__value=DISPOSABLE_NAMESPACE)},
            "provider_as": {"id": await _id(client, branch, "RoutingAutonomousSystem", asn__value=DISPOSABLE_ASN)},
            "password": {"id": await _id(client, branch, "RoutingPassword", name__value=DISPOSABLE_KEY)},
            "carrier_circuits": [
                {
                    "id": await _id(
                        client, branch, "TopologyCarrierCircuit", circuit_id__value=DISPOSABLE_DEDICATED_CARRIER
                    )
                }
            ],
            "member_of_groups": [
                {"id": await _id(client, branch, "CoreStandardGroup", name__value=GROUP_PROVIDER_CONNECTIONS)}
            ],
        }
        connection = await client.create(kind="TopologyProviderConnection", data=data, branch=branch)
        await connection.save()
        connection_id = connection.id
        logging.info("Created %s (%s)", DISPOSABLE, connection_id)

        await _converge(client, branch, connection_id)
        await _poll(f"{DISPOSABLE} as a handoff", lambda: self._dedicated_errors(client, branch))

        # the path type changes together with the carrier it rides: a VPN rides internet carriers only
        connection = await client.get(
            kind="TopologyProviderConnection", branch=branch, id=connection_id, include=["carrier_circuits"]
        )
        getattr(connection, "path_type").value = "internet_vpn"  # noqa: B009 - SDK attributes are dynamic
        getattr(connection, "vlan_id").value = None  # noqa: B009
        carriers = getattr(connection, "carrier_circuits")  # noqa: B009 - SDK relationships are dynamic
        for peer in list(carriers.peers):
            carriers.remove(peer.id)
        carriers.add(await _id(client, branch, "TopologyCarrierCircuit", circuit_id__value=DISPOSABLE_VPN_CARRIER))
        await connection.save()
        await _converge(client, branch, connection_id)
        await _poll(f"{DISPOSABLE} as a vpn, old outputs cleaned", lambda: self._vpn_errors(client, branch))
        await verify_no_failed_tasks(client=client, branch=branch)

    @pytest.mark.order(434)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_deleted", depends=["all_demo_provider_disposable"])
    @pytest.mark.asyncio
    async def test_05_deleting_a_connection_cascades_its_circuits_and_keeps_the_carriers(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Deleting the connection deletes its session circuit (cascade) and nothing a carrier or another connection
        owns; the carriers it listed are still there with the ends they had, the connections of test_01 still hold
        their circuits."""
        logging.info("=== %s - Step 5: Disposable connection, delete ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        before = await fetch_interconnect_inventory(client=client, branch=branch)
        carriers_before = {c["circuit_id"]: (c["ends"], c["status"]) for c in before["physical"]}
        assert DISPOSABLE_CIRCUIT in {c["name"] for c in before["virtual"]}, "no disposable circuit to cascade"

        try:
            connection = await client.get(kind="TopologyProviderConnection", branch=branch, name__value=DISPOSABLE)
            await connection.delete()
            await wait_for_tasks_completion(client, branch)

            async def _gone() -> list[str]:
                inventory = await fetch_interconnect_inventory(client=client, branch=branch)
                names = {c["name"] for c in inventory["virtual"]}
                return [f"{DISPOSABLE_CIRCUIT} still exists"] if DISPOSABLE_CIRCUIT in names else []

            await _poll(f"{DISPOSABLE_CIRCUIT} cascaded", _gone)
            inventory = await fetch_interconnect_inventory(client=client, branch=branch)
            carriers_after = {c["circuit_id"]: (c["ends"], c["status"]) for c in inventory["physical"]}
            assert carriers_after == carriers_before, "a carrier circuit changed with the connection that listed it"
            assert not any(DISPOSABLE in c["connections"] for c in inventory["physical"])
            connections = await fetch_connections(client, branch)
            assert DISPOSABLE not in connections
            errors: list[str] = []
            circuits = {c["name"]: c for c in inventory["virtual"]}
            for name, expected in sorted(ALL_DEMO_CONNECTIONS.items()):
                errors += connection_errors(connections.get(name), name, expected)
                for link in expected["links"]:
                    errors += link_errors(circuits.get(link["name"]), expected, link, name)
            assert not errors, "deleting the disposable connection moved the others:\n  - " + "\n  - ".join(errors)
        finally:
            await self._remove_leftovers(client, branch)
            await wait_for_tasks_completion(client, branch)
        await verify_no_failed_tasks(client=client, branch=branch)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    # ------------------------------------------------------------------
    # Judges of the disposable connection
    # ------------------------------------------------------------------

    @staticmethod
    async def _disposable_circuit(client: InfrahubClient, branch: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The disposable connection's circuits (inventory entries) and its row."""
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        circuits = [c for c in inventory["virtual"] if c["connection"] == DISPOSABLE]
        row = (await fetch_connections(client, branch)).get(DISPOSABLE, {})
        return circuits, row

    async def _dedicated_errors(self, client: InfrahubClient, branch: str) -> list[str]:
        """The disposable connection as the generator builds a dedicated one: one handoff on `<port>.1399` ends of the
        two carrier ports, and the session of its flavour (a /31 of our pool, our end the lower address)."""
        circuits, row = await self._disposable_circuit(client, branch)
        if len(circuits) != 1:
            return [f"{len(circuits)} circuit(s) of {DISPOSABLE}, expected 1"]
        (circuit,) = circuits
        errors: list[str] = []
        ends = sorted(
            [
                (DISPOSABLE_NEAR[1], f"{DISPOSABLE_NEAR[2]}.{DISPOSABLE_VLAN}"),
                (DISPOSABLE_FAR[1], f"{DISPOSABLE_FAR[2]}.{DISPOSABLE_VLAN}"),
            ]
        )
        if circuit["name"] != DISPOSABLE_CIRCUIT or circuit["circuit_type"] != "handoff":
            errors.append(
                f"circuit {circuit['name']} [{circuit['circuit_type']}], expected {DISPOSABLE_CIRCUIT} [handoff]"
            )
        if circuit["ends"] != ends:
            errors.append(f"ends {circuit['ends']}, expected {ends}")
        if row.get("vlan_id") != DISPOSABLE_VLAN or row.get("sessions") != [DISPOSABLE_CIRCUIT]:
            errors.append(f"connection holds vlan {row.get('vlan_id')} and {row.get('sessions')}")
        if errors:
            return errors
        sessions = _sessions_by_name(
            await client.execute_graphql(
                query=SESSIONS_QUERY, variables={"names": [f"{DISPOSABLE_CIRCUIT}-bgp"]}, branch_name=branch
            )
        )
        prefixes = _prefixes_by_description(
            await client.execute_graphql(
                query=CIRCUIT_PREFIXES_QUERY,
                variables={"descriptions": [f"{LABEL_HANDOFF} {DISPOSABLE_CIRCUIT}"]},
                branch_name=branch,
            )
        )
        pools = await self._pools(client, branch, {DISPOSABLE_NAMESPACE})
        return routed_session_errors(
            DISPOSABLE_CIRCUIT,
            circuit,
            sessions.get(f"{DISPOSABLE_CIRCUIT}-bgp"),
            label=LABEL_HANDOFF,
            flavour=DEFAULT_LINK_FLAVOUR,
            pools=pools[DISPOSABLE_NAMESPACE],
            prefixes=prefixes.get(f"{LABEL_HANDOFF} {DISPOSABLE_CIRCUIT}"),
            party=(DISPOSABLE_FAR[1], DISPOSABLE_ASN),
        )

    async def _vpn_errors(self, client: InfrahubClient, branch: str) -> list[str]:
        """The disposable connection after the change: one vpn circuit (the same name) on `Tunnel1` ends, the near one
        riding the internet carrier's port, the carrier listed, no VLAN, and none of the old sub-interfaces left."""
        circuits, row = await self._disposable_circuit(client, branch)
        errors: list[str] = []
        if len(circuits) != 1:
            return [f"{len(circuits)} circuit(s) of {DISPOSABLE}, expected 1: {[c['name'] for c in circuits]}"]
        (circuit,) = circuits
        if circuit["circuit_type"] != "vpn":
            errors.append(f"{circuit['name']} is a {circuit['circuit_type']}, expected vpn")
        tunnels = sorted([(DISPOSABLE_NEAR[1], "Tunnel1"), (DISPOSABLE_FAR[1], "Tunnel1")])
        if circuit["ends"] != tunnels:
            errors.append(f"ends {circuit['ends']}, expected {tunnels}")
        near = next((e for e in circuit["end_nodes"] if e["device"] == DISPOSABLE_NEAR[1]), None)
        if near is not None and near["parent_name"] != DISPOSABLE_VPN_PORT:
            errors.append(f"near tunnel rides {near['parent_name']!r}, expected {DISPOSABLE_VPN_PORT!r}")
        if row.get("path_type") != "internet_vpn" or row.get("vlan_id") is not None:
            errors.append(f"connection is {row.get('path_type')} with vlan {row.get('vlan_id')}")
        if row.get("carriers") != [DISPOSABLE_VPN_CARRIER] or row.get("sessions") != [DISPOSABLE_CIRCUIT]:
            errors.append(f"connection lists carriers {row.get('carriers')} and sessions {row.get('sessions')}")
        left = await fetch_interface_ids(
            client, branch, list(DISPOSABLE_SUB_INTERFACES), sorted(DISPOSABLE_SUB_INTERFACES.values())
        )
        if left:
            errors.append(f"old sub-interfaces still exist: {left}")
        return errors

    @staticmethod
    async def _remove_leftovers(client: InfrahubClient, branch: str) -> None:
        """Remove what a delete of the connection leaves (the generator's documented leftovers): the connection itself if
        a failed step kept it, the tunnel and sub-interfaces of its link and the peering add_virtual_circuit built."""
        leftover = await client.filters(kind="TopologyProviderConnection", branch=branch, name__value=DISPOSABLE)
        for node in leftover:
            await node.delete()
        for peering in await client.filters(
            kind="ManagedBGPPeering", branch=branch, name__value=f"{DISPOSABLE_CIRCUIT}-bgp"
        ):
            await peering.delete()
        for device, sub_interface in DISPOSABLE_SUB_INTERFACES.items():
            for interface in await client.filters(
                kind="DcimVirtualInterface",
                branch=branch,
                device__name__value=device,
                name__values=[sub_interface, "Tunnel1"],
            ):
                await interface.delete()
