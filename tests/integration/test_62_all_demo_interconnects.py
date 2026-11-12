"""Integration test — the 30_all interconnect and tenant-service layer."""

import logging

import pytest
from infrahub_sdk import InfrahubClient

from generators.helpers.naming import sdwan_edge_name
from tasks import ALL_DEMO_BRANCH
from utils.data_cleaning import clean_data
from utils.limits import CIRCUIT_ENDS

from .all_demo_circuit_constants import (
    ALL_DEMO_CARRIER_CIRCUIT_TYPES,
    ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS,
    ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS,
    ALL_DEMO_FACILITY_KINDS,
    ALL_DEMO_GATEWAY_ACCESS_CIRCUITS,
    ALL_DEMO_GATEWAY_ACCESS_LOCATIONS,
    ALL_DEMO_INTERNET_BUS,
    ALL_DEMO_ISP_ACCESS_DEVICE,
    ALL_DEMO_ISP_ACCESS_PORTS,
    ALL_DEMO_SDWAN_EDGE_UPLINK,
    ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS,
    ALL_DEMO_SHARED_INTERNET_CIRCUITS,
    HANDOFF_TYPE,
    INTERCONNECT_TYPE,
    TUNNEL_TYPE,
)
from .circuit_helpers import fetch_interconnect_inventory
from .conftest import TestInfrahubDockerWithClient
from .interconnect_queries import CIRCUIT_LOCATIONS_QUERY, GROUP_MEMBERS_QUERY, SCENARIO_NAME
from .interconnect_rules import (
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
from .wait_helpers import wait_for_condition

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
    @pytest.mark.dependency(scope="session", name="all_demo_carrier_circuits", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_01_verify_carrier_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify the physical underlay: type mix, provider, placement and two distinct ends."""
        logging.info("=== %s - Step 1: Carrier Circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)
        physical = inventory["physical"]

        errors: list[str] = []

        by_type: dict[str, int] = {}
        for circuit in physical:
            by_type[circuit["circuit_type"]] = by_type.get(circuit["circuit_type"], 0) + 1

        for circuit_type, expected in sorted(ALL_DEMO_CARRIER_CIRCUIT_TYPES.items()):
            actual = by_type.get(circuit_type, 0)
            if actual != expected:
                errors.append(f"circuit_type '{circuit_type}': {actual} circuit(s), expected {expected}")
        unexpected = sorted(set(by_type) - set(ALL_DEMO_CARRIER_CIRCUIT_TYPES))
        if unexpected:
            errors.append(f"unexpected circuit type(s): {unexpected}")

        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            if not circuit["provider"]:
                errors.append(f"{circuit_id}: no provider")
            # The shared circuits (dark fibre, cross-connects, ISP transit, the gateway's own access) are deliberately
            # unowned; only a customer's own internet underlay names an owner.
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

        assert not errors, f"30_all carrier circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        logging.info("Carrier circuits verified: %d (%s)", len(physical), dict(sorted(by_type.items())))

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
                if end == ALL_DEMO_INTERNET_BUS:
                    continue
                if end in circuit_of_port:
                    errors.append(f"{end} is an end of both {circuit_of_port[end]} and {circuit_id}")
                circuit_of_port[end] = circuit_id
        return errors

    @staticmethod
    def _placement_errors(physical: list[dict]) -> list[str]:
        """At most two distinct facilities, one per end that has a place (an on-ramp edge: its zone)."""
        errors: list[str] = []
        for circuit in sorted(physical, key=lambda c: c["circuit_id"]):
            circuit_id = circuit["circuit_id"]
            locations = circuit["location_nodes"]
            location_ids = {location["id"] for location in locations}
            if len(location_ids) != len(locations) or len(locations) > 2:
                errors.append(
                    f"{circuit_id}: terminates at {circuit['locations']}, expected at most 2 distinct locations"
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
            if len(devices) != 2:
                errors.append(f"{circuit_id}: end devices {_deployments(circuit)}, expected 2")
                continue
            # Exact, like checks/circuit_placement.py
            placed = {device["deployment_id"] for device in devices if device["deployment_id"]}
            if placed != location_ids:
                errors.append(
                    f"{circuit_id}: locations {[_label(location) for location in locations]} are not where its ends "
                    f"sit {_deployments(circuit)} (each placed end's deployment)"
                )
        return errors

    @staticmethod
    def _derived_location_errors(physical: list[dict]) -> list[str]:
        """The data authors only what is not derivable; add_circuit wrote exactly the derived ends."""
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
            # A circuit with no end in a cage has nothing but derivable facilities
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
        """INET-<office>: the office edge's uplink to its own access-PE port, in the office and the ISP."""
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
            expected_locations = [office]
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
    @pytest.mark.dependency(scope="session", name="all_demo_virtual_circuits", depends=["all_demo_carrier_circuits"])
    @pytest.mark.asyncio
    async def test_02_verify_virtual_circuits(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """Verify each virtual circuit's kind, owner, locations, ends, derived carrier and placement."""
        logging.info("=== %s - Step 2: Virtual Circuits ===", SCENARIO_NAME)

        inventory = await fetch_interconnect_inventory(client=async_client_main, branch=scenario_branch)

        errors = _expectation_errors(inventory["virtual"])
        errors.extend(self._virtual_placement_errors(inventory["virtual"], inventory["physical"]))

        assert not errors, f"30_all virtual circuits are wrong on branch '{scenario_branch}':\n" + "\n".join(
            f"  - {e}" for e in errors
        )

        kinds: dict[str, int] = {}
        for circuit in inventory["virtual"]:
            kinds[circuit["circuit_type"]] = kinds.get(circuit["circuit_type"], 0) + 1
        logging.info("Virtual circuits verified: %d %s", len(inventory["virtual"]), dict(sorted(kinds.items())))

    @staticmethod
    def _virtual_placement_errors(virtual: list[dict], physical: list[dict]) -> list[str]:
        """Where each virtual circuit ends and what carries it, per kind, then across circuits."""
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
            allowed = (1, CIRCUIT_ENDS) if circuit["circuit_type"] == HANDOFF_TYPE else (CIRCUIT_ENDS,)
            if len(locations) not in allowed:
                errors.append(
                    f"{name}: terminates at {circuit['locations']}, expected {' or '.join(map(str, allowed))} distinct location(s)"
                )
                continue
            if len({end["id"] for end in circuit["end_nodes"]}) != CIRCUIT_ENDS:
                errors.append(f"{name}: ends {circuit['ends']}, expected exactly {CIRCUIT_ENDS} distinct ports")
                continue
            if circuit["circuit_type"] == TUNNEL_TYPE:
                errors.extend(_tunnel_errors(circuit, physical_by_id))
            elif circuit["circuit_type"] == INTERCONNECT_TYPE:
                errors.extend(_interconnect_errors(circuit, physical_by_id))
            elif circuit["circuit_type"] == HANDOFF_TYPE:
                errors.extend(_handoff_errors(circuit))
            else:
                errors.append(f"{name}: kind '{circuit['circuit_type']}' is none of the four session circuit kinds")
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
        """Verify `virtual_circuits`, add_virtual_circuit's target group, is exactly the session circuits."""
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

        expected = {c["id"] for c in inventory["virtual"]}
        errors = []
        if expected - set(members):
            errors.append(
                f"not in the group: {sorted(c['name'] for c in inventory['virtual'] if c['id'] in expected - set(members))}"
            )
        if set(members) - expected:
            errors.append(f"members that are no session circuit: {sorted(set(members) - expected)}")
        if not expected:
            errors.append("no session circuit on the branch")

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
        """Verify every location's `circuits` lists exactly the circuits that name it, physical and virtual."""
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

    @pytest.mark.order(395)
    @pytest.mark.dependency(scope="session", depends=["all_demo_inventory"])
    @pytest.mark.asyncio
    async def test_02c_verify_patch_panel_pairing(
        self,
        async_client_main: InfrahubClient,
        scenario_branch: str,
    ) -> None:
        """PP-EQX-FR2-01 (type lc-24) has all 24 front ports, each on its rear port N."""
        query = """
        query {
          DcimPatchPanel(panel_id__value: "fr2-s1-r-1:PP-EQX-FR2-01") {
            edges { node { front_ports { edges { node { name { value } rear_port { node { name { value } } } } } } } }
          }
        }
        """

        async def paired() -> tuple[bool, dict[str, str | None]]:
            result = clean_data(await async_client_main.execute_graphql(query=query, branch_name=scenario_branch))
            panels = result.get("DcimPatchPanel") or []
            fronts = (panels[0].get("front_ports") or []) if panels else []
            pairs = {str(front["name"]): (front.get("rear_port") or {}).get("name") for front in fronts}
            return len(pairs) == 24 and all(pairs.values()), pairs

        pairs = await wait_for_condition(paired, max_attempts=30, poll_interval=10, description="patch panel pairing")
        assert pairs == {str(n): str(n) for n in range(1, 25)}
