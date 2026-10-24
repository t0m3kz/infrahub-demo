"""Integration test — the 30_all interconnect and tenant-service layer.

Everything here is the part of the model that leaves a single site: the dark
fibre and cross-connects into the colocation on-ramps, the virtual circuits
riding them (the DCI between the border gateways, the handoffs into AWS, the
Network Edge chain, Megaport and the SD-WAN gateway), the SD-WAN branches'
tunnels and internet underlay, and the cloud-side terminations that close the
on-prem-to-cloud path.

A physical circuit is a pure carrier. The role of a virtual circuit is its KIND:
a Tunnel (inventory only, no session), an Interconnect (a DCI: an underlay and
an EVPN session) or a Handoff (one eBGP session in one environment namespace).
Their carrier is never declared: it is derived from the circuit's own ports.

Two edges get special attention because the schema deliberately has exactly one
way to express each of them, and a regression would silently split the graph
into islands:

  * ``capabilities`` — how a circuit surfaces on the port it
    terminates on. It is the single uniform "what is this port doing" edge, and
    replaced the bespoke ``interfaces`` relationship the node used to declare.
  * ``cloud_endpoint`` — how a Handoff reaches its cloud side, which is a
    CloudVirtualInterface, not a device port. Without it the traversal walks
    from a border leaf out to the cage and then stops at an island boundary.

Circuit placement is checked from both sides: a physical circuit ends on two
facilities where its end devices sit (the end the data leaves out is the one
add_circuit derived), a tunnel on its owner's office, and every location's
reverse ``circuits`` lists exactly the circuits naming it. The per-kind rules
(_tunnel_errors, _interconnect_errors, _handoff_errors) assert on the live data
what checks/circuit_placement.py enforces, independently of it.

Also covered: the sessions add_virtual_circuit builds (two per DCI, one per
Handoff, none for a Tunnel), the tenant-scoped firewall and load-balancer
contexts (shared vs dedicated, driven by the customer design blueprint), the
VIPs of the load_balanced components with their R_server/R_client rules, and
the segment deployment legs, where a stretch segment must carry one leg per DC.

Runs against the branch test_59 builds; it never loads data of its own.

This module holds the circuit steps (physical, virtual, location circuits). The tenant-scoped services follow in
test_62b_all_demo_tenant_services.py and the colocation, session and internet steps in
test_62c_all_demo_circuit_sessions.py; their helpers are interconnect_queries.py and interconnect_rules.py.
"""

import logging

import pytest
from infrahub_sdk import InfrahubClient

from generators.helpers.naming import sdwan_edge_name
from utils.data_cleaning import clean_data

