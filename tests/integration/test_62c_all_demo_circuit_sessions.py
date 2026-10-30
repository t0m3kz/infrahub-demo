"""Integration test — the 30_all colocation, session and internet steps (continues test_62).

The colocation serving contexts, the sessions add_virtual_circuit builds (two per DCI, one per Handoff, none for a
Tunnel), the internet upstream, the zone edge pair and the office WAN exits.

Runs against the branch test_59 builds; it never loads data of its own.
"""

import ipaddress
import logging

import pytest
from infrahub_sdk import InfrahubClient

from tasks import ALL_DEMO_BRANCH
from utils.exchange_transit import transit_vlan, wan_exit_vlan
from utils.limits import CIRCUIT_ENDS, FW_CONTEXT_VLAN_END, FW_CONTEXT_VLAN_START, UNDERLAY_MTU

from .all_demo_circuit_constants import (
    ALL_DEMO_COLOCATION_SERVED,
    ALL_DEMO_COLOCATION_SERVING_ZONES,
    ALL_DEMO_DCI_CIRCUITS,
    ALL_DEMO_EDGE_INTERLINK_INTERFACE,
    ALL_DEMO_EDGE_INTERLINKS,
    ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE,
    ALL_DEMO_FABRIC_PEERINGS,
    ALL_DEMO_HANDOFF_PARTY_ASNS,
    ALL_DEMO_INTERNET_BGWS,
    ALL_DEMO_INTERNET_TRANSIT_PEERINGS,
    ALL_DEMO_INTERNET_UPSTREAM_BY_DC,
    ALL_DEMO_OFFICE_WAN_EXITS,
    ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT,
    ALL_DEMO_VIRTUAL_CIRCUITS,
    ALL_DEMO_ZONE_SITE_VIPS,
    HANDOFF_TYPE,
    TUNNEL_TYPE,
)
from .all_demo_firewall_constants import (
    ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS,
    ALL_DEMO_FIREWALL_MEMBERS,
    OFFICE_NAMESPACE,
    transit_namespace_type,
)
from .circuit_helpers import fetch_interconnect_inventory
from .conftest import TestInfrahubDockerWithClient
from .interconnect_queries import (
    CIRCUIT_PREFIXES_QUERY,
    COLOCATION_CONTEXTS_QUERY,
    EDGE_INTERLINK_CABLES_QUERY,
    EDGE_INTERLINK_INTERFACES_QUERY,
    EDGE_PAIR_POOL_QUERY,
    EDGE_PAIR_VIP_QUERY,
    SCENARIO_NAME,
    SESSIONS_QUERY,
    TRANSIT_SESSIONS_QUERY,
    WAN_EXIT_SESSIONS_QUERY,
)
from .interconnect_rules import (
    DCI_POOL_NETWORK,
    _address,
    _address_namespace,
    _interfaces,
    _p2p_errors,
    _prefixes_by_description,
    _process_nodes,
    _process_roles,
    _session_errors,
    _sessions_by_name,
    _vlan,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


def _cluster_members(context: dict) -> frozenset[str]:
    """The ids of the devices of the cluster a serving context sits on."""
    cluster = (context.get("cluster") or {}).get("node") or {}
    return frozenset(edge["node"]["id"] for edge in (cluster.get("capabilities") or {}).get("edges", []))


class TestAllDemoInterconnects(TestInfrahubDockerWithClient):
    """Verify the circuit layer and the tenant-scoped services on top of it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    @pytest.mark.order(408)
    @pytest.mark.dependency(scope="session", depends=["all_demo_tenant_services"])
    @pytest.mark.asyncio
    async def test_05_verify_colocation_serving_contexts(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each colocation deployment is served by the firewalls of every zone of its metro.

        A deployment is served by one shared context per zone of its metro that has a firewall pair (only FR2
        in 30_all). That context sits on the zone's cluster and has a VLAN from the zone's pool and one transit
        sub-interface per member per VRF leg (ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS; test_03b checks their
        addresses and exchanges). A deployment whose metro has no zone with firewalls has no context:
        pointing it at another metro's firewalls would hairpin its traffic across the WAN.
        """
        logging.info("=== %s - Step 5: Colocation Serving Contexts ===", SCENARIO_NAME)

        result = await async_client_main.execute_graphql(query=COLOCATION_CONTEXTS_QUERY, branch_name=scenario_branch)
        deployments = {
            edge["node"]["name"]["value"]: edge["node"] for edge in result["TopologyCustomerColocation"]["edges"]
        }

        errors: list[str] = []
        if sorted(deployments) != sorted(ALL_DEMO_COLOCATION_SERVED):
            errors.append(
                f"colocation deployments {sorted(deployments)}, expected {sorted(ALL_DEMO_COLOCATION_SERVED)}"
            )

        for name, served in sorted(ALL_DEMO_COLOCATION_SERVED.items()):
            deployment = deployments.get(name)
            if deployment is None:
                continue
            metro = (deployment.get("parent") or {}).get("node") or {}
            # Zone name -> its firewall ids, for the metro's zones that have a firewall pair (a virtual colocation
            # child arrives as an empty node).
            firewall_zones = {
                zone["name"]["value"]: {edge["node"]["id"] for edge in zone["devices"]["edges"]}
                for zone in (edge["node"] for edge in (metro.get("children") or {}).get("edges", []))
                if zone.get("name") and zone["devices"]["edges"]
            }
            contexts = [edge["node"] for edge in (deployment.get("serving_firewall_contexts") or {}).get("edges", [])]
            serving_zones = ALL_DEMO_COLOCATION_SERVING_ZONES[name]
            if sorted(firewall_zones) != sorted(serving_zones):
                errors.append(
                    f"{name}: the metro's zones with firewalls are {sorted(firewall_zones)}, expected {serving_zones}"
                )

            if not served:
                if contexts:
                    errors.append(
                        f"{name}: no zone with firewalls, but served by {[c['name']['value'] for c in contexts]}"
                    )
                continue

            clusters = {_cluster_members(c) for c in contexts}
            if len(contexts) != len(serving_zones) or clusters != {frozenset(ids) for ids in firewall_zones.values()}:
                errors.append(
                    f"{name}: served by {[c['name']['value'] for c in contexts]}, expected one shared context on "
                    f"the cluster of each of {sorted(firewall_zones)}"
                )
                continue
            for zone_name, zone_firewalls in sorted(firewall_zones.items()):
                context = next(c for c in contexts if _cluster_members(c) == zone_firewalls)
                context_name = context["name"]["value"]
                if (context.get("tenant") or {}).get("node"):
                    errors.append(
                        f"{name}: served by tenant-dedicated '{context_name}', expected the zone's shared one"
                    )
                expected = next(
                    (
                        e
                        for e in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS
                        if e["site"] == zone_name and e["tenant"] is None and name in e["deployments"]
                    ),
                    None,
                )
                if expected is None:
                    errors.append(f"{name}: no expected transit context for the zone '{zone_name}'")
                    continue
                vlan = (context.get("vlan_id") or {}).get("value")
                if not vlan or not FW_CONTEXT_VLAN_START <= int(vlan) <= FW_CONTEXT_VLAN_END:
                    errors.append(f"{name}: '{context_name}' VLAN {vlan}, expected one in the zone pool")
                    continue
                # Sub-interfaces are `<uplink>.<vlan>`; the tagged edge ports (Ethernet1/15-16) have no dot.
                sub_vlans = sorted(
                    str(edge["node"]["name"]["value"]).rsplit(".", 1)[-1]
                    for edge in (context.get("capabilities") or {}).get("edges", [])
                    if "." in str(edge["node"]["name"]["value"])
                )
                expected_vlans = sorted(
                    str(transit_vlan(int(vlan), transit_namespace_type(namespace)))
                    for namespace in expected["namespaces"]
                    for _ in range(ALL_DEMO_FIREWALL_MEMBERS)
                )
                if sub_vlans != expected_vlans:
                    errors.append(
                        f"{name}: '{context_name}' sub-interface VLANs {sub_vlans}, expected {expected_vlans}"
                    )

        assert not errors, f"30_all colocation serving contexts are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info(
            "Colocation serving contexts verified: %d served, %d edge-only",
            sum(ALL_DEMO_COLOCATION_SERVED.values()),
            len(ALL_DEMO_COLOCATION_SERVED) - sum(ALL_DEMO_COLOCATION_SERVED.values()),
        )

    # ------------------------------------------------------------------
    # Sessions of the virtual circuits (add_virtual_circuit)
    # ------------------------------------------------------------------

    @pytest.mark.order(409)
    @pytest.mark.dependency(scope="session", depends=["all_demo_virtual_circuits"])
    @pytest.mark.asyncio
    async def test_06_verify_dci_sessions(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify add_virtual_circuit built two keyed sessions on each Interconnect (DCI) circuit.

        Per circuit `<name>`: `<name>-underlay` (peering_role dci-underlay, eBGP, ttl 1, bfd, IPv6 unicast, 16 routes)
        on the two physical ports, addressed from one /127 of the DCI pool (the colocation edge the lower address) and
        MTU 9214; and `<name>-evpn` (peering_role dci, EBGP_MULTIHOP, ttl 2, extended communities, l2vpn evpn only)
        between the two VTEP loopbacks. Both run on the ends' overlay BGP processes and carry the circuit's own key
        `<name>-dci-key` (created once, never a fabric's overlay key). The /127 is the one prefix `DCI <name>` of the DCI pool.
        """
        logging.info("=== %s - Step 6: DCI Sessions ===", SCENARIO_NAME)

        client = async_client_main
        errors: list[str] = []
        names = {f"{circuit}-{suffix}": circuit for circuit in ALL_DEMO_DCI_CIRCUITS for suffix in ("underlay", "evpn")}
        sessions = _sessions_by_name(
            await client.execute_graphql(
                query=SESSIONS_QUERY, variables={"names": sorted(names)}, branch_name=scenario_branch
            )
        )
        if sorted(sessions) != sorted(names):
            errors.append(f"DCI sessions {sorted(sessions)}, expected {sorted(names)}")
        for role, expected_count in (("dci", len(ALL_DEMO_DCI_CIRCUITS)), ("dci-underlay", len(ALL_DEMO_DCI_CIRCUITS))):
            count = await client.execute_graphql(
                query="query ($role: String) { ManagedBGPPeering(peering_role__value: $role) { count } }",
                variables={"role": role},
                branch_name=scenario_branch,
            )
            if count["ManagedBGPPeering"]["count"] != expected_count:
                errors.append(
                    f"{count['ManagedBGPPeering']['count']} session(s) with peering_role {role}, expected {expected_count}"
                )
        prefixes = _prefixes_by_description(
            await client.execute_graphql(
                query=CIRCUIT_PREFIXES_QUERY,
                variables={"descriptions": [f"DCI {circuit}" for circuit in ALL_DEMO_DCI_CIRCUITS]},
                branch_name=scenario_branch,
            )
        )

        keys: dict[str, str] = {}
        for circuit, (_, ends) in sorted(ALL_DEMO_DCI_CIRCUITS.items()):
            devices = sorted(device for device, _ in ends)
            underlay = sessions.get(f"{circuit}-underlay")
            evpn = sessions.get(f"{circuit}-evpn")
            if underlay is None or evpn is None:
                continue
            key = f"{circuit}-dci-key"
            errors.extend(
                _session_errors(
                    underlay,
                    {
                        "status": "active",
                        "peering_role": "dci-underlay",
                        "session_type": "EBGP",
                        "ttl": 1,
                        "bfd_enabled": True,
                        "send_extended_community": False,
                        "maximum_routes": 16,
                        "key": key,
                        "families": [("ipv6", "unicast")],
                    },
                )
            )
            errors.extend(
                _session_errors(
                    evpn,
                    {
                        "status": "active",
                        "peering_role": "dci",
                        "session_type": "EBGP_MULTIHOP",
                        "ttl": 2,
                        "bfd_enabled": False,
                        "send_extended_community": True,
                        "maximum_routes": None,
                        "key": key,
                        "families": [("l2vpn", "evpn")],
                    },
                )
            )
            keys[circuit] = key
            for session in (underlay, evpn):
                if _process_roles(session) != {device: "overlay" for device in devices}:
                    errors.append(
                        f"{session['name']['value']}: runs on {_process_roles(session)}, expected {devices} overlay"
                    )

            # underlay: the two physical ports, one /127 of the DCI pool, the edge the lower address, MTU 9214
            ports = _interfaces(underlay)
            if sorted(ports) != sorted(ends):
                errors.append(f"{circuit}-underlay: on interfaces {sorted(ports)}, expected {sorted(ends)}")
                continue
            for (device, port), node in sorted(ports.items()):
                if node.get("mtu", {}).get("value") != UNDERLAY_MTU:
                    errors.append(
                        f"{circuit}-underlay: {device}:{port} MTU {node.get('mtu', {}).get('value')}, expected {UNDERLAY_MTU}"
                    )
            addresses = {end: _address(node) for end, node in ports.items()}
            errors.extend(
                _p2p_errors(
                    f"{circuit}-underlay",
                    list(addresses.values()),
                    127,
                    [DCI_POOL_NETWORK],
                    prefixes.get(f"DCI {circuit}"),
                )
            )
            if any(a is None or ":" not in a for a in addresses.values()):
                continue
            edge = next(end for end in addresses if end[0].startswith("eg-"))
            dc_end = next(end for end in addresses if end != edge)
            if ipaddress.IPv6Interface(str(addresses[edge])) > ipaddress.IPv6Interface(str(addresses[dc_end])):
                errors.append(
                    f"{circuit}-underlay: the colocation edge {edge[0]} has {addresses[edge]}, above the DC end's "
                    f"{addresses[dc_end]}: the facility end takes the lower address"
                )

            # evpn: between the two VTEP loopbacks (never the multisite VIP), /128 IPv6, one per end device
            loopbacks = _interfaces(evpn)
            if sorted(device for device, _ in loopbacks) != devices:
                errors.append(f"{circuit}-evpn: on devices {sorted(d for d, _ in loopbacks)}, expected {devices}")
                continue
            for (device, loopback), node in sorted(loopbacks.items()):
                role = node.get("role", {}).get("value")
                address = _address(node)
                if role not in ("loopback-vtep", "loopback"):
                    errors.append(f"{circuit}-evpn: {device}:{loopback} has role '{role}', expected the VTEP loopback")
                if address is None or ipaddress.ip_interface(address).network.prefixlen != 128 or ":" not in address:
                    errors.append(f"{circuit}-evpn: {device}:{loopback} address {address}, expected an IPv6 /128")

        if len(set(keys.values())) != len(keys):
            errors.append(f"the DCI circuits share a key: {sorted(keys.values())}")

        assert not errors, f"30_all DCI sessions are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("DCI sessions verified: %d on %d circuits", len(sessions), len(ALL_DEMO_DCI_CIRCUITS))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(409)
    @pytest.mark.dependency(scope="session", depends=["all_demo_virtual_circuits"])
    @pytest.mark.asyncio
    async def test_06b_verify_handoff_sessions(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify add_virtual_circuit built exactly one eBGP session on each Handoff circuit.

        Per circuit `<name>`: one `<name>-bgp` (peering_role regular, EBGP, ttl 1, bfd, no extended communities, the
        circuit's maximum_routes, IPv4 unicast, the circuit's key) between the two devices' BGP processes, on the two
        `<port>.<vlan>` sub-interfaces. Both are addressed in the circuit's namespace from one /31 of the pool
        `Handoff-<namespace>-IPv4` (the overlay-process end the lower address, else the first device by name); the
        prefix is the one `Handoff <name>`. The party process's `local_as` is the data's ASN, ours differs, and the
        VLAN is the same on both ends (and the VIF's).
        """
        logging.info("=== %s - Step 6b: Handoff Sessions ===", SCENARIO_NAME)

        client = async_client_main
        errors: list[str] = []
        inventory = await fetch_interconnect_inventory(client=client, branch=scenario_branch)
        handoffs = {c["name"]: c for c in inventory["virtual"] if c["circuit_type"] == HANDOFF_TYPE}
        expected = sorted(n for n, e in ALL_DEMO_VIRTUAL_CIRCUITS.items() if e["circuit_type"] == HANDOFF_TYPE)
        if sorted(handoffs) != expected:
            errors.append(f"handoff circuits {sorted(handoffs)}, expected {expected}")

        sessions = _sessions_by_name(
            await client.execute_graphql(
                query=SESSIONS_QUERY,
                variables={"names": sorted(f"{n}-bgp" for n in handoffs)},
                branch_name=scenario_branch,
            )
        )
        prefixes = _prefixes_by_description(
            await client.execute_graphql(
                query=CIRCUIT_PREFIXES_QUERY,
                variables={"descriptions": [f"Handoff {n}" for n in handoffs]},
                branch_name=scenario_branch,
            )
        )
        pools: dict[str, list[ipaddress.IPv4Network]] = {}
        for namespace in sorted({c["ip_namespace"] for c in handoffs.values() if c["ip_namespace"]}):
            pool = await client.execute_graphql(
                query=EDGE_PAIR_POOL_QUERY, variables={"name": f"Handoff-{namespace}-IPv4"}, branch_name=scenario_branch
            )
            pools[namespace] = [
                ipaddress.IPv4Network(r["node"]["prefix"]["value"])
                for pool_edge in pool["CoreIPPrefixPool"]["edges"]
                for r in pool_edge["node"]["resources"]["edges"]
                if r["node"].get("prefix")
            ]
            if not pools[namespace]:
                errors.append(f"pool Handoff-{namespace}-IPv4 is missing or has no prefix")

        for name, circuit in sorted(handoffs.items()):
            session = sessions.get(f"{name}-bgp")
            if session is None:
                errors.append(f"{name}: no {name}-bgp session")
                continue
            errors.extend(
                _session_errors(
                    session,
                    {
                        "status": "active",
                        "peering_role": "regular",
                        "session_type": "EBGP",
                        "ttl": 1,
                        "bfd_enabled": True,
                        "send_extended_community": False,
                        "maximum_routes": circuit["maximum_routes"],
                        "key": circuit["password"],
                        "families": [("ipv4", "unicast")],
                    },
                )
            )
            if circuit["maximum_routes"] is None or circuit["password"] is None:
                errors.append(
                    f"{name}: maximum_routes {circuit['maximum_routes']} / key {circuit['password']}, both are authored"
                )

            # the two sub-interfaces: the circuit's ends, one namespace, one /31 of its pool
            ports = _interfaces(session)
            if sorted(ports) != circuit["ends"]:
                errors.append(f"{name}-bgp: on interfaces {sorted(ports)}, expected {circuit['ends']}")
                continue
            by_end = {(end["device"], end["interface"]): end for end in circuit["end_nodes"]}
            for end, node in sorted(ports.items()):
                namespace = _address_namespace(node)
                if namespace != circuit["ip_namespace"]:
                    errors.append(
                        f"{name}-bgp: {end[0]}:{end[1]} is addressed in '{namespace}', expected '{circuit['ip_namespace']}'"
                    )
                if _address(node) != by_end[end]["address"]:
                    errors.append(
                        f"{name}-bgp: {end[0]}:{end[1]} address {_address(node)} differs from the circuit's end {by_end[end]['address']}"
                    )
                if node.get("status", {}).get("value") != "active":
                    errors.append(
                        f"{name}-bgp: {end[0]}:{end[1]} status {node.get('status', {}).get('value')}, expected active"
                    )
                if not str(node.get("description", {}).get("value") or "").startswith(f"Handoff {name} to "):
                    errors.append(
                        f"{name}-bgp: {end[0]}:{end[1]} description '{node.get('description', {}).get('value')}'"
                    )
                parent = ((node.get("parent_interface") or {}).get("node") or {}).get("name", {}).get("value")
                if parent != by_end[end]["parent_name"] or end[1] != f"{parent}.{_vlan(end[1])}":
                    errors.append(f"{name}-bgp: {end[0]}:{end[1]} is not <parent>.<vlan> of its parent ({parent})")
            addresses = {end: _address(node) for end, node in ports.items()}
            errors.extend(
                _p2p_errors(
                    f"{name}-bgp",
                    list(addresses.values()),
                    31,
                    pools.get(circuit["ip_namespace"] or "", []),
                    prefixes.get(f"Handoff {name}"),
                )
            )

            # processes: one per device; remote ASN is the party stub's local_as, ours differs (eBGP)
            processes = _process_nodes(session)
            devices = sorted(device for device, _ in circuit["ends"])
            if sorted(processes) != devices:
                errors.append(f"{name}-bgp: runs on processes of {sorted(processes)}, expected {devices}")
                continue
            asns = {device: node["local_as"]["node"]["asn"]["value"] for device, node in processes.items()}
            if len(set(asns.values())) != len(asns):
                errors.append(
                    f"{name}-bgp: both processes are in AS {sorted(set(asns.values()))}, an eBGP session needs two"
                )
            if name in ALL_DEMO_HANDOFF_PARTY_ASNS:
                party, asn = ALL_DEMO_HANDOFF_PARTY_ASNS[name]
                if asns.get(party) != asn:
                    errors.append(f"{name}-bgp: party {party} is in AS {asns.get(party)}, expected the data's AS{asn}")
            # our end (the overlay process), else the first device by name, has the lower address
            lower = min(devices, key=lambda device: (processes[device]["process_role"]["value"] != "overlay", device))
            lower_end = next(end for end in addresses if end[0] == lower)
            if all(addresses.values()) and lower_end != min(
                addresses, key=lambda end: ipaddress.IPv4Interface(str(addresses[end]))
            ):
                errors.append(
                    f"{name}-bgp: {lower} (the lower end) has {addresses[lower_end]}, not the lower address of the /31"
                )

        assert not errors, f"30_all handoff sessions are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Handoff sessions verified: %d", len(handoffs))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(409)
    @pytest.mark.dependency(scope="session", depends=["all_demo_virtual_circuits"])
    @pytest.mark.asyncio
    async def test_06c_verify_tunnels_have_no_session(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify a Tunnel circuit is an inventory record: no session of any name, ends on the gateway's public port
        and one more port, and no port it ends on is also an end of a session circuit (one gateway port serves many
        tunnels, but no Interconnect or Handoff).
        """
        logging.info("=== %s - Step 6c: Tunnels ===", SCENARIO_NAME)

        client = async_client_main
        inventory = await fetch_interconnect_inventory(client=client, branch=scenario_branch)
        tunnels = [c for c in inventory["virtual"] if c["circuit_type"] == TUNNEL_TYPE]
        errors: list[str] = []
        if not tunnels:
            errors.append("no tunnel circuit on the branch")
        names = [f"{tunnel['name']}{suffix}" for tunnel in tunnels for suffix in ("", "-bgp", "-underlay", "-evpn")]
        found = _sessions_by_name(
            await client.execute_graphql(query=SESSIONS_QUERY, variables={"names": names}, branch_name=scenario_branch)
        )
        if found:
            errors.append(f"tunnel session(s) {sorted(found)} exist: a tunnel has none")

        session_ports = {
            end["id"] for c in inventory["virtual"] if c["circuit_type"] != TUNNEL_TYPE for end in c["end_nodes"]
        }
        for tunnel in tunnels:
            gateway = [end for end in tunnel["end_nodes"] if end["interface"] == ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT]
            if len(gateway) != 1 or len(tunnel["end_nodes"]) != CIRCUIT_ENDS:
                errors.append(
                    f"{tunnel['name']}: ends {tunnel['ends']}, expected the gateway's '{ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT}' and one more"
                )
            shared = sorted(
                f"{end['device']}:{end['interface']}" for end in tunnel["end_nodes"] if end["id"] in session_ports
            )
            if shared:
                errors.append(f"{tunnel['name']}: end(s) {shared} are also ends of an interconnect or handoff circuit")

        assert not errors, f"30_all tunnels are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Tunnels verified: %d, no session", len(tunnels))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(410)
    @pytest.mark.dependency(scope="session", depends=["all_demo_carrier_circuits"])
    @pytest.mark.asyncio
    async def test_07_verify_internet_upstream(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the ISP transit sessions and that each DC's DCI lands on both upstream BGWs.

        The four TRANSIT-* eBGP sessions run in the INTERNET namespace (an
        `internet`-type namespace is what makes the edge filter them and
        export 0/0 as EVPN type-5). The upstream BGW of a DC is the edge its
        dark fibres end on: both edges of its metro, each a Multi-Site BGW with its own DCI.
        """
        logging.info("=== %s - Step 7: INTERNET upstream ===", SCENARIO_NAME)

        errors: list[str] = []
        result = await async_client_main.execute_graphql(
            query=TRANSIT_SESSIONS_QUERY,
            variables={"names": sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS)},
            branch_name=scenario_branch,
        )
        sessions = {edge["node"]["name"]["value"]: edge["node"] for edge in result["ManagedBGPPeering"]["edges"]}
        if sorted(sessions) != sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS):
            errors.append(f"transit sessions {sorted(sessions)}, expected {sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS)}")

        for name, edge_device in sorted(ALL_DEMO_INTERNET_TRANSIT_PEERINGS.items()):
            session = sessions.get(name)
            if session is None:
                continue
            if session["session_type"]["value"] != "EBGP":
                errors.append(f"{name}: session type {session['session_type']['value']}, expected EBGP")
            local = [
                edge["node"]
                for edge in session["capabilities"]["edges"]
                if edge["node"]["device"]["node"]["name"]["value"] == edge_device
            ]
            if len(local) != 1:
                errors.append(f"{name}: {len(local)} interface(s) on {edge_device}, expected 1")
                continue
            namespace = (((local[0].get("ip_address") or {}).get("node") or {}).get("ip_namespace") or {}).get(
                "node"
            ) or {}
            if namespace.get("name", {}).get("value") != "INTERNET":
                errors.append(f"{name}: local address of {edge_device} is not in namespace INTERNET")
            elif namespace["namespace_type"]["value"] != "internet":
                errors.append(f"{name}: namespace INTERNET has type {namespace['namespace_type']['value']}")

        transit_edges = set(ALL_DEMO_INTERNET_TRANSIT_PEERINGS.values())
        if transit_edges != ALL_DEMO_INTERNET_BGWS:
            errors.append(f"transit edges {sorted(transit_edges)} are not exactly the BGWs")

        # Each DC's DCI underlay sessions land on BOTH BGWs of the metro it fronts, and on no other edge.
        dci = await async_client_main.execute_graphql(
            query=SESSIONS_QUERY,
            variables={"names": [f"{circuit}-underlay" for circuit in ALL_DEMO_DCI_CIRCUITS]},
            branch_name=scenario_branch,
        )
        landing: dict[str, set[str]] = {}
        for edge in dci["ManagedBGPPeering"]["edges"]:
            node = edge["node"]
            circuit = node["name"]["value"].removesuffix("-underlay")
            devices = {i["node"]["device"]["node"]["name"]["value"] for i in node["capabilities"]["edges"]}
            landing.setdefault(ALL_DEMO_DCI_CIRCUITS[circuit][0], set()).update(
                device for device in devices if device.startswith("eg-")
            )
        for dc_name, upstream in sorted(ALL_DEMO_INTERNET_UPSTREAM_BY_DC.items()):
            if landing.get(dc_name) != set(upstream):
                errors.append(
                    f"{dc_name}: DCI lands on {sorted(landing.get(dc_name, ()))}, expected {sorted(upstream)}"
                )
        all_upstream = set().union(*ALL_DEMO_INTERNET_UPSTREAM_BY_DC.values())
        if all_upstream != ALL_DEMO_INTERNET_BGWS:
            errors.append("the upstream BGWs are not exactly ALL_DEMO_INTERNET_BGWS")

        assert not errors, f"30_all INTERNET upstream is wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("INTERNET upstream verified: %s", ALL_DEMO_INTERNET_UPSTREAM_BY_DC)
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(411)
    @pytest.mark.dependency(scope="session", depends=["all_demo_carrier_circuits"])
    @pytest.mark.asyncio
    async def test_08_verify_zone_edge_pair(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each zone with exactly two physical edges (FR2, PA4) links and peers them.

        Per zone: one DcimCable between Ethernet1/8 of both edges, both ports
        role `interlink` and addressed from one /127 inside the zone slice of
        Technical-IPv6; exactly one FABRIC-<a>-<b> iBGP session (ttl 1, role
        fabric, IPv6 unicast + l2vpn evpn) on both overlay processes, keyed
        with the zone's own key; and one shared Multi-Site VIP (a single
        IpamIPAddress on loopback100 of both edges).
        """
        logging.info("=== %s - Step 8: Metro edge pair ===", SCENARIO_NAME)

        client = async_client_main
        errors: list[str] = []
        sessions_result = await client.execute_graphql(
            query=SESSIONS_QUERY,
            variables={"names": sorted(ALL_DEMO_FABRIC_PEERINGS.values())},
            branch_name=scenario_branch,
        )
        sessions = {
            edge["node"]["name"]["value"]: edge["node"] for edge in sessions_result["ManagedBGPPeering"]["edges"]
        }
        all_fabric = await client.execute_graphql(
            query='query { ManagedBGPPeering(peering_role__value: "fabric") { count } }', branch_name=scenario_branch
        )
        fabric_count = all_fabric["ManagedBGPPeering"]["count"]
        if fabric_count != len(ALL_DEMO_FABRIC_PEERINGS):
            errors.append(f"{fabric_count} fabric session(s), expected {len(ALL_DEMO_FABRIC_PEERINGS)}")

        for zone, ends in sorted(ALL_DEMO_EDGE_INTERLINKS.items()):
            devices = [device for device, _ in ends]
            ports = await client.execute_graphql(
                query=EDGE_INTERLINK_INTERFACES_QUERY,
                variables={"devices": devices, "name": ALL_DEMO_EDGE_INTERLINK_INTERFACE},
                branch_name=scenario_branch,
            )
            by_device = {
                e["node"]["device"]["node"]["name"]["value"]: e["node"] for e in ports["DcimPhysicalInterface"]["edges"]
            }
            if sorted(by_device) != sorted(devices):
                errors.append(f"{zone}: {ALL_DEMO_EDGE_INTERLINK_INTERFACE} on {sorted(by_device)}, expected {devices}")
                continue
            for device, node in sorted(by_device.items()):
                if node["role"]["value"] != "interlink":
                    errors.append(f"{zone}/{device}: {node['name']['value']} has role {node['role']['value']}")

            port_ids = {node["id"] for node in by_device.values()}
            cable_result = await client.execute_graphql(
                query=EDGE_INTERLINK_CABLES_QUERY, variables={"ids": sorted(port_ids)}, branch_name=scenario_branch
            )
            cables = cable_result["DcimCable"]["edges"]
            if len(cables) != 1:
                errors.append(f"{zone}: {len(cables)} cable(s) on the interlink ports, expected exactly 1")
            elif {e["node"]["id"] for e in cables[0]["node"]["endpoints"]["edges"]} != port_ids:
                errors.append(f"{zone}: the cable does not join exactly the two interlink ports")

            pool_name = ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE.format(zone=zone)
            pool = await client.execute_graphql(
                query=EDGE_PAIR_POOL_QUERY, variables={"name": pool_name}, branch_name=scenario_branch
            )
            pool_edges = pool["CoreIPPrefixPool"]["edges"]
            slices = [
                ipaddress.IPv6Network(r["node"]["prefix"]["value"])
                for pool_edge in pool_edges
                for r in pool_edge["node"]["resources"]["edges"]
                if r["node"].get("prefix")
            ]
            if len(pool_edges) != 1 or not slices:
                errors.append(f"{zone}: pool {pool_name} missing or without a prefix ({len(pool_edges)} pool(s))")
            addresses = [
                ipaddress.IPv6Interface(node["ip_address"]["node"]["address"]["value"])
                for node in by_device.values()
                if node.get("ip_address")
            ]
            if len(addresses) != 2 or addresses[0].network != addresses[1].network:
                errors.append(f"{zone}: interlink addresses {addresses} are not one IPv6 P2P")
            elif addresses[0].network.prefixlen != 127 or not any(addresses[0].network.subnet_of(s) for s in slices):
                errors.append(f"{zone}: interlink {addresses[0].network} is not a /127 inside {pool_name} {slices}")

            name = ALL_DEMO_FABRIC_PEERINGS[zone]
            session = sessions.get(name)
            if session is None:
                errors.append(f"{zone}: no {name} session")
            else:
                if session["peering_role"]["value"] != "fabric" or session["session_type"]["value"] != "IBGP":
                    errors.append(f"{name}: {session['peering_role']['value']}/{session['session_type']['value']}")
                if session["ttl"]["value"] != 1:
                    errors.append(f"{name}: ttl {session['ttl']['value']}, expected 1 (directly connected)")
                key = ((session.get("password") or {}).get("node") or {}).get("name", {}).get("value")
                if key != f"{zone}-fabric-key":
                    errors.append(f"{name}: keyed with '{key}', expected '{zone}-fabric-key'")
                families = sorted(
                    (e["node"]["afi"]["value"], e["node"]["safi"]["value"])
                    for e in session["address_families"]["edges"]
                )
                if families != [("ipv6", "unicast"), ("l2vpn", "evpn")]:
                    errors.append(f"{name}: address families {families}")
                processes = sorted(
                    (e["node"]["name"]["value"], e["node"]["process_role"]["value"])
                    for e in session["bgp_processes"]["edges"]
                )
                if processes != sorted((f"{device}-bgp-overlay", "overlay") for device in devices):
                    errors.append(f"{name}: runs on {processes}")
                session_ports = sorted(
                    (e["node"]["device"]["node"]["name"]["value"], e["node"]["name"]["value"])
                    for e in session["capabilities"]["edges"]
                )
                if session_ports != sorted(ends):
                    errors.append(f"{name}: on interfaces {session_ports}, expected {sorted(ends)}")

            # Shared Multi-Site VIP: one address object, referenced by loopback100 of both edges.
            vip_result = await client.execute_graphql(
                query=EDGE_PAIR_VIP_QUERY, variables={"devices": devices}, branch_name=scenario_branch
            )
            vips = {
                e["node"]["device"]["node"]["name"]["value"]: e["node"]
                for e in vip_result["DcimVirtualInterface"]["edges"]
            }
            if sorted(vips) != sorted(devices):
                errors.append(f"{zone}: loopback100 on {sorted(vips)}, expected {devices}")
                continue
            vip_ids = {((node.get("ip_address") or {}).get("node") or {}).get("id") for node in vips.values()}
            if len(vip_ids) != 1 or None in vip_ids:
                errors.append(f"{zone}: loopback100 of {devices} reference {len(vip_ids)} address object(s), not 1")
            if any(node["role"]["value"] != "multisite-vip" for node in vips.values()):
                errors.append(f"{zone}: loopback100 is not role multisite-vip on every edge")
            vip_address = ALL_DEMO_ZONE_SITE_VIPS[zone]
            count = await client.execute_graphql(
                query="query ($a: String) { IpamIPAddress(address__value: $a) { count } }",
                variables={"a": vip_address},
                branch_name=scenario_branch,
            )
            if count["IpamIPAddress"]["count"] != 1:
                errors.append(f"{zone}: {count['IpamIPAddress']['count']} IpamIPAddress for {vip_address}, expected 1")

        assert not errors, f"30_all zone edge pairs are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Zone edge pairs verified: %s", sorted(ALL_DEMO_EDGE_INTERLINKS))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)

    @pytest.mark.order(411)
    @pytest.mark.dependency(scope="session", depends=["all_demo_carrier_circuits"])
    @pytest.mark.asyncio
    async def test_09_verify_office_wan_exit(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each office with a fabric and an SD-WAN edge exits through it (add_office_fabric).

        Per distribution-switch: one WAN-EXIT-<switch>-<edge> eBGP session (role regular, ttl 1, ipv4 unicast, keyed
        with the office's wan-exit key) between a regular BGP process on the switch and one on the edge, over two
        dot1q sub-interfaces <port>.<tag> in the office's tenant VRF (the tag is the VRF's wan_exit_vlan) addressed
        from one /31. The switches of an office share one AS, the edge has another, both from the 32-bit private pool.
        """
        logging.info("=== %s - Step 9: Office WAN exit ===", SCENARIO_NAME)

        errors: list[str] = []
        for office, switches in sorted(ALL_DEMO_OFFICE_WAN_EXITS.items()):
            edge = f"{office}-EDGE1"
            namespace = OFFICE_NAMESPACE
            tag = wan_exit_vlan(transit_namespace_type(namespace))
            expected = {f"WAN-EXIT-{switch}-{edge}": switch for switch in switches}
            result = await async_client_main.execute_graphql(
                query=WAN_EXIT_SESSIONS_QUERY, variables={"names": sorted(expected)}, branch_name=scenario_branch
            )
            sessions = {e["node"]["name"]["value"]: e["node"] for e in result["ManagedBGPPeering"]["edges"]}
            if sorted(sessions) != sorted(expected):
                errors.append(f"{office}: WAN exit sessions {sorted(sessions)}, expected {sorted(expected)}")

            switch_asns: set[int] = set()
            edge_asns: set[int] = set()
            for name, switch in sorted(expected.items()):
                session = sessions.get(name)
                if session is None:
                    continue
                if (session["peering_role"]["value"], session["session_type"]["value"]) != ("regular", "EBGP"):
                    errors.append(f"{name}: {session['peering_role']['value']}/{session['session_type']['value']}")
                if session["ttl"]["value"] != 1:
                    errors.append(f"{name}: ttl {session['ttl']['value']}, expected 1 (directly connected)")
                key = ((session.get("password") or {}).get("node") or {}).get("name", {}).get("value")
                if key != f"{office.lower()}-wan-exit-key":
                    errors.append(f"{name}: keyed with '{key}', expected '{office.lower()}-wan-exit-key'")
                families = sorted(
                    (e["node"]["afi"]["value"], e["node"]["safi"]["value"])
                    for e in session["address_families"]["edges"]
                )
                if families != [("ipv4", "unicast")]:
                    errors.append(f"{name}: address families {families}")
                processes = {e["node"]["name"]["value"]: e["node"] for e in session["bgp_processes"]["edges"]}
                if sorted(processes) != sorted([f"{switch}-bgp-wan-exit", f"{edge}-bgp-wan-exit"]):
                    errors.append(f"{name}: runs on {sorted(processes)}")
                else:
                    if {p["process_role"]["value"] for p in processes.values()} != {"regular"}:
                        errors.append(
                            f"{name}: process roles {[p['process_role']['value'] for p in processes.values()]}"
                        )
                    switch_asns.add(processes[f"{switch}-bgp-wan-exit"]["local_as"]["node"]["asn"]["value"])
                    edge_asns.add(processes[f"{edge}-bgp-wan-exit"]["local_as"]["node"]["asn"]["value"])
                ends = {
                    e["node"]["device"]["node"]["name"]["value"]: e["node"] for e in session["capabilities"]["edges"]
                }
                if sorted(ends) != sorted([switch, edge]):
                    errors.append(f"{name}: on devices {sorted(ends)}, expected {sorted([switch, edge])}")
                    continue
                networks = set()
                for device, end in sorted(ends.items()):
                    parent = ((end.get("parent_interface") or {}).get("node") or {}).get("name", {}).get("value")
                    if not parent or end["name"]["value"] != f"{parent}.{tag}":
                        errors.append(f"{name}: {device} interface {end['name']['value']}, expected <port>.{tag}")
                    ip = ((end.get("ip_address") or {}).get("node")) or {}
                    if ((ip.get("ip_namespace") or {}).get("node") or {}).get("name", {}).get("value") != namespace:
                        errors.append(f"{name}: {device} address is not in {namespace}")
                    if ip.get("address"):
                        networks.add(ipaddress.IPv4Interface(ip["address"]["value"]).network)
                if len(networks) != 1 or next(iter(networks)).prefixlen != 31:
                    errors.append(f"{name}: the two ends are not one IPv4 /31 ({sorted(map(str, networks))})")
            if len(switch_asns) > 1:
                errors.append(f"{office}: the switches use several AS numbers {sorted(switch_asns)}, expected one")
            if switch_asns & edge_asns or any(asn < 4200000000 for asn in switch_asns | edge_asns):
                errors.append(f"{office}: AS numbers {sorted(switch_asns)} / {sorted(edge_asns)} (edge)")

        assert not errors, f"30_all office WAN exits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Office WAN exits verified: %s", sorted(ALL_DEMO_OFFICE_WAN_EXITS))
        logging.info("=== %s - COMPLETED ===", SCENARIO_NAME)
