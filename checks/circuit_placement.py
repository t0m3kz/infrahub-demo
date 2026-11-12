"""Validate where a circuit terminates and what carries it, per circuit type: locations, owner, carrier, environment.

Rules by type (carrier, tunnel, dci, handoff, vpn) and the known gaps: see AGENTS.md Circuit placement.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import permutations
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.circuit_underlay import (
    BGP_PROCESS,
    CLOUD_PEERING_KIND,
    DCI_TYPE,
    DECOMMISSIONED,
    HANDOFF_TYPE,
    INTERNET_KIND,
    OVERLAY_ROLE,
    SESSION_CIRCUIT,
    SESSION_CIRCUIT_TYPES,
    TUNNEL_TYPE,
    VPN_TYPE,
    derived_underlay,
    is_provider_device,
    overlay_processes,
    port_cabled,
    port_underlay,
)
from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, TENANT_NAMESPACE_TYPES
from utils.interface_encapsulation import parent_of, vlan_of
from utils.internet_object import internet_findings
from utils.limits import CIRCUIT_ENDS
from utils.ownership import CircuitOwnerError, circuit_owner

CARRIER = "TopologyCarrierCircuit"
# The session circuit types, keyed by their ``circuit_type`` value.
TUNNEL = TUNNEL_TYPE
INTERCONNECT = DCI_TYPE
HANDOFF = HANDOFF_TYPE
VPN = VPN_TYPE
CIRCUIT_TYPES = (TUNNEL, INTERCONNECT, HANDOFF, VPN)
NOUNS = {TUNNEL: "Tunnel", INTERCONNECT: "Interconnect", HANDOFF: "Handoff", VPN: "Vpn"}
# The fields a handoff (and a vpn, for the namespace) carries; the schema leaves them optional on the one session kind.
HANDOFF_ONLY_FIELDS = ("vrf", "password")

INTERNET_CARRIER = "internet"
OFFICE = "TopologyCustomerOffice"
PHYSICAL_DEVICE = "DcimPhysicalDevice"
PHYSICAL_PORT = "DcimPhysicalInterface"
VIRTUAL_PORT = "DcimVirtualInterface"
# The shape of a handoff end: a sub-interface, a routed physical port, a cloud peering interface.
SUB_INTERFACE = "sub-interface"
ROUTED_PORT = "routed port"
CLOUD_PEERING = "cloud peering interface"


def _label(place: dict[str, Any] | None) -> str:
    """``Kind 'name'``, for a message."""
    place = place or {}
    return f"{place.get('typename') or '<unknown kind>'} '{place.get('name') or place.get('id') or '<unknown>'}'"


def _labels(places: list[dict[str, Any]]) -> str:
    return ", ".join(_label(place) for place in places) or "none"


def _name(circuit: dict[str, Any]) -> str:
    return str(circuit.get("name") or circuit.get("id") or "<unnamed-circuit>")


def _owner_org(owner: dict[str, Any] | None) -> str:
    owner = owner or {}
    return str(owner.get("org_id") or owner.get("id") or "<no owner>")


def _offices(locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The TopologyCustomerOffice among the locations."""
    return [location for location in locations if location.get("typename") == OFFICE]


def _office_owners(locations: list[dict[str, Any]]) -> list[str]:
    """The owner key of each office among the locations that has one."""
    return [_owner_org(office["owner"]) for office in _offices(locations) if isinstance(office.get("owner"), dict)]


def _end_at(location: dict[str, Any], deployment: dict[str, Any] | None) -> bool:
    """The location is the end device's deployment itself (no sibling cage, never the metro)."""
    return bool(deployment) and bool(location.get("id")) and location.get("id") == (deployment or {}).get("id")


