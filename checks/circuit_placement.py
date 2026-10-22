"""Validate where a circuit terminates: its locations against its end devices, its owner and its underlay.

The schema holds the kinds (a physical circuit's ``locations`` peers TopologyFacility); what it cannot hold is
held here, on every circuit at once:

Physical circuits
    1. exactly two distinct locations (the authored cage plus the ends add_circuit derives),
    2. each location is the ``deployment`` of one end's device, one end per location: a DC, ISP or cloud region,
       or for colocation kit the zone (or virtual colocation) the device is deployed to. A sibling zone of the
       same metro is not the end: add_circuit (``plan_locations``) refuses an authored location that differs from
       the end device's deployment. The metro is no facility and no device is deployed to it,
    3. owned exactly when it is an office's access circuit: one ending on a TopologyCustomerOffice is owned by
       that office's owner (add_sdwan_edge's INET-<office>); every other one is shared underlay, unowned.

Virtual circuits
    1. exactly two distinct locations,
    2. owned: one end is a footprint of the circuit's owner with an end device in its facility (a DC, the
       colocation metro, the cloud region; an office is its own facility); the other end is a provider facility
       (TopologyCloudRegion, TopologySaasRegion, TopologyColocationZone, TopologyVirtualColocation) or another
       footprint,
    3. unowned (platform overlay): facilities at both ends, no customer footprint,
    4. ``physical_circuits`` form one contiguous path, each hop sharing a facility with the next: end to end
       for a physical- or provider-backed circuit, from one end to the internet (a TopologyExternalProviders)
       for an internet-backed one. A physical_backed circuit rides at least one. Relationship peers carry no
       order, so the path is searched over every order of the hops.

A colocation footprint is parented by the METRO, not a cage, so a zone or virtual colocation of that metro
reaches it: the chain of VC-C001-EQXFR-AWS-EUC1 starts at FR2, a zone of C001-P-FR's metro FR. A virtual circuit's end count is not checked:
a provider-managed one may surface on a single port, its far end the provider's (VC-C001-EQXFR-AWS-EUC1 has only
the cage edge's port, its cloud end is a CloudVirtualInterface). A physical circuit's end count and port kind are
checks/physical_circuit_ports.py's.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import permutations
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

CIRCUIT_LOCATIONS = 2
"""A circuit terminates on exactly two places, one per end."""

ZONE = "TopologyColocationZone"
VIRTUAL_COLOCATION = "TopologyVirtualColocation"
# The colocation kinds a circuit can end on, each a child of its metro: a cage and a provider-hosted presence.
ZONE_KINDS = frozenset({ZONE, VIRTUAL_COLOCATION})
INTERNET = "TopologyExternalProviders"
OFFICE = "TopologyCustomerOffice"
# Footprints whose facility is their parent (a DC, a colocation metro, a cloud region). An office is its own.
PARENTED_FOOTPRINTS = frozenset({"TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud"})
FOOTPRINTS = PARENTED_FOOTPRINTS | {OFFICE}
FACILITIES = frozenset(
    {"TopologyDataCenter", *ZONE_KINDS, "TopologyCloudRegion", INTERNET, "TopologySaasRegion", OFFICE}
)
PROVIDER_FAR_ENDS = frozenset({"TopologyCloudRegion", "TopologySaasRegion", *ZONE_KINDS})


def _label(place: dict[str, Any] | None) -> str:
    """``Kind 'name'``, for a message."""
    place = place or {}
    return f"{place.get('typename') or '<unknown kind>'} '{place.get('name') or place.get('id') or '<unknown>'}'"


def _labels(places: list[dict[str, Any]]) -> str:
    return ", ".join(_label(place) for place in places) or "none"


def _within(place: dict[str, Any] | None, anchor_id: str | None) -> bool:
    """The place is the anchor, or a zone / virtual colocation of it when the anchor is a colocation metro."""
    if not place or not anchor_id:
        return False
    if place.get("id") == anchor_id:
        return True
    return place.get("typename") in ZONE_KINDS and (place.get("parent") or {}).get("id") == anchor_id


def _end_at(location: dict[str, Any], deployment: dict[str, Any] | None) -> bool:
    """The location is the end device's deployment itself. Colocation kit is deployed per zone or virtual
    colocation, and add_circuit (plan_locations) refuses an authored location that differs from it: no sibling
    cage of the same metro, never the metro."""
    return bool(deployment) and bool(location.get("id")) and location.get("id") == (deployment or {}).get("id")


def _facility(location: dict[str, Any]) -> dict[str, Any]:
    """The facility a location stands for: a footprint's parent, any other location itself."""
    if location.get("typename") in PARENTED_FOOTPRINTS:
        return location.get("parent") or {}
    return location


