"""add_circuit: the derived facility locations of a TopologyCarrierCircuit.

A carrier circuit is a pure carrier (fibre, cross-connect, internet access): it has no role and no session. This
generator's one job is its placement. An endpoint device whose ``deployment`` is itself a facility
(TopologyDataCenter, TopologyExternalProviders, TopologyCloudRegion, TopologyColocationZone,
TopologyVirtualColocation) places that end, so the circuit's locations are the authored ones plus these derived
ones, exactly two distinct. An on-ramp edge deploys to its zone or virtual colocation, which is the facility the
circuit ends on. A derived facility that contradicts an authored one of the same kind, or anything but two
locations, fails the run before any write; the missing derived ones are added with one RelationshipAdd, so an
already complete circuit writes nothing.

Derived from the devices as they are when the circuit is created or its ends change: no trigger watches an end
device's ``deployment`` (a rule on a device kind can only dispatch a generator targeting devices, and none targets
every device). Every producer sets it at device creation, before any circuit names the device; a later edit is
reported by checks/circuit_placement.py, and a re-run of this generator adds a deployment that was missing but never
moves a derived location.

The end devices' VRF instances (generators/helpers/vrf.py) are converged as well: a physical port's address in a
non-default namespace (the INTERNET address of a cage edge, authored in data) demands an instance, and this is the
generator that runs on the circuit loaded right after it.

The derived locations are written untracked: the circuit is the run's target, not its output (tracked, the cleanup
could delete it). Nothing else is written.

BGP sessions over a connection between topologies come from the virtual circuits, not from here: the
dci and handoff session circuit generator (generators/topology/virtual_circuit.py) waits for this
generator (``CARRIER_CIRCUIT_GENERATOR``) on the carrier circuits it rides and reads their ends. This generator
never waits for it (no cycle): it only fires it, without waiting, for every session circuit on the circuit's ports
(or on a sub-interface whose parent is one of them).

What reaches a session: it reruns on its own created / capabilities / status (a handoff also ip_namespace and password),
and, through this fan-out, when a carrier circuit on its ports (or on a port's parent) is created, has its ends
changed (capabilities) or its status changed (data/events/99_actions.yml: created, capabilities and status rules on
TopologyCarrierCircuit). It does NOT rerun when a cable is added later, when a port is removed
from a circuit (the circuit no longer lists the port, so the fan-out does not find the session), or when a circuit's
type changes (no rule).

No trigger loop: this generator writes only the derived ``locations`` relationship (one RelationshipAdd), never the
status or the ends that the rules watch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from utils.circuit_underlay import session_circuits_on
from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.vrf import VrfMixin
from ..pools import PoolMixin
from ..protocols import DcimVirtualInterface, TopologyCarrierCircuit

CARRIER_CIRCUIT_GENERATOR = "add_circuit"
VIRTUAL_CIRCUIT_GENERATOR = "add_virtual_circuit"

# Device deployments that are themselves the facility a carrier circuit end
# terminates on, so that end's location is derived from its device. A colocation
# zone or virtual colocation is one: the on-ramp edge deploys to it, not to the
# metro. Not an office (generators/topology/sdwan_edge.py authors its circuit's
# locations); those ends stay authored.
_DERIVABLE_FACILITY_KINDS = frozenset(
    {
        "TopologyDataCenter",
        "TopologyExternalProviders",
        "TopologyCloudRegion",
        "TopologyColocationZone",
        "TopologyVirtualColocation",
    }
)


@dataclass(frozen=True)
class CircuitLocation:
    """A place a circuit terminates on: a circuit location or an endpoint device's deployment."""

    id: str
    kind: str
    label: str

    @classmethod
    def parse(cls, node: Any) -> CircuitLocation | None:
        """From a cleaned node dict (id, typename, display_label); None for an empty slot."""
        if not isinstance(node, dict) or not node.get("id"):
            return None
        return cls(id=node["id"], kind=node.get("typename") or "", label=node.get("display_label") or node["id"])


def circuit_locations(circuit: dict[str, Any]) -> list[CircuitLocation]:
    """The circuit's current (authored or earlier derived) locations, deduplicated by id."""
    locations: dict[str, CircuitLocation] = {}
    for node in circuit.get("locations") or []:
        location = CircuitLocation.parse(node)
        if location is not None:
            locations.setdefault(location.id, location)
    return list(locations.values())


@dataclass(frozen=True)
class CircuitEndpoint:
    """One end of a circuit, parsed from the query's interface selection."""

    interface_id: str
    interface_name: str
    device_name: str
    # The device's deployment, whatever its kind; None when it has none.
    deployment: CircuitLocation | None
    device_id: str = ""

    @property
    def label(self) -> str:
        return f"{self.device_name}/{self.interface_name}"

    @classmethod
    def parse(cls, interface: dict[str, Any] | None) -> CircuitEndpoint | None:
        """Build an endpoint from a cleaned interface dict; None for an empty slot."""
        if not interface or not interface.get("id"):
            return None
        device = interface.get("device") or {}
        return cls(
            interface_id=interface["id"],
            interface_name=interface.get("name") or "?",
            device_name=device.get("name") or "?",
            deployment=CircuitLocation.parse(device.get("deployment")),
            device_id=device.get("id") or "",
        )


