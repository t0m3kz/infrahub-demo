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
    4. a tunnel of ours (``TUNNEL_LINK_TYPES``: sd_wan, vpn_ipsec, vpn_ssl, gre, geneve) is internet_backed or
       physical_backed, never provider_virtual_only or unset: the generator selects its session on the link type
       alone and rule 5 applies no physical rule to those two modes, so such a tunnel would escape both,
    5. the underlay is derived, never listed: the physical circuits found in ``capabilities`` of the circuit's own
       ports (``utils.circuit_underlay.derived_underlay``), each once, a decommissioned one ignored (it neither
       satisfies "rides at least one" nor counts in the path). A physical_backed or internet_backed circuit has at
       least one, and they are contiguous over the port-derived circuits: in some order each shares a facility with
       the next, end to end for a physical_backed circuit, from one end to the internet (a TopologyExternalProviders)
       for an internet_backed one. Relationship peers carry no order, so every order of the hops is tried. A
       provider_virtual_only or unset transport mode has no physical rule: an Equinix Network Edge has no physical
       circuit,
    6. circuits meeting at a device share an environment class: prod and non-prod never connect. An owned circuit's
       environment is that of its owner's footprints among its locations (a DC or colocation footprint states it,
       an office is always PROD, a cloud footprint or one without an environment has none and is skipped, as is an
       unowned platform overlay). When the owner's footprints on one circuit disagree, any prod footprint makes the
       circuit prod (the strict side), otherwise it is non-prod. Two circuits meet where a port of each sits on the
       same device; one finding per device, naming each circuit and its environment. Several tenants of one class
       sharing an on-ramp edge (eg-fr201 Ethernet1/13) pass: their owners need not match, and d / t / s (all
       non-prod) may meet.

A colocation footprint is parented by the METRO, not a cage, so a zone or virtual colocation of that metro
reaches it: the path of VC-C001-EQXFR-AWS-EUC1 starts at FR2, a zone of C001-P-FR's metro FR.
A virtual circuit's end count is not checked:
a provider-managed one may surface on a single port, its far end the provider's (VC-C001-EQXFR-AWS-EUC1 has only
the cage edge's port, its cloud end is a CloudVirtualInterface). A physical circuit's end count and port kind are
checks/physical_circuit_ports.py's.

Known limitations (the check sees the final state of the data, nothing more):

- a port removed from a physical circuit reruns nothing: the virtual circuits that sat on it keep their session
  until their next run,
- a virtual circuit end on a logical port (LAG, sub-interface) derives nothing for that end: there is no walk to the
  parent interface,
- the DC to Network Edge Equinix Fabric hop has no addresses and no session,
- a virtual circuit created before its ports are linked to a physical circuit (add_sdwan_edge) is reported if the
  check runs in between.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from itertools import permutations
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.circuit_underlay import DECOMMISSIONED, TUNNEL_LINK_TYPES, derived_underlay
from utils.data_cleaning import clean_data
from utils.exchange_transit import namespace_type_for_environment

from .segment_placement import OFFICE_ENVIRONMENT

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
# The transport modes held to a physical path; provider_virtual_only and unset have no physical rule.
PHYSICAL_TRANSPORT_MODES = frozenset({"physical_backed", "internet_backed"})
PROVIDER_FAR_ENDS = frozenset({"TopologyCloudRegion", "TopologySaasRegion", *ZONE_KINDS})
# Footprints that state an environment (TopologyEnvironmentScoped); an office has none, it is always PROD (`p`, the
# Production choice, checks.segment_placement.OFFICE_ENVIRONMENT); a cloud footprint has none either, its CloudAccount carries it, so it is unknown here.
ENVIRONMENT_FOOTPRINTS = frozenset({"TopologyCustomerDC", "TopologyCustomerColocation"})


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