def _locations(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The circuit's locations, each once."""
    seen: dict[str, dict[str, Any]] = {}
    for location in circuit.get("locations") or []:
        if isinstance(location, dict):
            seen.setdefault(str(location.get("id")), location)
    return list(seen.values())


def _ends(circuit: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    """(``device:port``, the device's deployment) per end port."""
    ends = []
    for port in circuit.get("capabilities") or []:
        if not isinstance(port, dict):
            continue
        device = port.get("device") or {}
        label = f"{device.get('name') or '<unknown device>'}:{port.get('name') or port.get('id')}"
        deployment = device.get("deployment")
        ends.append((label, deployment if isinstance(deployment, dict) else None))
    return ends


def _ends_label(ends: list[tuple[str, dict[str, Any] | None]]) -> str:
    return ", ".join(f"{port} in {_label(deployment)}" for port, deployment in ends) or "no end ports"


def _chain_reaches(
    hops: list[list[dict[str, Any]]], start_id: str | None, arrived: Callable[[dict[str, Any]], bool]
) -> bool:
    """Some order of ALL hops starts within ``start_id``, joins each hop to the next on a shared facility and
    finishes on a place ``arrived`` accepts."""

    def walk(place: dict[str, Any] | None, remaining: list[list[dict[str, Any]]]) -> bool:
        if not remaining:
            return place is not None and arrived(place)
        for index, hop in enumerate(remaining):
            rest = remaining[:index] + remaining[index + 1 :]
            for near, far in (hop, hop[::-1]):
                joins = _within(near, start_id) if place is None else near.get("id") == place.get("id")
                if joins and walk(far, rest):
                    return True
        return False

    return walk(None, hops)


class CheckCircuitPlacement(InfrahubCheck):
    """Physical and virtual circuits terminate on the right places, owned by the right customer, on a contiguous
    underlay."""

    query = "circuit_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for circuit in cleaned.get("TopologyPhysicalCircuit") or []:
            self._validate_physical(circuit)
        for circuit in cleaned.get("TopologyVirtualCircuit") or []:
            self._validate_virtual(circuit)

    def _validate_physical(self, circuit: dict[str, Any]) -> None:
        name = circuit.get("name") or circuit.get("id") or "<unnamed-circuit>"
        locations = _locations(circuit)
        self._validate_physical_owner(circuit, name, locations)
        if len(locations) != CIRCUIT_LOCATIONS:
            self._error(
                circuit,
                "TopologyPhysicalCircuit",
                f"Physical circuit '{name}' terminates on {len(locations)} location(s) ({_labels(locations)}): a "
                f"physical circuit has exactly {CIRCUIT_LOCATIONS} distinct locations, one facility per end.",
            )
            return

        ends = _ends(circuit)
        if len(ends) != CIRCUIT_LOCATIONS:
            return  # checks/physical_circuit_ports.py reports the end count
        undeployed = [port for port, deployment in ends if deployment is None]
        for port in undeployed:
            self._error(
                circuit,
                "TopologyPhysicalCircuit",
                f"Physical circuit '{name}' end '{port}' is on a device with no deployment: set the device's "
                "deployment to the facility it sits in (an on-ramp edge: its colocation zone).",
            )
        if undeployed:
            return

        if any(
            all(_end_at(location, deployment) for location, (_, deployment) in zip(locations, order))
            for order in permutations(ends)
        ):
            return
        self._error(
            circuit,
            "TopologyPhysicalCircuit",
            f"Physical circuit '{name}' terminates on {_labels(locations)} but its ends are {_ends_label(ends)}: "
            "each location is the deployment of one end's device (a DC, ISP, cloud region, or for colocation kit the "
            "zone or virtual colocation the device is deployed to; not a sibling zone of its metro).",
        )

    def _validate_physical_owner(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """An office's access circuit is owned by the office's owner; any other physical circuit is unowned."""
        owner = circuit.get("owner") if isinstance(circuit.get("owner"), dict) else None
        owner_id = (owner or {}).get("id")
        org = (owner or {}).get("org_id") or owner_id
        ownership = f"owned by '{org}'" if owner_id else "unowned"
        offices = [location for location in locations if location.get("typename") == OFFICE]
        for office in offices:
            office_owner = office.get("owner") or {}
            if office_owner.get("id") != owner_id:
                office_org = office_owner.get("org_id") or office_owner.get("id") or "<no owner>"
                self._error(
                    circuit,
                    "TopologyPhysicalCircuit",
                    f"Physical circuit '{name}' ends on {_label(office)} of '{office_org}' but is {ownership}: an "
                    "office's access circuit is owned by the office's owner.",
                )
        if owner_id and not offices:
            self._error(
                circuit,
                "TopologyPhysicalCircuit",
                f"Physical circuit '{name}' is owned by '{org}' but ends on no office ({_labels(locations)}): a "
                "physical circuit is shared underlay, unowned, unless it is an office's access circuit.",
            )

    def _validate_virtual(self, circuit: dict[str, Any]) -> None:
        name = circuit.get("name") or circuit.get("id") or "<unnamed-circuit>"
        locations = _locations(circuit)
        if len(locations) != CIRCUIT_LOCATIONS:
            self._error(
                circuit,
                "TopologyVirtualCircuit",
                f"Virtual circuit '{name}' terminates on {len(locations)} location(s) ({_labels(locations)}): a "
                f"virtual circuit has exactly {CIRCUIT_LOCATIONS} distinct locations, one per end.",
            )
            return

        owner = circuit.get("owner")
        if isinstance(owner, dict):
            self._validate_owned(circuit, name, owner, locations)
        else:
            self._validate_unowned(circuit, name, locations)
        self._validate_underlay(circuit, name, locations)

    def _validate_owned(
        self, circuit: dict[str, Any], name: str, owner: dict[str, Any], locations: list[dict[str, Any]]
    ) -> None:
        """One end is a footprint of the owner with an end device in its facility; the other a provider facility
        or another footprint."""
        org = owner.get("org_id") or owner.get("id")
        own = [
            location
            for location in locations
            if location.get("typename") in FOOTPRINTS and (location.get("owner") or {}).get("id") == owner.get("id")
        ]
        if not own:
            self._error(
                circuit,
                "TopologyVirtualCircuit",
                f"Virtual circuit '{name}' is owned by '{org}' but none of its locations ({_labels(locations)}) is a "
                f"footprint of '{org}': its tenant end is a {', '.join(sorted(FOOTPRINTS))} owned by '{org}'.",
            )
            return

        ends = _ends(circuit)
        placed = [
            location
            for location in own
            if any(_within(deployment, _facility(location).get("id")) for _, deployment in ends)
        ]
        if not placed:
            tenant = own[0]
            self._error(
                circuit,
                "TopologyVirtualCircuit",
                f"Virtual circuit '{name}' tenant end {_label(tenant)} is in {_label(_facility(tenant))} but none "
                f"of its ends is ({_ends_label(ends)}): terminate it on a device deployed to "
                f"{_label(_facility(tenant))} (or a zone or virtual colocation of it).",
            )
            return

        tenant = placed[0]
        far = next(location for location in locations if location is not tenant)
        if far.get("typename") not in PROVIDER_FAR_ENDS | FOOTPRINTS:
            self._error(
                circuit,
                "TopologyVirtualCircuit",
                f"Virtual circuit '{name}' runs from {_label(tenant)} to {_label(far)}: an owned virtual circuit's "
                f"far end is a provider facility ({', '.join(sorted(PROVIDER_FAR_ENDS))}) or another footprint.",
            )

    def _validate_unowned(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """A platform overlay terminates on facilities at both ends."""
        for location in locations:
            if location.get("typename") not in FACILITIES:
                self._error(
                    circuit,
                    "TopologyVirtualCircuit",
                    f"Virtual circuit '{name}' has no owner but terminates on {_label(location)}: an unowned "
                    f"(platform) virtual circuit terminates on facilities at both ends "
                    f"({', '.join(sorted(FACILITIES))}); set the circuit's owner or name the facility.",
                )

    def _validate_underlay(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """``physical_circuits`` form one contiguous path: end to end, or one end to the internet."""
        first, second = (_facility(location) for location in locations)
        named_hops = [
            (hop.get("name") or hop.get("id"), _locations(hop))
            for hop in circuit.get("physical_circuits") or []
            if isinstance(hop, dict)
        ]
        hops = [ends for _, ends in named_hops]
        transport_mode = circuit.get("transport_mode")
        if not hops:
            if transport_mode == "physical_backed":
                self._error(
                    circuit,
                    "TopologyVirtualCircuit",
                    f"Virtual circuit '{name}' is physical_backed but rides no physical circuit: list its underlay "
                    f"from {_label(first)} to {_label(second)} in physical_circuits.",
                )
            return
        if any(len(hop) != CIRCUIT_LOCATIONS for hop in hops):
            return  # that physical circuit's own location count is reported against it

        if transport_mode == "internet_backed":
            reached = any(
                _chain_reaches(hops, start.get("id"), lambda place: place.get("typename") == INTERNET)
                for start in (first, second)
            )
            path = f"from {_label(first)} or {_label(second)} to the internet (a {INTERNET})"
        else:
            reached = any(
                _chain_reaches(hops, start.get("id"), lambda place, finish=finish: _within(place, finish.get("id")))
                for start, finish in ((first, second), (second, first))
            )
            path = f"from {_label(first)} to {_label(second)}"
        if reached:
            return
        underlay = ", ".join(
            f"{hop_name} ({' - '.join(str(end.get('name')) for end in ends)})" for hop_name, ends in named_hops
        )
        self._error(
            circuit,
            "TopologyVirtualCircuit",
            f"Virtual circuit '{name}' underlay {underlay} is not one contiguous path {path}: in some order, each "
            "physical circuit shares a facility with the next, the first starts and the last finishes at the path's "
            "ends.",
        )

    def _error(self, circuit: dict[str, Any], kind: str, message: str) -> None:
        """Log one finding against the circuit."""
        self.log_error(message=message, object_id=circuit.get("id"), object_type=kind)