def _locations(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The circuit's locations, each once."""
    seen: dict[str, dict[str, Any]] = {}
    for location in circuit.get("locations") or []:
        if isinstance(location, dict):
            seen.setdefault(str(location.get("id")), location)
    return list(seen.values())


def _facility(device: dict[str, Any] | None) -> dict[str, Any] | None:
    """Where a device sits: its ``deployment``, else its region or SaaS region, else its provider ``location``."""
    device = device or {}
    if device.get("typename") == INTERNET_KIND:
        return device
    place = device.get("deployment") or device.get("region") or device.get("saas_region") or device.get("location")
    return place if isinstance(place, dict) else None


def _ref_id(node: Any) -> str | None:
    """The id of a cleaned reference: a dict, or the bare id clean_data collapses an id-only object to."""
    value = node.get("id") if isinstance(node, dict) else node
    return str(value) if value else None


def _ports(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The circuit's own ports (``capabilities``)."""
    return [port for port in circuit.get("capabilities") or [] if isinstance(port, dict)]


def _port_label(port: dict[str, Any]) -> str:
    device = port.get("device") or {}
    return f"{device.get('name') or '<unknown device>'}:{port.get('name') or port.get('id')}"


def _ends(circuit: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    """(``device:port``, the device's facility) per end port of a circuit."""
    return [(_port_label(port), _facility(port.get("device"))) for port in _ports(circuit)]


def _ends_label(ends: list[tuple[str, dict[str, Any] | None]]) -> str:
    return ", ".join(f"{port} in {_label(deployment)}" for port, deployment in ends) or "no end ports"


def _live_carriers(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The carrier circuits at one port (or its parent interface), a decommissioned one ignored."""
    return [carrier for carrier in port_underlay(port) if carrier.get("status") != DECOMMISSIONED]


def _live_underlay(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The live carriers of all the circuit's ports, each once."""
    return [carrier for carrier in derived_underlay(circuit) if carrier.get("status") != DECOMMISSIONED]


def _carriers_label(carriers: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{_name(carrier)} ({' - '.join(str(end.get('name')) for end in _locations(carrier))})" for carrier in carriers
    )


def _end_shape(port: dict[str, Any]) -> tuple[str | None, int | None]:
    """(shape, VLAN) of a handoff end; the shape is None when the end is none of the three."""
    if port.get("typename") == CLOUD_PEERING_KIND:
        return CLOUD_PEERING, vlan_of(port)
    if port.get("typename") == VIRTUAL_PORT and parent_of(port):
        return SUB_INTERFACE, vlan_of(port)
    if port.get("typename") == PHYSICAL_PORT and port.get("ip_address"):
        return ROUTED_PORT, None
    return None, None


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
    """True when some order of ALL hops starts at ``start_id``, links each hop on a shared facility and ends where
    ``arrived`` accepts.
    """

    def walk(place: dict[str, Any] | None, remaining: list[list[dict[str, Any]]]) -> bool:
        if not remaining:
            return place is not None and arrived(place)
        for index, hop in enumerate(remaining):
            rest = remaining[:index] + remaining[index + 1 :]
            for near, far in (hop, hop[::-1]):
                joins = near.get("id") == start_id if place is None else near.get("id") == place.get("id")
                if joins and walk(far, rest):
                    return True
        return False

    return walk(None, hops)


def _environment_class(circuit_type: str, circuit: dict[str, Any]) -> tuple[str, str] | None:
    """(class, source) of a circuit, None when it states none; a tunnel is PROD."""
    if circuit_type == TUNNEL:
        office = next((location for location in _locations(circuit) if location.get("typename") == OFFICE), None)
        return (PROD_TYPE, f"office '{office.get('name')}'") if office else None
    if circuit_type in (HANDOFF, VPN):
        namespace = (circuit.get("vrf") or {}).get("security_zone") or {}
        if namespace.get("vrf_type") in TENANT_NAMESPACE_TYPES:
            return namespace["vrf_type"], f"namespace '{namespace.get('name')}'"
    return None


class CheckCircuitPlacement(InfrahubCheck):
    """Carrier and session circuits terminate on the right places; circuits meeting at a device share an environment
    class.
    """

    query = "circuit_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for circuit in cleaned.get(CARRIER) or []:
            self._validate_carrier(circuit)
        for node, message in internet_findings(cleaned.get(CARRIER) or [], cleaned.get(INTERNET_KIND) or []):
            if node.get("typename") == INTERNET_KIND:
                self.log_error(message=message, object_id=node.get("id"), object_type=INTERNET_KIND)
            else:
                self._error(node, CARRIER, message)
        by_type: dict[str, list[dict[str, Any]]] = {circuit_type: [] for circuit_type in CIRCUIT_TYPES}
        for circuit in cleaned.get(SESSION_CIRCUIT) or []:
            circuit_type = circuit.get("circuit_type")
            if circuit_type in by_type:
                by_type[circuit_type].append(circuit)
            else:
                self._error(
                    circuit,
                    SESSION_CIRCUIT,
                    f"Session circuit '{_name(circuit)}' has circuit_type '{circuit_type}': it is one of "
                    f"{', '.join(CIRCUIT_TYPES)}.",
                )
        # A decommissioned dci/handoff is not validated (nothing builds it); a tunnel always is.
        live = {
            circuit_type: [c for c in circuits if c.get("status") != DECOMMISSIONED]
            if circuit_type in SESSION_CIRCUIT_TYPES
            else circuits
            for circuit_type, circuits in by_type.items()
        }
        for circuit in live[TUNNEL]:
            self._validate_tunnel(circuit)
        for circuit in live[INTERCONNECT]:
            self._validate_interconnect(circuit)
        for circuit in live[HANDOFF]:
            self._validate_handoff(circuit)
        for circuit in live[VPN]:
            self._validate_vpn(circuit)
        self._validate_session_ports(
            [(circuit_type, circuit) for circuit_type in SESSION_CIRCUIT_TYPES for circuit in live[circuit_type]]
        )
        self._validate_environments(
            [(circuit_type, circuit) for circuit_type in CIRCUIT_TYPES for circuit in live[circuit_type]]
        )

    # Carrier circuits

    def _validate_carrier(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        self._validate_owner(circuit, CARRIER, locations)
        if len(locations) > CIRCUIT_ENDS:
            self._error(
                circuit,
                CARRIER,
                f"Carrier circuit '{name}' terminates on {len(locations)} location(s) ({_labels(locations)}): a "
                f"carrier circuit has at most {CIRCUIT_ENDS} distinct locations, one facility per end that is placed.",
            )
            return

        ends = _ends(circuit)
        if len(ends) != CIRCUIT_ENDS:
            return  # checks/carrier_circuit_ports.py reports the end count
        provider = _ref_id(circuit.get("provider"))
        placed: list[tuple[str, dict[str, Any] | None]] = []
        routers: list[dict[str, Any]] = []
        unplaced = False
        for port in _ports(circuit):
            label, device = _port_label(port), port.get("device") or {}
            if (deployment := _facility(device)) is not None:
                placed.append((label, deployment))
            elif is_provider_device(device):
                routers.append(device)
            else:
                unplaced = True
                self._error(
                    circuit,
                    CARRIER,
                    f"Carrier circuit '{name}' end '{label}' is on a device with no deployment and not a router of "
                    "a provider: set the device's deployment to the facility it sits in (an on-ramp edge: its "
                    "colocation zone), or own it by a provider.",
                )
        if unplaced:
            return
        if routers and not any(_ref_id(device.get("owner")) == provider for device in routers):
            self._error(
                circuit,
                CARRIER,
                f"Carrier circuit '{name}' has provider-owned router end(s) ({', '.join(_name(d) for d in routers)}) "
                "but none is owned by the circuit's provider: the circuit's provider names the end that has no "
                "location.",
            )
            return
        if len(placed) == len(locations) and any(
            all(_end_at(location, deployment) for location, (_, deployment) in zip(locations, order))
            for order in permutations(placed)
        ):
            return
        self._error(
            circuit,
            CARRIER,
            f"Carrier circuit '{name}' terminates on {_labels(locations)} but its placed ends are "
            f"{_ends_label(placed)}: each location is the deployment of one end's device (a DC, cloud region, or for "
            "colocation kit the zone or virtual colocation the device is deployed to; not a sibling zone of its "
            "metro), and an end that is a provider's router has none.",
        )

    def _validate_owner(self, circuit: dict[str, Any], kind: str, locations: list[dict[str, Any]]) -> None:
        """The owner is derived from the offices among the locations: one owner; only a tunnel needs an office."""
        noun = "Carrier" if kind == CARRIER else NOUNS[kind]
        name = _name(circuit)
        try:
            circuit_owner(_office_owners(locations))
        except CircuitOwnerError as exc:
            self._error(
                circuit,
                kind,
                f"{noun} circuit '{name}' has an owner that cannot be derived: {exc} ({_labels(_offices(locations))}).",
            )
            return
        offices = _offices(locations)
        if kind == TUNNEL and not offices:
            self._error(
                circuit,
                kind,
                f"Tunnel circuit '{name}' has no office among its locations ({_labels(locations)}): a tunnel is owned "
                f"by its office's owner, none of its locations is a {OFFICE}.",
            )
        elif kind in (INTERCONNECT, HANDOFF, VPN) and _office_owners(locations):
            self._error(
                circuit,
                kind,
                f"{noun} circuit '{name}' terminates on {_labels(offices)}: an office owns a tunnel and its access "
                f"circuit; a {kind} circuit names no office.",
            )

    # Tunnel circuits

    def _validate_tunnel(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        ports = _ports(circuit)
        self._validate_shape(circuit, TUNNEL, locations, ports, CIRCUIT_ENDS)
        self._validate_fields(circuit, TUNNEL, ("password",))
        if circuit.get("registered_prefixes") and not (circuit.get("vrf") or {}).get("id"):
            self._error(
                circuit,
                TUNNEL,
                f"Tunnel circuit '{name}' registers prefixes but has no routing VRF.",
            )

        self._validate_owner(circuit, TUNNEL, locations)

        for port in ports:
            if port.get("typename") == CLOUD_PEERING_KIND:
                continue
            live = _live_carriers(port)
            if any(carrier.get("circuit_type") == INTERNET_CARRIER for carrier in live):
                continue
            found = f"its live carrier circuits are {_carriers_label(live)}" if live else "it is on no live one"
            self._error(
                circuit,
                TUNNEL,
                f"Tunnel circuit '{name}' end '{_port_label(port)}' rides no live internet carrier circuit (one "
                f"of circuit_type {INTERNET_CARRIER}): {found}. Every end of a tunnel has its own internet access.",
            )

    # Interconnect circuits

    def _validate_interconnect(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        ports = _ports(circuit)
        self._validate_owner(circuit, INTERCONNECT, locations)
        self._validate_fields(circuit, INTERCONNECT, (*HANDOFF_ONLY_FIELDS, "registered_prefixes"))
        self._validate_shape(circuit, INTERCONNECT, locations, ports, CIRCUIT_ENDS)
        self._validate_overlay_processes(circuit, name, ports)

        uncarried = [port for port in ports if not _live_carriers(port)]
        for port in uncarried:
            self._error(
                circuit,
                INTERCONNECT,
                f"Interconnect circuit '{name}' end '{_port_label(port)}' is on no live carrier circuit: terminate "
                "it on a port cabled through a carrier circuit.",
            )
        if uncarried or len(locations) != CIRCUIT_ENDS:
            return

        carriers = _live_underlay(circuit)
        hops = [_locations(carrier) for carrier in carriers]
        if any(len(hop) != CIRCUIT_ENDS for hop in hops):
            return  # that carrier circuit's own location count is reported against it

        if len(carriers) == 1:
            carrier_ports = {str(end.get("id")) for end in carriers[0].get("capabilities") or []}
            if carrier_ports != {str(port.get("id")) for port in ports}:
                ends = ", ".join(
                    f"{(end.get('device') or {}).get('name')}:{end.get('name')}"
                    for end in carriers[0].get("capabilities") or []
                )
                self._error(
                    circuit,
                    INTERCONNECT,
                    f"Interconnect circuit '{name}' ports {', '.join(_port_label(port) for port in ports)} are not "
                    f"the ends of its carrier {_name(carriers[0])} ({ends or 'no ends'}): a DCI is pure fibre, the "
                    "carrier circuit's two ends are the circuit's two ports.",
                )

        first, second = locations
        if any(
            _chain_reaches(hops, start.get("id"), lambda place, finish=finish: place.get("id") == finish.get("id"))
            for start, finish in ((first, second), (second, first))
        ):
            return
        self._error(
            circuit,
            INTERCONNECT,
            f"Interconnect circuit '{name}' carrier {_carriers_label(carriers)} is not one contiguous path from "
            f"{_label(first)} to {_label(second)}: in some order, each carrier circuit shares a facility with the "
            "next, the first starts and the last finishes at the path's ends.",
        )

    def _validate_overlay_processes(self, circuit: dict[str, Any], name: str, ports: list[dict[str, Any]]) -> None:
        """Each end device runs exactly one overlay BGP process: the EVPN session is built between them."""
        devices = {str(port["device"]["id"]): port["device"] for port in ports if (port.get("device") or {}).get("id")}
        for device in devices.values():
            overlay = overlay_processes(device)
            if len(overlay) != 1:
                self._error(
                    circuit,
                    INTERCONNECT,
                    f"Interconnect circuit '{name}' end device '{device.get('name')}' has {len(overlay)} {BGP_PROCESS} "
                    f"process(es) of process_role '{OVERLAY_ROLE}': a border gateway runs exactly one.",
                )

    # Handoff circuits

    def _validate_handoff(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        ports = _ports(circuit)
        locations = _locations(circuit)
        self._validate_shape(circuit, HANDOFF, locations, ports, 1)
        self._validate_fields(circuit, HANDOFF, ("registered_prefixes",))
        self._validate_owner(circuit, HANDOFF, locations)
        if not ((circuit.get("vrf") or {}).get("security_zone") or {}).get("name"):
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' has no vrf: its /31 is allocated and its session runs in an "
                "environment namespace.",
            )
        self._validate_handoff_placement(circuit, name, locations)

        physical = [port for port in ports if (port.get("device") or {}).get("typename") == PHYSICAL_DEVICE]
        if physical and not any(_live_carriers(port) or port_cabled(port) for port in physical):
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' has no live carrier circuit and no cable at "
                f"{', '.join(_port_label(port) for port in physical)} or their parent interfaces: a handoff on a "
                "physical device rides a cross-connect or fibre that terminates on the parent port (a cable when "
                "both ends are in one facility).",
            )

        self._validate_handoff_ends(circuit, name, ports)

    def _validate_handoff_ends(self, circuit: dict[str, Any], name: str, ports: list[dict[str, Any]]) -> None:
        """Each end is a sub-interface, a routed port or a cloud peering interface, of one shape with equal VLANs."""
        shapes = [(port, *_end_shape(port)) for port in ports]
        unshaped = [port for port, shape, _ in shapes if shape is None]
        for port in unshaped:
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' end '{_port_label(port)}' is neither a sub-interface (a "
                "virtual interface with an encapsulation) nor a routed physical port (an IP address, no VLAN) nor a "
                "cloud peering interface: a physical port with no IP address and no VLAN cannot carry a handoff.",
            )
        if unshaped:
            return
        own = {shape for _, shape, _ in shapes if shape != CLOUD_PEERING}
        if len(own) > 1:
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' mixes a {SUB_INTERFACE} and a {ROUTED_PORT} "
                f"({', '.join(f'{_port_label(port)} is a {shape}' for port, shape, _ in shapes)}): both ends are "
                "sub-interfaces of one VLAN, or both are routed ports.",
            )
            return
        vlans = {vlan for _, _, vlan in shapes if vlan is not None}
        if len(vlans) > 1 or (vlans and ROUTED_PORT in own):
            listing = ", ".join(
                f"{_port_label(port)} = {'no VLAN' if vlan is None else vlan}" for port, _, vlan in shapes
            )
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' ends carry different VLANs ({listing}): one VLAN joins the two ends, a "
                "routed port has none.",
            )

    def _validate_handoff_placement(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """The handoff's locations are exactly the facilities its two end devices are deployed in."""
        ends = _ends(circuit)
        if not 1 <= len(locations) <= CIRCUIT_ENDS or len(ends) != CIRCUIT_ENDS:
            return
        deployed = {(deployment or {}).get("id") for _, deployment in ends}
        if {location.get("id") for location in locations} == deployed:
            return
        self._error(
            circuit,
            HANDOFF,
            f"Handoff circuit '{name}' terminates on {_labels(locations)} but its ends are {_ends_label(ends)}: a "
            "handoff's locations are exactly the facilities its two end devices are deployed in.",
        )

    # Vpn circuits

    def _validate_vpn(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        ports = _ports(circuit)
        locations = _locations(circuit)
        self._validate_shape(circuit, VPN, locations, ports, 1)
        self._validate_fields(circuit, VPN, ("registered_prefixes",))
        self._validate_owner(circuit, VPN, locations)
        if not ((circuit.get("vrf") or {}).get("security_zone") or {}).get("name"):
            self._error(
                circuit,
                VPN,
                f"Vpn circuit '{name}' has no vrf: its session runs in an environment routing domain.",
            )

    # Rules across circuits

    def _validate_fields(self, circuit: dict[str, Any], circuit_type: str, forbidden: tuple[str, ...]) -> None:
        """None of the ``forbidden`` fields is set (the schema leaves them optional on the session kind)."""
        present = [field for field in forbidden if circuit.get(field) not in (None, [], {})]
        if present:
            self._error(
                circuit,
                circuit_type,
                f"{NOUNS[circuit_type]} circuit '{_name(circuit)}' sets {', '.join(present)}: not a field of a "
                f"{circuit_type} circuit.",
            )

    def _validate_shape(
        self,
        circuit: dict[str, Any],
        kind: str,
        locations: list[dict[str, Any]],
        ports: list[dict[str, Any]],
        min_locations: int,
    ) -> None:
        """Between ``min_locations`` and two distinct locations, and two end ports."""
        noun = NOUNS[kind]
        if not min_locations <= len(locations) <= CIRCUIT_ENDS:
            expected = "exactly 2" if min_locations == CIRCUIT_ENDS else "1 or 2"
            self._error(
                circuit,
                kind,
                f"{noun} circuit '{_name(circuit)}' terminates on {len(locations)} location(s) "
                f"({_labels(locations)}): a {noun.lower()} circuit has {expected} distinct locations.",
            )
        if len(ports) != CIRCUIT_ENDS:
            self._error(
                circuit,
                kind,
                f"{noun} circuit '{_name(circuit)}' has {len(ports)} end port(s): it terminates on exactly "
                f"{CIRCUIT_ENDS} ports, one per end.",
            )

    def _validate_session_ports(self, circuits: list[tuple[str, dict[str, Any]]]) -> None:
        """A port is an end of at most one session circuit; circuits sharing ports are one finding."""
        by_port: dict[str, tuple[str, list[tuple[str, dict[str, Any]]]]] = {}
        for kind, circuit in circuits:
            for port in _ports(circuit):
                members = by_port.setdefault(str(port.get("id")), (_port_label(port), []))[1]
                if all(other is not circuit for _, other in members):
                    members.append((kind, circuit))
        shared: dict[tuple[str, ...], tuple[list[tuple[str, dict[str, Any]]], list[str]]] = {}
        for label, members in by_port.values():
            if len(members) > 1:
                ordered = sorted(members, key=lambda member: _name(member[1]))
                key = tuple(str(circuit.get("id")) for _, circuit in ordered)
                shared.setdefault(key, (ordered, []))[1].append(label)
        for ordered, labels in shared.values():
            kind, first = ordered[0]
            names = ", ".join(f"'{_name(circuit)}'" for _, circuit in ordered)
            shared_ports = ", ".join(f"'{label}'" for label in sorted(labels))
            self._error(
                first,
                kind,
                f"Circuits {names} all terminate on {shared_ports}: a port is an end of at most one Interconnect, "
                "Handoff or Vpn circuit, one session per address.",
            )

    def _validate_environments(self, circuits: list[tuple[str, dict[str, Any]]]) -> None:
        """Circuits with a port on the same device share an environment class: one finding per device."""
        meeting: dict[str, tuple[str, list[tuple[str, dict[str, Any], str, str]]]] = {}
        for kind, circuit in circuits:
            environment = _environment_class(kind, circuit)
            if environment is None:
                continue
            for device_id, device_name in _devices(circuit).items():
                meeting.setdefault(device_id, (device_name, []))[1].append((kind, circuit, *environment))
        for device_name, members in meeting.values():
            if len({env_class for _, _, env_class, _ in members}) < 2:
                continue
            members.sort(key=lambda member: _name(member[1]))
            listing = ", ".join(
                f"'{_name(circuit)}' ({source}, {env_class})" for _, circuit, env_class, source in members
            )
            kind, first, _, _ = members[0]
            self._error(
                first,
                kind,
                f"Device '{device_name}' carries virtual circuits of different environments: {listing}: prod and "
                "non-prod never connect, so terminate each environment's circuits on its own device.",
            )

    def _error(self, circuit: dict[str, Any], kind: str, message: str) -> None:
        """Log one finding against the circuit."""
        object_type = CARRIER if kind == CARRIER else SESSION_CIRCUIT
        self.log_error(message=message, object_id=circuit.get("id"), object_type=object_type)