def _ports(circuit: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The circuit's own ports (``capabilities``), each once."""
    for port in circuit.get("capabilities") or []:
        if isinstance(port, dict):
            yield port


def _ends(circuit: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    """(``device:port``, the device's deployment) per end port."""
    ends = []
    for port in _ports(circuit):
        device = port.get("device") or {}
        label = f"{device.get('name') or '<unknown device>'}:{port.get('name') or port.get('id')}"
        deployment = device.get("deployment")
        ends.append((label, deployment if isinstance(deployment, dict) else None))
    return ends


def _ends_label(ends: list[tuple[str, dict[str, Any] | None]]) -> str:
    return ", ".join(f"{port} in {_label(deployment)}" for port, deployment in ends) or "no end ports"


def _environment(circuit: dict[str, Any]) -> str | None:
    """The environment of an owned circuit: its owner's footprints among its locations, None when unknown.

    Unowned circuits and circuits whose owner has no footprint stating an environment (a cloud footprint, a
    footprint without one) have none. Footprints that disagree on the class: any prod one makes the circuit prod,
    else the lowest environment value of the non-prod ones, so the answer never depends on the order."""
    owner_id = (circuit.get("owner") or {}).get("id")
    if not owner_id:
        return None
    environments = set()
    for location in _locations(circuit):
        if (location.get("owner") or {}).get("id") != owner_id:
            continue
        if location.get("typename") == OFFICE:
            environments.add(OFFICE_ENVIRONMENT)
        elif location.get("typename") in ENVIRONMENT_FOOTPRINTS and location.get("environment"):
            environments.add(location["environment"])
    if not environments:
        return None
    return OFFICE_ENVIRONMENT if OFFICE_ENVIRONMENT in environments else min(environments)


def _devices(circuit: dict[str, Any]) -> dict[str, str]:
    """Device id -> name of the devices the circuit's own ports sit on."""
    devices = {}
    for port in _ports(circuit):
        device = port.get("device")
        if isinstance(device, dict) and device.get("id"):
            devices[str(device["id"])] = str(device.get("name") or device["id"])
    return devices


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
    derived underlay; virtual circuits meeting at a device share an environment class (prod never meets non-prod)."""

    query = "circuit_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for circuit in cleaned.get("TopologyPhysicalCircuit") or []:
            self._validate_physical(circuit)
        virtual = cleaned.get("TopologyVirtualCircuit") or []
        for circuit in virtual:
            self._validate_virtual(circuit)
        self._validate_environments(virtual)

    def _validate_environments(self, circuits: list[dict[str, Any]]) -> None:
        """Virtual circuits with a port on the same device are of one environment class: prod and non-prod never
        connect. One finding per conflicting device, against its first circuit by name, naming every circuit."""
        classes: dict[str, str] = {}  # environment value -> its class, computed once

        def env_class(environment: str) -> str:
            if environment not in classes:
                classes[environment] = namespace_type_for_environment(environment)
            return classes[environment]

        # device id -> (device name, [(circuit, environment)]), each circuit once per device
        meeting: dict[str, tuple[str, list[tuple[dict[str, Any], str]]]] = {}
        for circuit in circuits:
            environment = _environment(circuit)
            if environment is None:
                continue
            for device_id, device_name in _devices(circuit).items():
                meeting.setdefault(device_id, (device_name, []))[1].append((circuit, environment))
        for device_name, members in meeting.values():
            if len({env_class(environment) for _, environment in members}) < 2:
                continue
            members.sort(key=lambda member: str(member[0].get("name") or member[0].get("id")))
            listing = ", ".join(
                f"'{circuit.get('name') or circuit.get('id')}' (environment '{environment}', {env_class(environment)})"
                for circuit, environment in members
            )
            self._error(
                members[0][0],
                "TopologyVirtualCircuit",
                f"Device '{device_name}' carries virtual circuits of different environments: {listing}: prod and "
                "non-prod never connect, so terminate each environment's circuits on its own device.",
            )

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

        self._validate_tunnel_transport(circuit, name)
        owner = circuit.get("owner")
        if isinstance(owner, dict):
            self._validate_owned(circuit, name, owner, locations)
        else:
            self._validate_unowned(circuit, name, locations)
        self._validate_underlay(circuit, name, locations)

    def _validate_tunnel_transport(self, circuit: dict[str, Any], name: str) -> None:
        """A tunnel of ours is internet_backed or physical_backed: the generator selects its session on the link type
        alone and the underlay rule holds only those two modes to a physical path."""
        link_type = circuit.get("link_type")
        transport_mode = circuit.get("transport_mode")
        if link_type in TUNNEL_LINK_TYPES and transport_mode not in PHYSICAL_TRANSPORT_MODES:
            self._error(
                circuit,
                "TopologyVirtualCircuit",
                f"Virtual circuit '{name}' is a tunnel (link_type '{link_type}') with transport_mode "
                f"'{transport_mode or 'unset'}': a tunnel runs over the internet or a physical circuit, set its "
                f"transport_mode to one of {', '.join(sorted(PHYSICAL_TRANSPORT_MODES))}.",
            )

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
        """The derived underlay (live physical circuits on the circuit's own ports) exists and is contiguous:
        end to end for a physical_backed circuit, from one end to the internet for an internet_backed one. A
        provider_virtual_only or unset circuit has no physical rule (an Equinix Network Edge has no physical circuit
        at all)."""
        transport_mode = circuit.get("transport_mode")
        if transport_mode not in PHYSICAL_TRANSPORT_MODES:
            return
        first, second = (_facility(location) for location in locations)
        named_hops = [
            (hop.get("name") or hop.get("id"), _locations(hop))
            for hop in derived_underlay(circuit)
            if hop.get("status") != DECOMMISSIONED
        ]
        hops = [ends for _, ends in named_hops]
        if not hops:
            expected = (
                f"from {_label(first)} to {_label(second)}"
                if transport_mode == "physical_backed"
                else f"from {_label(first)} or {_label(second)} to the internet (a {INTERNET})"
            )
            self._error(
                circuit,
                "TopologyVirtualCircuit",
                f"Virtual circuit '{name}' is {transport_mode} but its ports are on no physical circuit: terminate "
                f"it on ports cabled through a physical circuit {expected}.",
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
