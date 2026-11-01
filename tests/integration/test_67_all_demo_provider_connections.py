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
  * test_04 creates a connection from INTENT only (near, far, path_type, ip_namespace, the hub account: no carrier, no
    near_port) and judges it against the choice generators.helpers.near_port makes from the stack's own facts: the
    carrier `<name>-XC` (provisioning) between the chosen `cloud` port and the NIC `vnic-<name>`, the handoff and its
    session. test_05 reruns it (a no-op), test_06 deletes it (sessions cascade, the carrier stays).
  * test_07 requests a connection with an explicit near_port on that carrier's port: the carrier is adopted, none is
    created. test_08 requests one on a busy port: refused, nothing written. test_09 changes the adopter to an
    internet_vpn: tunnels, `carrier_circuits` cleared, no carrier created, the adopted carrier left. test_10 deletes it
    (circuits cascade, carriers stay) and restores the branch.

It runs AFTER test_66 because it adds and deletes objects on the branch the other modules read. What a delete leaves
behind (the link interfaces, the peering and the addresses add_virtual_circuit built) is the generator's documented
leftover: the test removes the interfaces and the peering itself at the end so the branch is as it found it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from infrahub_sdk import InfrahubClient

from tasks import ALL_DEMO_BRANCH
from utils.data_cleaning import clean_data
from utils.limits import GROUP_CARRIER_CIRCUITS, GROUP_PROVIDER_CONNECTIONS, GROUP_VIRTUAL_CIRCUITS

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
    carrier_state,
    connection_errors,
    created_connection_errors,
    expected_near_port,
    fetch_connections,
    fetch_interface_ids,
    fetch_role_ports,
    link_errors,
    other_carrier_errors,
    unexpected_failed_tasks,
    unowned_circuit_errors,
)
from .routed_link_rules import (
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


# The throwaway connections: all FR2 -> eu-central-1 (a cloud region: the generator creates the far NIC, so nothing
# depends on a free port of a provider device) on the Direct Connect service edge, the account of the hub. Three
# requests: CREATED asks for everything from intent only, ADOPTER names the port of the carrier CREATED left behind,
# REFUSED names a port that is busy.
NEAR_FACILITY = "FR2"
FAR_FACILITY = "eu-central-1"
FAR_KIND = "TopologyCloudRegion"
SERVICE_EDGE = "EXT-AWS-DX-FR2"
ACCOUNT = "net-hub-aws-prod"
DIRECT_CONNECT_ASN = 64512
NAMESPACE = "PROD"
CLOUD_PORTS = ("Ethernet1/3", "Ethernet1/4", "Ethernet1/5")  # the `cloud` ports of the FR2 edges (bootstrap template)
CREATED = "PC-TEST67-CREATED-PROD"
ADOPTER = "PC-TEST67-ADOPT-PROD"
REFUSED = "PC-TEST67-BUSY-PROD"
CREATED_CARRIER = f"{CREATED}-XC"
CREATED_NIC = f"vnic-{CREATED}"
# The path-type change: the VPN of the AWS gateway (its own Amazon-side AS), leaving by the port of an FR2 internet
# carrier, two provider-assigned /30 ranges that the test creates and removes.
VPN_ASN = 64513
VPN_STUB = "EXT-AWS-VPN-EUC1"
VPN_NEAR = ("eg-fr201", "Ethernet1/14")
VPN_RANGES = ("169.254.77.0/30", "169.254.77.4/30")
TUNNEL = re.compile(r"Tunnel\d+")

# What one scenario hands to the next (the tests of the class run in order, in one process).
STATE: dict[str, Any] = {}


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
    # A request with nothing but intent: create, rerun, delete, adopt, refuse, change the path type
    # ------------------------------------------------------------------

    @pytest.mark.order(433)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_created", depends=["all_demo_provider_rerun"])
    @pytest.mark.asyncio
    async def test_04_a_connection_from_intent_gets_its_port_carrier_and_nic(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """A connection with only near, far, path_type, ip_namespace (and the hub account): the generator takes the port
        generators.helpers.near_port chooses from what the stack holds (a `cloud` port of the FR2 edges, the least loaded
        device first, then the first free port by name), creates the NIC `vnic-<name>` on the service edge and a carrier
        `<name>-XC` (provisioning, virtual_connection) between them, lists it in `carrier_circuits`, and builds the
        handoff and its session on `<port>.<vlan>` ends (the VLAN is chosen and written back)."""
        logging.info("=== %s - Step 4: Connection from intent ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        await wait_for_tasks_completion(client, branch)
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        connections = await fetch_connections(client, branch)
        assert not {CREATED, ADOPTER, REFUSED} & set(connections), "a throwaway connection of an earlier run is left"
        ports = await fetch_role_ports(client, branch, NEAR_FACILITY, FAR_KIND)
        assert ports, f"no port with the cloud role on the devices of {NEAR_FACILITY}: is the edge template loaded?"
        chosen = expected_near_port(ports, inventory, connections)
        assert chosen.name in CLOUD_PORTS, f"the choice {chosen.label} is no cloud port {CLOUD_PORTS}"
        STATE["near"] = (chosen.device_name, chosen.name)
        STATE["carriers_before"] = carrier_state(inventory)
        logging.info("Expected near port: %s", chosen.label)

        STATE["created_id"] = await _request(client, branch, CREATED)
        await _converge(client, branch, STATE["created_id"])
        await _poll(f"{CREATED} from intent", lambda: self._created_errors(client, branch, CREATED, STATE["near"]))
        await verify_no_failed_tasks(client=client, branch=branch)

    @pytest.mark.order(434)
    @pytest.mark.dependency(
        scope="session", name="all_demo_provider_created_rerun", depends=["all_demo_provider_created"]
    )
    @pytest.mark.asyncio
    async def test_05_rerunning_the_created_connection_changes_nothing(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """A second run finds the connection's own carrier (it keeps its near port, it selects nothing again): no
        carrier, NIC, interface, address or session is added, moved or re-created, and the branch snapshot is equal."""
        logging.info("=== %s - Step 5: Rerun of the created connection ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        await wait_for_tasks_completion(client, branch)
        before, labels = await snapshot_topology(client, branch, CONNECTION_SNAPSHOT_KINDS)
        outcome = await run_generator(
            client=client, generator_name=GENERATOR, node_ids=[STATE["created_id"]], branch=branch
        )
        assert outcome["success"], f"{GENERATOR} failed: {outcome}"
        await wait_for_tasks_completion(client, branch)
        after, labels_after = await snapshot_topology(client, branch, CONNECTION_SNAPSHOT_KINDS)
        changes = diff_snapshots(before, after, {**labels_after, **labels})
        assert not changes, f"a rerun of {GENERATOR} over {CREATED} changed the branch:\n  - " + "\n  - ".join(
            changes[:50]
        )
        assert not await self._created_errors(client, branch, CREATED, STATE["near"])
        await verify_no_failed_tasks(client=client, branch=branch)

    @pytest.mark.order(435)
    @pytest.mark.dependency(
        scope="session", name="all_demo_provider_created_deleted", depends=["all_demo_provider_created_rerun"]
    )
    @pytest.mark.asyncio
    async def test_06_deleting_the_connection_cascades_its_sessions_and_keeps_the_carrier(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Deleting the connection deletes its session circuit (cascade); the carrier and the NIC it created are shared
        underlay and stay, with their ends and no connection listing them (the next request adopts it). The link
        interfaces and the peering are the generator's documented leftover: the test removes them, so the carrier's
        port is free of sub-interfaces for the next scenario."""
        logging.info("=== %s - Step 6: Delete the created connection ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        circuit = f"{CREATED}-L1"
        try:
            connection = await client.get(kind="TopologyProviderConnection", branch=branch, name__value=CREATED)
            await connection.delete()
            await wait_for_tasks_completion(client, branch)
            await _poll(f"{circuit} cascaded", lambda: self._circuits_of(client, branch, CREATED, expected=0))
            inventory = await fetch_interconnect_inventory(client=client, branch=branch)
            assert CREATED not in await fetch_connections(client, branch)
            carrier = next((c for c in inventory["physical"] if c["circuit_id"] == CREATED_CARRIER), None)
            assert carrier is not None, "the carrier went with the connection that created it"
            assert carrier["ends"] == sorted([STATE["near"], (SERVICE_EDGE, CREATED_NIC)])
            assert carrier["status"] == "provisioning"
            # PC2 (checks/provider_connections.py) reports this carrier from now on: it ends in a provider-hosted
            # place and no connection lists it. That is the accepted leftover of a deleted request.
            assert carrier["connections"] == []
            assert not other_carrier_errors(STATE["carriers_before"], inventory, {CREATED_CARRIER})
            STATE["adoptable"] = carrier_state(inventory)
        finally:
            await self._remove_leftovers(client, branch, [CREATED])
            await wait_for_tasks_completion(client, branch)
        await verify_no_failed_tasks(client=client, branch=branch)

    @pytest.mark.order(436)
    @pytest.mark.dependency(
        scope="session", name="all_demo_provider_adopted", depends=["all_demo_provider_created_deleted"]
    )
    @pytest.mark.asyncio
    async def test_07_an_explicit_near_port_adopts_the_carrier_that_ends_on_it(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """A new request whose `near_port` is the port the leftover carrier ends on reuses it (found by its end, never
        by name or list): no `<name>-XC` carrier, no `vnic-<name>`, `carrier_circuits` is exactly that carrier, and the
        session rides the carrier's own two ends (the NIC of the first request)."""
        logging.info("=== %s - Step 7: Adopt an existing carrier ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        near = STATE["near"]
        port_id = await _port_id(client, branch, *near)
        STATE["adopter_id"] = await _request(client, branch, ADOPTER, near_port=port_id)
        await _converge(client, branch, STATE["adopter_id"])

        async def _judge() -> list[str]:
            inventory = await fetch_interconnect_inventory(client=client, branch=branch)
            row = (await fetch_connections(client, branch)).get(ADOPTER)
            if row is None:
                return [f"{ADOPTER}: no such connection"]
            errors = [] if row["carriers"] == [CREATED_CARRIER] else [f"carrier_circuits {row['carriers']}"]
            if row.get("near") != near:
                errors.append(f"near_port {row.get('near')}, expected {near}")
            carrier = next((c for c in inventory["physical"] if c["circuit_id"] == CREATED_CARRIER), None)
            if carrier is None or carrier["connections"] != [ADOPTER]:
                errors.append(f"{CREATED_CARRIER} lists {carrier and carrier['connections']}, expected [{ADOPTER}]")
            # nothing new: no carrier of its own, no NIC of its own, every other carrier as it was
            errors += other_carrier_errors(STATE["adoptable"], inventory, {CREATED_CARRIER})
            if any(c["circuit_id"] == f"{ADOPTER}-XC" for c in inventory["physical"]):
                errors.append(f"{ADOPTER}-XC was created, the existing carrier had to be reused")
            if await fetch_interface_ids(client, branch, [SERVICE_EDGE], [f"vnic-{ADOPTER}"]):
                errors.append(f"vnic-{ADOPTER} was created")
            return errors + await self._dedicated_errors(client, branch, ADOPTER, near, (SERVICE_EDGE, CREATED_NIC))

        await _poll(f"{ADOPTER} adopts {CREATED_CARRIER}", _judge)
        await verify_no_failed_tasks(client=client, branch=branch)

    @pytest.mark.order(437)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_refused", depends=["all_demo_provider_adopted"])
    @pytest.mark.asyncio
    async def test_08_an_explicit_near_port_in_use_is_refused_and_nothing_is_written(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """A request whose `near_port` carries the sub-interfaces of the adopter's session is refused (never another port
        is taken): the run fails, and the connection lists no carrier and holds no session, no NIC and no carrier was
        created, every other carrier is as it was. The refused runs stay as failed tasks of the branch: the final check
        of this module tolerates exactly those that name this connection."""
        logging.info("=== %s - Step 8: Busy explicit port ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        before = carrier_state(await fetch_interconnect_inventory(client=client, branch=branch))
        port_id = await _port_id(client, branch, *STATE["near"])
        refused_id = await _request(client, branch, REFUSED, near_port=port_id)
        await wait_for_tasks_completion(client, branch)
        outcome = await run_generator(client=client, generator_name=GENERATOR, node_ids=[refused_id], branch=branch)
        assert not outcome["success"], f"{GENERATOR} accepted a busy near_port: {outcome}"
        await wait_for_tasks_completion(client, branch)

        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        row = (await fetch_connections(client, branch))[REFUSED]
        assert row["carriers"] == [] and row["sessions"] == [], f"the refused connection holds {row}"
        assert row.get("vlan_id") is None
        assert not other_carrier_errors(before, inventory, set()), "the refused run touched a carrier"
        assert not [c for c in inventory["virtual"] if c["connection"] == REFUSED]
        assert not await fetch_interface_ids(client, branch, [SERVICE_EDGE], [f"vnic-{REFUSED}"])
        connection = await client.get(kind="TopologyProviderConnection", branch=branch, id=refused_id)
        await connection.delete()
        await wait_for_tasks_completion(client, branch)

    @pytest.mark.order(438)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_changed", depends=["all_demo_provider_refused"])
    @pytest.mark.asyncio
    async def test_09_changing_the_path_type_to_a_vpn_clears_the_carrier_list_and_the_old_outputs(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """The adopter becomes an internet_vpn in one save (the AWS VPN gateway's AS, the FR2 internet carrier port of
        the demo VPN, two provider-assigned ranges): its handoff is replaced by two `vpn` circuits on `Tunnel<n>` ends
        (the near ones riding the internet port), the sub-interfaces are deleted, `carrier_circuits` is cleared, no
        carrier is created, and the carrier it had adopted is left alone: shared underlay, listed by nobody (PC2 reports
        it, the accepted leftover of a request that moved to the internet)."""
        logging.info("=== %s - Step 9: Path type change ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        before = carrier_state(await fetch_interconnect_inventory(client=client, branch=branch))
        namespace_id = await _id(client, branch, "IpamNamespace", name__value=NAMESPACE)
        range_ids = []
        for prefix in VPN_RANGES:
            node = await client.create(
                kind="IpamPrefix",
                data={
                    "prefix": prefix,
                    "ip_namespace": {"id": namespace_id},
                    "status": "active",
                    "description": "test_67 provider-assigned tunnel range: deleted by the test",
                },
                branch=branch,
            )
            await node.save()
            range_ids.append(node.id)

        connection = await client.get(
            kind="TopologyProviderConnection", branch=branch, id=STATE["adopter_id"], include=["link_prefixes"]
        )
        getattr(connection, "path_type").value = "internet_vpn"  # noqa: B009 - SDK attributes are dynamic
        getattr(connection, "vlan_id").value = None  # noqa: B009
        setattr(
            connection, "provider_as", {"id": await _id(client, branch, "RoutingAutonomousSystem", asn__value=VPN_ASN)}
        )
        setattr(connection, "near_port", {"id": await _port_id(client, branch, *VPN_NEAR)})
        for range_id in range_ids:
            getattr(connection, "link_prefixes").add(range_id)  # noqa: B009
        await connection.save()
        await _converge(client, branch, STATE["adopter_id"])

        async def _judge() -> list[str]:
            return await self._vpn_errors(client, branch, ADOPTER, before)

        await _poll(f"{ADOPTER} as a vpn, old outputs cleaned", _judge)
        await _assert_no_other_failures(client, branch)

    @pytest.mark.order(439)
    @pytest.mark.dependency(scope="session", name="all_demo_provider_deleted", depends=["all_demo_provider_changed"])
    @pytest.mark.asyncio
    async def test_10_deleting_the_vpn_connection_cascades_its_circuits_and_restores_the_branch(
        self, async_client_main: InfrahubClient, scenario_branch: str
    ) -> None:
        """Deleting the connection deletes its circuits (cascade) and moves no carrier and no other connection; the
        test then removes the generator's leftovers (tunnels, peerings, ranges) and the carrier and NIC the first
        request created, and the carriers are exactly those test_01 found."""
        logging.info("=== %s - Step 10: Delete the VPN connection ===", SCENARIO_NAME)
        client, branch = async_client_main, scenario_branch
        try:
            connection = await client.get(kind="TopologyProviderConnection", branch=branch, name__value=ADOPTER)
            await connection.delete()
            await wait_for_tasks_completion(client, branch)
            await _poll(f"{ADOPTER} circuits cascaded", lambda: self._circuits_of(client, branch, ADOPTER, expected=0))
            inventory = await fetch_interconnect_inventory(client=client, branch=branch)
            assert not other_carrier_errors(STATE["carriers_before"], inventory, {CREATED_CARRIER})
            assert CREATED_CARRIER in carrier_state(inventory), "a carrier went with the connection that listed it"
            connections = await fetch_connections(client, branch)
            assert not {ADOPTER, CREATED, REFUSED} & set(connections)
            errors: list[str] = []
            circuits = {c["name"]: c for c in inventory["virtual"]}
            for name, expected in sorted(ALL_DEMO_CONNECTIONS.items()):
                errors += connection_errors(connections.get(name), name, expected)
                for link in expected["links"]:
                    errors += link_errors(circuits.get(link["name"]), expected, link, name)
            assert not errors, "the throwaway connections moved the others:\n  - " + "\n  - ".join(errors)
        finally:
            await self._remove_leftovers(client, branch, [ADOPTER, REFUSED, CREATED], final=True)
            await wait_for_tasks_completion(client, branch)
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        assert carrier_state(inventory) == STATE["carriers_before"], "the carriers are not as test_01 found them"
        await _assert_no_other_failures(client, branch)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    # ------------------------------------------------------------------
    # Judges of the throwaway connections
    # ------------------------------------------------------------------

    @staticmethod
    async def _circuits_of(client: InfrahubClient, branch: str, connection: str, expected: int) -> list[str]:
        """Finding when the connection does not hold ``expected`` session circuits."""
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        found = sorted(c["name"] for c in inventory["virtual"] if c["connection"] == connection)
        return [] if len(found) == expected else [f"{connection} holds {found}, expected {expected} circuit(s)"]

    async def _created_errors(self, client: InfrahubClient, branch: str, name: str, near: tuple[str, str]) -> list[str]:
        """The connection built from intent: its carrier (ends, status, type, provider, group, derived locations), the
        NIC, no other carrier, and the dedicated link on the carrier's ends."""
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        row = (await fetch_connections(client, branch)).get(name)
        errors = created_connection_errors(
            name, row, inventory, near=near, far_device=SERVICE_EDGE, carrier_type="virtual_connection"
        )
        errors += other_carrier_errors(STATE["carriers_before"], inventory, {f"{name}-XC"})
        carrier = next((c for c in inventory["physical"] if c["circuit_id"] == f"{name}-XC"), None)
        if carrier is not None:
            # add_circuit derives the facilities of the ends when the carrier is created
            places = sorted([NEAR_FACILITY, FAR_FACILITY])
            if carrier["locations"] != places:
                errors.append(f"{name}-XC: locations {carrier['locations']}, expected {places}")
            members = clean_data(
                await client.execute_graphql(
                    query=GROUP_MEMBERS_QUERY, variables={"name": GROUP_CARRIER_CIRCUITS}, branch_name=branch
                )
            )["CoreGroup"]
            held = {str(member["id"]) for member in (members[0]["members"] if members else [])}
            if carrier["id"] not in held:
                errors.append(f"{name}-XC is not in the group {GROUP_CARRIER_CIRCUITS}")
        if not await fetch_interface_ids(client, branch, [SERVICE_EDGE], [f"vnic-{name}"]):
            errors.append(f"no NIC vnic-{name} on {SERVICE_EDGE}")
        if errors:
            return errors
        return await self._dedicated_errors(client, branch, name, near, (SERVICE_EDGE, f"vnic-{name}"))

    async def _dedicated_errors(
        self, client: InfrahubClient, branch: str, name: str, near: tuple[str, str], far: tuple[str, str]
    ) -> list[str]:
        """One handoff `<name>-L1` on `<port>.<vlan>` ends of ``near`` and ``far`` (device, port), the VLAN written back
        to the connection, and the session of the AWS Direct Connect flavour (a /31 of our pool, our end the lower)."""
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        row = (await fetch_connections(client, branch)).get(name) or {}
        circuit_name = f"{name}-L1"
        circuits = [c for c in inventory["virtual"] if c["connection"] == name]
        if [c["name"] for c in circuits] != [circuit_name]:
            return [f"{name} holds {[c['name'] for c in circuits]}, expected [{circuit_name}]"]
        vlan = row.get("vlan_id")
        if not vlan:
            return [f"{name}: the chosen VLAN was not written back to vlan_id"]
        (circuit,) = circuits
        ends = sorted([(near[0], f"{near[1]}.{vlan}"), (far[0], f"{far[1]}.{vlan}")])
        for device, port in (near, far):
            STATE.setdefault("interfaces", {}).setdefault(device, set()).add(f"{port}.{vlan}")
        errors: list[str] = []
        if circuit["circuit_type"] != "handoff":
            errors.append(f"{circuit_name} is a {circuit['circuit_type']}, expected handoff")
        if circuit["ends"] != ends:
            errors.append(f"ends {circuit['ends']}, expected {ends}")
        if row.get("sessions") != [circuit_name]:
            errors.append(f"connection lists sessions {row.get('sessions')}")
        if errors:
            return errors
        sessions = _sessions_by_name(
            await client.execute_graphql(
                query=SESSIONS_QUERY, variables={"names": [f"{circuit_name}-bgp"]}, branch_name=branch
            )
        )
        prefixes = _prefixes_by_description(
            await client.execute_graphql(
                query=CIRCUIT_PREFIXES_QUERY,
                variables={"descriptions": [f"{LABEL_HANDOFF} {circuit_name}"]},
                branch_name=branch,
            )
        )
        pools = await self._pools(client, branch, {NAMESPACE})
        flavour, _ = _flavour("PC-FR2-AWS-EUC1-PROD")
        return routed_session_errors(
            circuit_name,
            circuit,
            sessions.get(f"{circuit_name}-bgp"),
            label=LABEL_HANDOFF,
            flavour=flavour,
            pools=pools[NAMESPACE],
            prefixes=prefixes.get(f"{LABEL_HANDOFF} {circuit_name}"),
            party=(SERVICE_EDGE, DIRECT_CONNECT_ASN),
        )

    async def _vpn_errors(
        self, client: InfrahubClient, branch: str, name: str, carriers_before: dict[str, tuple[Any, ...]]
    ) -> list[str]:
        """The adopter after the change: two vpn circuits `<name>-L1`, `-L2` on `Tunnel<n>` ends (the near ones riding the
        internet port), no VLAN, no carrier listed, no carrier created, the adopted carrier untouched, and none of the
        old sub-interfaces left."""
        inventory = await fetch_interconnect_inventory(client=client, branch=branch)
        row = (await fetch_connections(client, branch)).get(name) or {}
        circuits = sorted((c for c in inventory["virtual"] if c["connection"] == name), key=lambda c: c["name"])
        errors: list[str] = []
        if [c["name"] for c in circuits] != [f"{name}-L1", f"{name}-L2"]:
            return [f"{name} holds {[c['name'] for c in circuits]}, expected two vpn circuits"]
        for circuit in circuits:
            if circuit["circuit_type"] != "vpn":
                errors.append(f"{circuit['name']} is a {circuit['circuit_type']}, expected vpn")
            by_device = {end["device"]: end for end in circuit["end_nodes"]}
            near, far = by_device.get(VPN_NEAR[0]), by_device.get(VPN_STUB)
            if (
                near is None
                or far is None
                or not (TUNNEL.fullmatch(near["interface"]) and TUNNEL.fullmatch(far["interface"]))
            ):
                errors.append(
                    f"{circuit['name']}: ends {circuit['ends']}, expected a Tunnel on {VPN_NEAR[0]} and {VPN_STUB}"
                )
            elif near["parent_name"] != VPN_NEAR[1]:
                errors.append(f"{circuit['name']}: near tunnel rides {near['parent_name']!r}, expected {VPN_NEAR[1]!r}")
            else:
                STATE.setdefault("interfaces", {}).setdefault(VPN_NEAR[0], set()).add(near["interface"])
                STATE["interfaces"].setdefault(VPN_STUB, set()).add(far["interface"])
        if row.get("path_type") != "internet_vpn" or row.get("vlan_id") is not None:
            errors.append(f"connection is {row.get('path_type')} with vlan {row.get('vlan_id')}")
        if row.get("carriers") != []:
            errors.append(f"connection still lists carriers {row.get('carriers')}")
        errors += other_carrier_errors(carriers_before, inventory, {CREATED_CARRIER})
        carrier = next((c for c in inventory["physical"] if c["circuit_id"] == CREATED_CARRIER), None)
        if (
            carrier is None
            or carrier["connections"] != []
            or carrier["ends"] != sorted([STATE["near"], (SERVICE_EDGE, CREATED_NIC)])
        ):
            errors.append(f"the adopted carrier {CREATED_CARRIER} was touched: {carrier}")
        old = {
            device: sorted(n for n in names if not TUNNEL.fullmatch(n)) for device, names in STATE["interfaces"].items()
        }
        for device, names in old.items():
            left = await fetch_interface_ids(client, branch, [device], names) if names else []
            errors += [f"old sub-interface still exists: {item}" for item in left]
        return errors

    @staticmethod
    async def _remove_leftovers(
        client: InfrahubClient, branch: str, connections: list[str], *, final: bool = False
    ) -> None:
        """Remove what the deletes leave (the generator's documented leftovers): the connections themselves if a failed
        step kept them, the peerings add_virtual_circuit built, the sub-interfaces and tunnels the test noted, and,
        once the last connection of the module is gone, the carrier and NIC the first request created and the ranges."""
        for name in connections:
            for node in await client.filters(kind="TopologyProviderConnection", branch=branch, name__value=name):
                await node.delete()
            for circuit in (f"{name}-L1", f"{name}-L2"):
                for peering in await client.filters(
                    kind="ManagedBGPPeering", branch=branch, name__value=f"{circuit}-bgp"
                ):
                    await peering.delete()
        for device, names in sorted(STATE.get("interfaces", {}).items()):
            for interface in await client.filters(
                kind="DcimVirtualInterface", branch=branch, device__name__value=device, name__values=sorted(names)
            ):
                await interface.delete()
        if final:
            for carrier in await client.filters(
                kind="TopologyCarrierCircuit", branch=branch, circuit_id__value=CREATED_CARRIER
            ):
                await carrier.delete()
            for nic in await client.filters(
                kind="DcimVirtualInterface", branch=branch, device__name__value=SERVICE_EDGE, name__value=CREATED_NIC
            ):
                await nic.delete()
            for prefix in await client.filters(kind="IpamPrefix", branch=branch, prefix__values=list(VPN_RANGES)):
                try:
                    await prefix.delete()
                except Exception:  # noqa: BLE001 - a range with addresses left by add_virtual_circuit may refuse
                    logging.warning("could not delete the test range %s", prefix.id)


async def _assert_no_other_failures(client: InfrahubClient, branch: str) -> None:
    """verify_no_failed_tasks, except for the runs the busy-port scenario made fail on purpose."""
    unexpected = await unexpected_failed_tasks(client, branch, (REFUSED,))
    assert not unexpected, "failed task(s) other than the refusal of the busy port:\n  - " + "\n  - ".join(unexpected)


async def _port_id(client: InfrahubClient, branch: str, device: str, name: str) -> str:
    """The id of the physical interface ``name`` of ``device``."""
    return (
        await client.get(kind="DcimPhysicalInterface", branch=branch, device__name__value=device, name__value=name)
    ).id


async def _request(client: InfrahubClient, branch: str, name: str, near_port: str | None = None) -> str:
    """Create a connection with nothing but intent (near, far, path_type, ip_namespace, the hub account and its AS);
    ``near_port`` is the one optional addition (an explicit port). Returns its id."""
    data: dict[str, Any] = {
        "name": name,
        "description": "test_67 throwaway connection: deleted by the test",
        "path_type": "dedicated",
        "status": "active",
        "bandwidth": 1000,
        "near": {"id": await _id(client, branch, "TopologyColocationZone", name__value=NEAR_FACILITY)},
        "far": {"id": await _id(client, branch, FAR_KIND, name__value=FAR_FACILITY)},
        "ip_namespace": {"id": await _id(client, branch, "IpamNamespace", name__value=NAMESPACE)},
        "account": {"id": await _id(client, branch, "CloudAccount", name__value=ACCOUNT)},
        "provider_as": {"id": await _id(client, branch, "RoutingAutonomousSystem", asn__value=DIRECT_CONNECT_ASN)},
        "member_of_groups": [
            {"id": await _id(client, branch, "CoreStandardGroup", name__value=GROUP_PROVIDER_CONNECTIONS)}
        ],
    }
    if near_port:
        data["near_port"] = {"id": near_port}
    connection = await client.create(kind="TopologyProviderConnection", data=data, branch=branch)
    await connection.save()
    logging.info("Created %s (%s)", name, connection.id)
    return str(connection.id)