from .conftest import TestInfrahubDockerWithClient
from .interconnect_queries import CIRCUIT_LOCATIONS_QUERY, GROUP_MEMBERS_QUERY, SCENARIO_NAME
from .interconnect_rules import (
    _CIRCUIT_ENDS,
    _OFFICE_KIND,
    _ZONE_KINDS,
    _deployments,
    _environment_errors,
    _expectation_errors,
    _handoff_errors,
    _interconnect_errors,
    _label,
    _session_port_errors,
    _tunnel_errors,
)
from .test_constants import (
    ALL_DEMO_BRANCH,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_GATEWAY_ACCESS_CIRCUITS,
    ALL_DEMO_GATEWAY_ACCESS_LOCATIONS,
    ALL_DEMO_ISP_ACCESS_DEVICE,
    ALL_DEMO_ISP_ACCESS_LOCATION,
    ALL_DEMO_ISP_ACCESS_PORTS,
    ALL_DEMO_PHYSICAL_CIRCUIT_TYPES,
    ALL_DEMO_SDWAN_EDGE_UPLINK,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
    ALL_DEMO_SHARED_INTERNET_CIRCUITS,
    HANDOFF_KIND,
    INTERCONNECT_KIND,
    TUNNEL_KIND,
)
from .test_helpers import fetch_interconnect_inventory

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class TestAllDemoInterconnects(TestInfrahubDockerWithClient):
    """Verify the circuit layer and the tenant-scoped services on top of it."""

    @pytest.fixture(scope="class")
    def scenario_branch(self) -> str:
        return ALL_DEMO_BRANCH

    # ------------------------------------------------------------------
    # Physical layer
    # ------------------------------------------------------------------

    @pytest.mark.order(394)
    @pytest.mark.dependency(scope="session", name="all_demo_physical_circuits", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_verify_physical_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the physical underlay: type mix, provider, placement and two distinct ends.

        Every circuit is point-to-point, so exactly two locations and exactly
        two distinct ports (its capabilities, min and max count 2) are
        hard invariants, and a port is an end of at most one circuit — the
        SD-WAN offices' INET-<office> circuits, created concurrently, are where a
        shared access port would show. Each location is a facility, and each
        is where one end's device is deployed (_placement_errors); the ends the
        data leaves out are the ones add_circuit derived (_derived_location_errors).
        """
        logging.info("=== %s - Step 1: Physical Circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        physical = inventory["physical"]

        errors: list[str] = []

        by_type: dict[str, int] = {}
        for circuit in physical:
            by_type[circuit["circuit_type"]] = by_type.get(circuit["circuit_type"], 0) + 1

        for circuit_type, expected in sorted(ALL_DEMO_PHYSICAL_CIRCUIT_TYPES.items()):
            actual = by_type.get(circuit_type, 0)
            if actual != expected:
                errors.append(f"circuit_type '{circuit_type}': {actual} circuit(s), expected {expected}")
        unexpected = sorted(set(by_type) - set(ALL_DEMO_PHYSICAL_CIRCUIT_TYPES))
        if unexpected:
            errors.append(f"unexpected circuit type(s): {unexpected}")

        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            if not circuit["provider"]:
                errors.append(f"{circuit_id}: no provider")
            # The shared circuits (dark fibre, cross-connects, ISP transit, the
            # gateway's own access) are deliberately unowned; only a customer's
            # own internet underlay names an owner.
            transit = circuit_id in ALL_DEMO_SHARED_INTERNET_CIRCUITS
            if circuit["circuit_type"] == "internet" and not transit and not circuit["owner"]:
                errors.append(f"{circuit_id}: internet underlay with no owning customer")
            if transit and circuit["owner"]:
                errors.append(f"{circuit_id}: shared transit circuit owned by '{circuit['owner']}'")
            if circuit["circuit_type"] in ("dark_fiber", "cross_connect") and circuit["owner"]:
                errors.append(f"{circuit_id}: shared {circuit['circuit_type']} circuit owned by '{circuit['owner']}'")

        errors.extend(self._port_errors(physical))
        errors.extend(self._placement_errors(physical))
        errors.extend(self._derived_location_errors(physical))
        errors.extend(self._sdwan_underlay_errors(physical))
        errors.extend(self._gateway_access_errors(physical))

        assert not errors, f"30_all physical circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Physical circuits verified: %d (%s)", len(physical), dict(sorted(by_type.items())))

    @staticmethod
    def _port_errors(physical: list[dict]) -> list[str]:
        """Every circuit has two distinct ends, and no port is an end of two circuits."""
        errors: list[str] = []
        circuit_of_port: dict[tuple[str, str], str] = {}
        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            if len(circuit["end_ids"]) != 2 or len(set(circuit["end_ids"])) != 2:
                errors.append(f"{circuit_id}: ends {circuit['ends']}, expected exactly 2 distinct ports")
            for end in circuit["ends"]:
                if end in circuit_of_port:
                    errors.append(f"{end} is an end of both {circuit_of_port[end]} and {circuit_id}")
                circuit_of_port[end] = circuit_id
        return errors

    @staticmethod
    def _placement_errors(physical: list[dict]) -> list[str]:
        """Two distinct facilities, each the deployment of one end's device (an on-ramp edge: its zone).

        A facility is a TopologyFacility kind (DC, colocation zone, cloud region,
        ISP, SaaS region, office); a pod, a metro or a footprint is never one, and an
        office only ends its own internet access. The two ends' devices must account for
        the two locations one-to-one: the border leaf's DC, the PE's ISP, the provider
        router's region, and for the on-ramp edge (deployed to its zone) that zone.
        """
        errors: list[str] = []
        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            locations = circuit["location_nodes"]
            if len({location["id"] for location in locations}) != 2:
                errors.append(
                    f"{circuit_id}: terminates at {circuit['locations']}, expected exactly 2 distinct locations"
                )
                continue
            for location in locations:
                if location["kind"] not in ALL_DEMO_FACILITY_KINDS:
                    errors.append(
                        f"{circuit_id}: ends at {_label(location)}, not a facility {sorted(ALL_DEMO_FACILITY_KINDS)}"
                    )
                elif location["kind"] == _OFFICE_KIND and circuit["circuit_type"] != "internet":
                    errors.append(
                        f"{circuit_id}: a {circuit['circuit_type']} circuit ends at office {location['name']}"
                    )
            devices = circuit["end_devices"]
            if len(devices) != 2 or any(not device["deployment_id"] for device in devices):
                errors.append(f"{circuit_id}: end devices {_deployments(circuit)}, expected 2, each with a deployment")
                continue
            (first, second), (left, right) = locations, devices
            # Exact, like checks/circuit_placement.py's _end_at: a physical circuit's location IS the end device's
            # deployment (an on-ramp edge's zone), never a sibling cage of the same metro.
            if not (
                (first["id"] == left["deployment_id"] and second["id"] == right["deployment_id"])
                or (first["id"] == right["deployment_id"] and second["id"] == left["deployment_id"])
            ):
                errors.append(
                    f"{circuit_id}: locations {[_label(location) for location in locations]} are not where its ends "
                    f"sit {_deployments(circuit)} (each end's deployment)"
                )
        return errors

    @staticmethod
    def _derived_location_errors(physical: list[dict]) -> list[str]:
        """The data authors only what is not derivable; add_circuit wrote exactly the derived ends.

        Derivable: an end device's deployment that is itself a facility (DC, ISP,
        cloud region, colocation zone or virtual colocation). The 30_all data must not
        list one, except the cage it authors (redundant with the edge's zone, which
        add_circuit also derives: an authored zone must be that zone), and the
        locations the branch holds beyond the data's are exactly those. Circuits no data file declares
        (INET-<office>, add_sdwan_edge) are written whole by their generator.
        """
        errors: list[str] = []
        by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        for circuit_id, declared in sorted(ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS.items()):
            circuit = by_id.get(circuit_id)
            if circuit is None:
                errors.append(f"{circuit_id}: declared in 30_all but not on the branch")
                continue
            derivable = {
                str(device["deployment_name"])
                for device in circuit["end_devices"]
                if device["deployment_kind"] in ALL_DEMO_FACILITY_KINDS
            }
            listed = sorted(
                set(declared)
                & {
                    str(device["deployment_name"])
                    for device in circuit["end_devices"]
                    if device["deployment_kind"] in ALL_DEMO_FACILITY_KINDS - _ZONE_KINDS
                }
            )
            # A circuit with no end in a cage has nothing but derivable facilities, and the schema requires one
            # location: authoring exactly one of them is then unavoidable, not redundant.
            no_cage_end = not any(device["deployment_kind"] in _ZONE_KINDS for device in circuit["end_devices"])
            if listed and not (no_cage_end and len(declared) == 1):
                errors.append(f"{circuit_id}: the data lists {listed}, which add_circuit derives from the end devices")
            missing = sorted(set(declared) - set(circuit["locations"]))
            if missing:
                errors.append(f"{circuit_id}: declared location(s) {missing} are not on the branch")
            written = set(circuit["locations"]) - set(declared)
            if written != derivable - set(declared):
                errors.append(
                    f"{circuit_id}: the generator wrote {sorted(written)} beyond the data's {sorted(declared)}, "
                    f"expected the end devices' facility deployments {sorted(derivable - set(declared))}"
                )
        return errors

    @staticmethod
    def _sdwan_underlay_errors(physical: list[dict]) -> list[str]:
        """INET-<office>: the office edge's uplink to its own access-PE port, in the office and the ISP.

        The offices board in one load and add_sdwan_edge picks the first free
        port of the access PE under a lock, so the five circuits together
        hold exactly xe-0/1/[0-4].
        """
        errors: list[str] = []
        by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        isp_ports: list[str] = []
        for circuit_id, office in sorted(ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS.items()):
            circuit = by_id.get(circuit_id)
            if circuit is None:
                errors.append(f"{circuit_id}: SD-WAN internet underlay of {office} was not created")
                continue
            if (sdwan_edge_name(office), ALL_DEMO_SDWAN_EDGE_UPLINK) not in circuit["ends"]:
                errors.append(
                    f"{circuit_id}: ends {circuit['ends']}, expected {sdwan_edge_name(office)} {ALL_DEMO_SDWAN_EDGE_UPLINK}"
                )
            isp_ends = [port for device, port in circuit["ends"] if device == ALL_DEMO_ISP_ACCESS_DEVICE]
            if len(isp_ends) != 1:
                errors.append(f"{circuit_id}: {len(isp_ends)} end(s) on {ALL_DEMO_ISP_ACCESS_DEVICE}, expected 1")
            isp_ports.extend(isp_ends)
            expected_locations = sorted([office, ALL_DEMO_ISP_ACCESS_LOCATION])
            if circuit["locations"] != expected_locations:
                errors.append(f"{circuit_id}: locations {circuit['locations']}, expected {expected_locations}")
        if set(isp_ports) != ALL_DEMO_ISP_ACCESS_PORTS or len(isp_ports) != len(ALL_DEMO_ISP_ACCESS_PORTS):
            errors.append(
                f"{ALL_DEMO_ISP_ACCESS_DEVICE} ports {sorted(isp_ports)}, "
                f"expected one circuit on each of {sorted(ALL_DEMO_ISP_ACCESS_PORTS)}"
            )
        return errors

    @staticmethod
    def _gateway_access_errors(physical: list[dict]) -> list[str]:
        """The shared gateway's own access: its eth1 to the ISP edge port, in the cage and the ISP, unowned."""
        errors: list[str] = []
        by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        for circuit_id, ends in sorted(ALL_DEMO_GATEWAY_ACCESS_CIRCUITS.items()):
            circuit = by_id.get(circuit_id)
            if circuit is None:
                errors.append(f"{circuit_id}: the gateway's internet access was not created")
                continue
            if set(circuit["ends"]) != ends:
                errors.append(f"{circuit_id}: ends {circuit['ends']}, expected {sorted(ends)}")
            if circuit["locations"] != sorted(ALL_DEMO_GATEWAY_ACCESS_LOCATIONS):
                errors.append(
                    f"{circuit_id}: locations {circuit['locations']}, expected {sorted(ALL_DEMO_GATEWAY_ACCESS_LOCATIONS)}"
                )
        return errors

    # ------------------------------------------------------------------
    # Virtual layer
    # ------------------------------------------------------------------

    @pytest.mark.order(395)
    @pytest.mark.dependency(scope="session", name="all_demo_virtual_circuits", depends=["all_demo_physical_circuits"])
    @pytest.mark.asyncio
    async def test_02_verify_virtual_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each virtual circuit's kind, owner, locations, ends, derived carrier and placement.

        The role of a virtual circuit is its kind (Tunnel, Interconnect, Handoff). The carrier is derived, never
        declared: the physical circuits on the circuit's own ports (or on the parent of a sub-interface), compared
        with ALL_DEMO_VIRTUAL_CIRCUITS. The per-kind rules (_virtual_placement_errors) hold on the live data.
        """
        logging.info("=== %s - Step 2: Virtual Circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)

        errors = _expectation_errors(inventory["virtual"])
        errors.extend(self._virtual_placement_errors(inventory["virtual"], inventory["physical"]))

        assert not errors, f"30_all virtual circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        kinds: dict[str, int] = {}
        for circuit in inventory["virtual"]:
            kinds[circuit["kind"]] = kinds.get(circuit["kind"], 0) + 1
        logging.info("Virtual circuits verified: %d %s", len(inventory["virtual"]), dict(sorted(kinds.items())))

    @staticmethod
    def _virtual_placement_errors(virtual: list[dict], physical: list[dict]) -> list[str]:
        """Where each virtual circuit ends and what carries it, per kind, then across circuits.

        Every circuit has two distinct ports, and the locations the data authors are exactly the ones on the branch
        (add_sdwan_edge writes a tunnel's own). Then the rules of its kind (_tunnel_errors, _interconnect_errors,
        _handoff_errors): the same ones checks/circuit_placement.py enforces, asserted here on the live data. Across
        circuits: one port, one session (_session_port_errors), and prod never meets non-prod at a device
        (_environment_errors).
        """
        errors: list[str] = []
        physical_by_id = {circuit["circuit_id"]: circuit for circuit in physical}
        for circuit in sorted(virtual, key=lambda c: c["name"]):
            name = circuit["name"]
            if name in ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS and circuit["locations"] != sorted(
                ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[name]
            ):
                errors.append(
                    f"{name}: locations {circuit['locations']}, declared "
                    f"{sorted(ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS[name])} (a virtual circuit derives none)"
                )
            locations = {location["id"] for location in circuit["location_nodes"]}
            allowed = (1, _CIRCUIT_ENDS) if circuit["kind"] == HANDOFF_KIND else (_CIRCUIT_ENDS,)
            if len(locations) not in allowed:
                errors.append(
                    f"{name}: terminates at {circuit['locations']}, expected {' or '.join(map(str, allowed))} distinct location(s)"
                )
                continue
            if len({end["id"] for end in circuit["end_nodes"]}) != _CIRCUIT_ENDS:
                errors.append(f"{name}: ends {circuit['ends']}, expected exactly {_CIRCUIT_ENDS} distinct ports")
                continue
            if circuit["kind"] == TUNNEL_KIND:
                errors.extend(_tunnel_errors(circuit, physical_by_id))
            elif circuit["kind"] == INTERCONNECT_KIND:
                errors.extend(_interconnect_errors(circuit, physical_by_id))
            elif circuit["kind"] == HANDOFF_KIND:
                errors.extend(_handoff_errors(circuit))
            else:
                errors.append(f"{name}: kind '{circuit['kind']}' is none of the three virtual circuit kinds")
        errors.extend(_session_port_errors(virtual))
        errors.extend(_environment_errors(virtual))
        return errors

    @pytest.mark.order(395)
    @pytest.mark.dependency(scope="session", depends=["all_demo_virtual_circuits"])
    @pytest.mark.asyncio
    async def test_02a_verify_virtual_circuits_group(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify `virtual_circuits`, add_virtual_circuit's target group, is exactly the Interconnect and Handoff
        circuits: a Tunnel has no session (the SD-WAN orchestrator owns its overlay routing), so it is not a member."""
        logging.info("=== %s - Step 2a: virtual_circuits group ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        result = clean_data(
            await async_client_main.execute_graphql(
                query=GROUP_MEMBERS_QUERY, variables={"name": "virtual_circuits"}, branch_name=scenario_branch
            )
        )
        groups = result["CoreGroup"]
        assert groups, "group 'virtual_circuits' not found"
        members = {str(member["id"]): str(member["typename"]) for member in groups[0]["members"] or []}

        session_kinds = {INTERCONNECT_KIND, HANDOFF_KIND}
        expected = {c["id"] for c in inventory["virtual"] if c["kind"] in session_kinds}
        tunnels = {c["id"] for c in inventory["virtual"] if c["kind"] == TUNNEL_KIND}
        errors = []
        if expected - set(members):
            errors.append(
                f"not in the group: {sorted(c['name'] for c in inventory['virtual'] if c['id'] in expected - set(members))}"
            )
        if set(members) - expected:
            errors.append(f"members that are no interconnect or handoff circuit: {sorted(set(members) - expected)}")
        if tunnels & set(members):
            errors.append("a tunnel circuit is in the group, it has no session to generate")
        if not expected:
            errors.append("no interconnect or handoff circuit on the branch")

        assert not errors, f"virtual_circuits group is wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

    @pytest.mark.order(395)
    @pytest.mark.dependency(scope="session", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_02b_verify_location_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify every location's `circuits` lists exactly the circuits that name it, physical and virtual.

        `circuits` is the reverse of a circuit's `locations` (one identifier,
        circuit__location), so the two directions must agree for every location on
        the branch: a circuit missing from its location's list, or a location listing
        a circuit that does not name it, is a schema or generator write that took
        only one side.
        """
        logging.info("=== %s - Step 2b: Location circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        result = await async_client_main.execute_graphql(query=CIRCUIT_LOCATIONS_QUERY, branch_name=scenario_branch)
        cleaned = clean_data(result)

        names: dict[str, str] = {}
        forward: dict[str, set[str]] = {}  # location id -> the circuits naming it
        for circuit in [*inventory["physical"], *inventory["virtual"]]:
            names[circuit["id"]] = circuit.get("circuit_id") or circuit["name"]
            for location in circuit["location_nodes"]:
                forward.setdefault(location["id"], set()).add(circuit["id"])
        reverse: dict[str, set[str]] = {}  # location id -> its `circuits`
        location_names: dict[str, str] = {}
        for location in cleaned.get("TopologyConnectableLocation") or []:
            location_names[str(location["id"])] = f"{location['name']} ({location['typename']})"
            reverse[str(location["id"])] = {str(circuit["id"]) for circuit in location.get("circuits") or []}

        errors: list[str] = []
        for location_id in sorted(set(forward) | set(reverse), key=lambda i: location_names.get(i, i)):
            named_by, listed = forward.get(location_id, set()), reverse.get(location_id, set())
            if named_by != listed:
                label = location_names.get(location_id, location_id)
                errors.append(
                    f"{label}: `circuits` lacks {sorted(names.get(c, c) for c in named_by - listed)} and lists "
                    f"{sorted(names.get(c, c) for c in listed - named_by)} that do not name it"
                )
        if not forward:
            errors.append("no circuit names any location")

        assert not errors, f"30_all location circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logging.info("Location circuits verified: %d location(s) with circuits", len(forward))