def circuit_endpoints(circuit: dict[str, Any]) -> list[CircuitEndpoint]:
    """The circuit's ends, from its one ``capabilities`` edge, in query order and deduplicated by interface id."""
    endpoints: dict[str, CircuitEndpoint] = {}
    for interface in circuit.get("capabilities") or []:
        endpoint = CircuitEndpoint.parse(interface)
        if endpoint is not None:
            endpoints.setdefault(endpoint.interface_id, endpoint)
    return list(endpoints.values())


def plan_locations(
    current: list[CircuitLocation], endpoints: list[CircuitEndpoint]
) -> tuple[list[CircuitLocation], str | None]:
    """(derived locations the circuit still lacks, problem or None).

    An endpoint device whose deployment is a derivable facility places its end.
    A current location of a kind some end derives, but not derived itself
    (DC10 authored while the device sits in DC11), is a contradiction. The
    current and derived locations together must be exactly two distinct ones.
    """
    derived: dict[str, tuple[CircuitLocation, str]] = {}
    for endpoint in endpoints:
        deployment = endpoint.deployment
        if deployment is not None and deployment.kind in _DERIVABLE_FACILITY_KINDS:
            derived.setdefault(deployment.id, (deployment, endpoint.device_name))
    for location in current:
        if location.id in derived:
            continue
        sources = [f"{device} is in {loc.label}" for loc, device in derived.values() if loc.kind == location.kind]
        if sources:
            return [], (
                f"location {location.label} ({location.kind}) contradicts the endpoint devices' deployment: "
                + ", ".join(sources)
            )
    current_ids = {location.id for location in current}
    missing = [location for location, _ in derived.values() if location.id not in current_ids]
    placed = current + missing
    if len(placed) != 2:
        labels = ", ".join(location.label for location in placed) or "none"
        return [], (
            f"needs exactly 2 distinct locations (authored, plus a DC, ISP, cloud region or colocation derived from an "
            f"endpoint device's deployment), found {len(placed)}: {labels}"
        )
    return missing, None


class CarrierCircuitGenerator(PoolMixin, VrfMixin, CommonGenerator):
    """add_circuit — a TopologyCarrierCircuit's derived facility locations."""

    client: Any

    async def generate(self, data: dict[str, Any]) -> None:
        circuits = clean_data(data).get("TopologyCarrierCircuit") or []
        if not circuits:
            self.logger.error("No TopologyCarrierCircuit data in GraphQL response")
            return
        circuit = circuits[0]
        circuit_name: str = circuit.get("circuit_id") or circuit.get("name") or ""
        if not circuit_name:
            self.logger.error("TopologyCarrierCircuit missing circuit_id/name")
            return

        endpoints = circuit_endpoints(circuit)
        summary = " | ".join(e.label for e in endpoints) or "no endpoints"
        self.logger.info(f"Processing carrier circuit {circuit_name}: {summary}")
        missing_locations, problem = plan_locations(circuit_locations(circuit), endpoints)
        if problem:
            self.logger.error(f"{circuit_name} ({summary}): {problem}")
            return

        await self._add_locations(circuit["id"], circuit_name, missing_locations)
        # The INTERNET addresses of a cage edge are authored data, loaded just before the circuit they ride: this run
        # is the one that sees them, so it converges the end devices' VRF instances (nothing else writes them).
        await self.reconcile_vrfs({endpoint.device_id for endpoint in endpoints if endpoint.device_id})
        await self._fan_out_sessions(endpoints, circuit_name)
        self.logger.info(f"Carrier circuit {circuit_name} — completed")

    async def _add_locations(self, circuit_id: str, circuit_name: str, locations: list[CircuitLocation]) -> None:
        """Add the derived locations the circuit lacks: one RelationshipAdd, untracked.

        A RelationshipAdd names only these peers, so it never drops an authored location.
        """
        if not locations:
            return
        node = await self.client.get(kind=TopologyCarrierCircuit, id=circuit_id)
        await node.add_relationships(relation_to_update="locations", related_nodes=[loc.id for loc in locations])
        self.logger.info(f"  {circuit_name}: derived location(s) {', '.join(loc.label for loc in locations)}")

    async def _fan_out_sessions(self, endpoints: list[CircuitEndpoint], circuit_name: str) -> None:
        """Re-run add_virtual_circuit for every session circuit riding this carrier, without waiting.

        A virtual circuit rides this one when it ends on one of its ports or on a sub-interface whose
        ``parent_interface`` is one of them (the derived underlay, utils/circuit_underlay.py). Each of those runs
        waits for this run itself (wait_for_parent_generator_and_refetch), so it sees what this run wrote.

        This run happens when the circuit is created, its ends change or its status changes; a port removed from
        the circuit is no longer in ``endpoints``, so the sessions that sat on it are not reached.
        """
        port_ids = [endpoint.interface_id for endpoint in endpoints]
        if not port_ids:
            return
        sub_interfaces = await self.client.filters(kind=DcimVirtualInterface, parent_interface__ids=port_ids)
        end_ids = [*port_ids, *(sub.id for sub in sub_interfaces)]
        session_ids = {vc.id for vc in await session_circuits_on(self.client, end_ids)}
        if session_ids:
            self.logger.info(f"  {circuit_name} carries {len(session_ids)} session circuit(s)")
            await self.run_generator(VIRTUAL_CIRCUIT_GENERATOR, sorted(session_ids), wait=False)
