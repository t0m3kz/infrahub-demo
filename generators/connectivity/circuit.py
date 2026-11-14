"""add_circuit: the derived facility locations of a TopologyCarrierCircuit (see AGENTS.md Circuit placement). Adds
derived locations with one untracked RelationshipAdd (at most two; a contradiction fails before any write) and converges
the ends' VRF instances. Fires add_virtual_circuit without waiting for session circuits on its ports; it writes only
``locations``, so no loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from generators.helpers.encapsulation import riding_interface_ids
from generators.helpers.vrf import VrfMixin
from generators.mixins.common import CommonGenerator
from generators.mixins.pools import PoolMixin
from generators.protocols import TopologyCarrierCircuit
from utils.circuit_underlay import INTERNET_KIND, session_circuits_on
from utils.data_cleaning import clean_data
from utils.limits import CIRCUIT_ENDS

CARRIER_CIRCUIT_GENERATOR = "add_circuit"
VIRTUAL_CIRCUIT_GENERATOR = "add_virtual_circuit"

# Deployments that are themselves the facility a circuit end terminates on (zone, virtual colocation); not an office
# (sdwan_edge.py authors those locations).
_DERIVABLE_FACILITY_KINDS = frozenset(
    {
        "TopologyDataCenter",
        "TopologyCloudRegion",
        "TopologySaasRegion",
        "TopologyColocationZone",
        "TopologyVirtualColocation",
        INTERNET_KIND,
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
    # The device's deployment; for a cloud transport its region or SaaS region; for a provider connect its location; for
    # THE INTERNET the device itself. None when it has none.
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
            deployment=CircuitLocation.parse(
                device
                if device.get("typename") == INTERNET_KIND
                else device.get("deployment")
                or device.get("region")
                or device.get("saas_region")
                or device.get("location")
            ),
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
    """(derived locations the circuit still lacks, problem or None); a current location of a kind some end derives but
    not itself is a contradiction.
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
    if len(placed) > CIRCUIT_ENDS:
        labels = ", ".join(location.label for location in placed) or "none"
        return [], (
            f"needs at most 2 distinct locations (authored, plus a DC, cloud region or colocation derived from an "
            f"endpoint device's deployment; none for a provider's router), found {len(placed)}: {labels}"
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
        # Authored INTERNET addresses of a cage edge are loaded just before the circuit; this run converges the VRF
        # instances
        await self.reconcile_vrfs({endpoint.device_id for endpoint in endpoints if endpoint.device_id})
        await self._fan_out_sessions(endpoints, circuit_name)
        self.logger.info(f"Carrier circuit {circuit_name} — completed")

    async def _add_locations(self, circuit_id: str, circuit_name: str, locations: list[CircuitLocation]) -> None:
        """Add the derived locations the circuit lacks: one untracked RelationshipAdd, never dropping an authored one."""
        if not locations:
            return
        node = await self.client.get(kind=TopologyCarrierCircuit, id=circuit_id)
        await node.add_relationships(relation_to_update="locations", related_nodes=[loc.id for loc in locations])
        self.logger.info(f"  {circuit_name}: derived location(s) {', '.join(loc.label for loc in locations)}")

    async def _fan_out_sessions(self, endpoints: list[CircuitEndpoint], circuit_name: str) -> None:
        """Re-run add_virtual_circuit, without waiting, for every session circuit on this carrier's ports or their
        sub-interfaces.
        """
        port_ids = [endpoint.interface_id for endpoint in endpoints]
        if not port_ids:
            return
        end_ids = [*port_ids, *await riding_interface_ids(self.client, port_ids)]
        session_ids = {vc.id for vc in await session_circuits_on(self.client, end_ids)}
        if session_ids:
            self.logger.info(f"  {circuit_name} carries {len(session_ids)} session circuit(s)")
            await self.run_generator(VIRTUAL_CIRCUIT_GENERATOR, sorted(session_ids), wait=False)
