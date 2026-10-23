"""Validate where a circuit terminates and what carries it: locations, owner, carrier and environment.

The schema holds the kinds (a physical circuit's ``locations`` peers TopologyFacility); what it cannot hold is
held here. A physical circuit is a pure carrier (fibre, cross-connect, internet); the role of a virtual circuit is
its KIND, so each kind has its own rules.

Physical circuits
    1. exactly two distinct locations (the authored cage plus the ends add_circuit derives),
    2. each location is the ``deployment`` of one end's device, one end per location: a DC, ISP or cloud region,
       or for colocation kit the zone (or virtual colocation) the device is deployed to. A sibling zone of the
       same metro is not the end: add_circuit (``plan_locations``) refuses an authored location that differs from
       the end device's deployment. The metro is no facility and no device is deployed to it,
    3. owned exactly when it is an office's access circuit: one ending on a TopologyCustomerOffice is owned by
       that office's owner (add_sdwan_edge's INET-<office>); every other one is shared underlay, unowned.

Tunnel circuit (office edge to SD-WAN gateway; inventory only, the orchestrator owns the overlay routing)
    1. exactly two distinct locations and two end ports,
    2. owned by the office's owner: one location is a TopologyCustomerOffice of that owner,
    3. every end port rides a live internet physical circuit of its own (one with an end on a
       TopologyExternalProviders): the office edge uplink its INET-<office>, the gateway's public port its own
       internet access. The ends share no path, so there is no contiguity rule.

Interconnect circuit (DCI between two EVPN Multi-Site border gateways; an underlay and an EVPN session are built)
    1. unowned, exactly two distinct locations and two end ports,
    2. every end port is on a live physical circuit,
    3. contiguity over the port-derived carrier: the carriers found at the two ports (each once) form one path
       between the two locations, in some order each shares a facility with the next (relationship peers carry no
       order, so every order is tried),
    4. pure fibre: when the carrier is one circuit, its two ends are exactly the circuit's two ports (a carrier that
       terminates on other ports than the circuit names is drift),
    5. each end device carries exactly one ManagedBGP process of role ``overlay`` (keeps a Network Edge router out
       of a DCI: the EVPN session runs between those processes).

Handoff circuit (our edge sub-interface to a party device sub-interface, in one environment namespace)
    1. one or two distinct locations (both ends may sit in one cage) and two end ports,
    2. at least one end on a physical device is carried at its port or its parent interface: by a live physical
       circuit, or by a DcimCable (a hop inside one facility, such as the gateway and the cage edge in one cage, is a
       cable, not a circuit; a cable has no status). An end on a Network Edge appliance or other virtual device has
       none and is not required to,
    3. both end ports are sub-interfaces named ``<port>.<vlan>`` with the same VLAN, and when the circuit has a
       cloud endpoint (a VIF) its ``vlan_id`` is that VLAN.

All session circuits (Interconnect and Handoff)
    A port is an end of at most one of them, so two circuits never share the same pair of ports: one session per
    address. A Tunnel is exempt: one gateway port serves many tunnels.

Environment class
    Prod and non-prod never connect: circuits meeting at a device share a class, one finding per device. A
    Handoff's class is that of its ``ip_namespace`` (``namespace_type`` prod or non_prod; INTERNET, MANAGEMENT or
    none state no class and are skipped). A Tunnel is PROD: its office is always production. An Interconnect runs
    in the underlay and has none. Several handoffs of one class sharing an on-ramp edge pass.

The Interconnect and Tunnel rules read circuits only: a DCI is a pure-fibre circuit by construction, and a tunnel
rides an internet access circuit.

Contiguity is over the port-derived carrier: the physical circuits found in the ``capabilities`` of each end port,
or of its parent interface when the port is a sub-interface (``utils.circuit_underlay``); a decommissioned one is
ignored (it neither satisfies "rides a circuit" nor counts in a path). A virtual circuit's underlay is derived,
never listed.

Known limitations (the check sees the final state of the data, nothing more):

- a port removed from a physical circuit reruns nothing: the virtual circuits that sat on it keep their session
  until their next run,
- a logical port further than one ``parent_interface`` level (a LAG, a sub-interface of a sub-interface) derives
  nothing,
- a virtual circuit created before its ports are linked to a physical circuit (add_sdwan_edge) is reported if the
  check runs in between,
- a physical circuit's end count and port kind are checks/physical_circuit_ports.py's.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import permutations
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.circuit_underlay import DECOMMISSIONED, derived_underlay, port_cabled, port_underlay
from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, TENANT_NAMESPACE_TYPES

PHYSICAL = "TopologyPhysicalCircuit"
TUNNEL = "TopologyTunnelCircuit"
INTERCONNECT = "TopologyInterconnectCircuit"
HANDOFF = "TopologyHandoffCircuit"
VIRTUAL_KINDS = (TUNNEL, INTERCONNECT, HANDOFF)
NOUNS = {TUNNEL: "Tunnel", INTERCONNECT: "Interconnect", HANDOFF: "Handoff"}
SESSION_KINDS = (INTERCONNECT, HANDOFF)
"""The kinds a session is generated on: a port is an end of at most one of them."""

CIRCUIT_ENDS = 2
"""A circuit terminates on exactly two places and two ports, one per end."""
INTERNET = "TopologyExternalProviders"
OFFICE = "TopologyCustomerOffice"
PHYSICAL_DEVICE = "DcimPhysicalDevice"
BGP = "ManagedBGP"
OVERLAY = "overlay"


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


def _end_at(location: dict[str, Any], deployment: dict[str, Any] | None) -> bool:
    """The location is the end device's deployment itself. Colocation kit is deployed per zone or virtual
    colocation, and add_circuit (plan_locations) refuses an authored location that differs from it: no sibling
    cage of the same metro, never the metro."""
    return bool(deployment) and bool(location.get("id")) and location.get("id") == (deployment or {}).get("id")


def _locations(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The circuit's locations, each once."""
    seen: dict[str, dict[str, Any]] = {}
    for location in circuit.get("locations") or []:
        if isinstance(location, dict):
            seen.setdefault(str(location.get("id")), location)
    return list(seen.values())


def _ports(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The circuit's own ports (``capabilities``)."""
    return [port for port in circuit.get("capabilities") or [] if isinstance(port, dict)]


def _port_label(port: dict[str, Any]) -> str:
    device = port.get("device") or {}
    return f"{device.get('name') or '<unknown device>'}:{port.get('name') or port.get('id')}"


def _ends(circuit: dict[str, Any]) -> list[tuple[str, dict[str, Any] | None]]:
    """(``device:port``, the device's deployment) per end port of a physical circuit."""
    ends = []
    for port in _ports(circuit):
        deployment = (port.get("device") or {}).get("deployment")
        ends.append((_port_label(port), deployment if isinstance(deployment, dict) else None))
    return ends


def _ends_label(ends: list[tuple[str, dict[str, Any] | None]]) -> str:
    return ", ".join(f"{port} in {_label(deployment)}" for port, deployment in ends) or "no end ports"


def _live_carriers(port: dict[str, Any]) -> list[dict[str, Any]]:
    """The physical circuits at one port (or its parent interface), a decommissioned one ignored."""
    return [carrier for carrier in port_underlay(port) if carrier.get("status") != DECOMMISSIONED]


def _live_underlay(circuit: dict[str, Any]) -> list[dict[str, Any]]:
    """The live carriers of all the circuit's ports, each once."""
    return [carrier for carrier in derived_underlay(circuit) if carrier.get("status") != DECOMMISSIONED]


def _carriers_label(carriers: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{_name(carrier)} ({' - '.join(str(end.get('name')) for end in _locations(carrier))})" for carrier in carriers
    )


def _vlan(port: dict[str, Any]) -> int | None:
    """The VLAN of a sub-interface named ``<port>.<vlan>``, None for any other name."""
    _, dot, suffix = str(port.get("name") or "").rpartition(".")
    return int(suffix) if dot and suffix.isdigit() else None


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
    """Some order of ALL hops starts at ``start_id``, joins each hop to the next on a shared facility and
    finishes on a place ``arrived`` accepts."""

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


def _environment_class(kind: str, circuit: dict[str, Any]) -> tuple[str, str] | None:
    """(class, where it comes from) of a circuit, None when it states none.

    A handoff takes the ``namespace_type`` of its ``ip_namespace`` when that is a tenant VRF (PROD, NON-PROD); a
    tunnel is PROD because its office is always production."""
    if kind == TUNNEL:
        office = next((location for location in _locations(circuit) if location.get("typename") == OFFICE), None)
        return (PROD_TYPE, f"office '{office.get('name')}'") if office else None
    if kind == HANDOFF:
        namespace = circuit.get("ip_namespace") or {}
        if namespace.get("namespace_type") in TENANT_NAMESPACE_TYPES:
            return namespace["namespace_type"], f"namespace '{namespace.get('name')}'"
    return None


class CheckCircuitPlacement(InfrahubCheck):
    """Physical and virtual circuits terminate on the right places, owned by the right customer, on the right
    carrier; virtual circuits meeting at a device share an environment class (prod never meets non-prod)."""

    query = "circuit_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for circuit in cleaned.get(PHYSICAL) or []:
            self._validate_physical(circuit)
        virtual = {kind: cleaned.get(kind) or [] for kind in VIRTUAL_KINDS}
        for circuit in virtual[TUNNEL]:
            self._validate_tunnel(circuit)
        for circuit in virtual[INTERCONNECT]:
            self._validate_interconnect(circuit)
        for circuit in virtual[HANDOFF]:
            self._validate_handoff(circuit)
        self._validate_session_ports([(kind, circuit) for kind in SESSION_KINDS for circuit in virtual[kind]])
        self._validate_environments([(kind, circuit) for kind in VIRTUAL_KINDS for circuit in virtual[kind]])

    # ------------------------------------------------------------------
    # Physical circuits
    # ------------------------------------------------------------------

    def _validate_physical(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        self._validate_physical_owner(circuit, name, locations)
        if len(locations) != CIRCUIT_ENDS:
            self._error(
                circuit,
                PHYSICAL,
                f"Physical circuit '{name}' terminates on {len(locations)} location(s) ({_labels(locations)}): a "
                f"physical circuit has exactly {CIRCUIT_ENDS} distinct locations, one facility per end.",
            )
            return

        ends = _ends(circuit)
        if len(ends) != CIRCUIT_ENDS:
            return  # checks/physical_circuit_ports.py reports the end count
        undeployed = [port for port, deployment in ends if deployment is None]
        for port in undeployed:
            self._error(
                circuit,
                PHYSICAL,
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
            PHYSICAL,
            f"Physical circuit '{name}' terminates on {_labels(locations)} but its ends are {_ends_label(ends)}: "
            "each location is the deployment of one end's device (a DC, ISP, cloud region, or for colocation kit the "
            "zone or virtual colocation the device is deployed to; not a sibling zone of its metro).",
        )

    def _validate_physical_owner(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """An office's access circuit is owned by the office's owner; any other physical circuit is unowned."""
        owner = circuit.get("owner") if isinstance(circuit.get("owner"), dict) else None
        owner_id = (owner or {}).get("id")
        org = _owner_org(owner)
        ownership = f"owned by '{org}'" if owner_id else "unowned"
        offices = [location for location in locations if location.get("typename") == OFFICE]
        for office in offices:
            office_owner = office.get("owner") or {}
            if office_owner.get("id") != owner_id:
                self._error(
                    circuit,
                    PHYSICAL,
                    f"Physical circuit '{name}' ends on {_label(office)} of '{_owner_org(office_owner)}' but is "
                    f"{ownership}: an office's access circuit is owned by the office's owner.",
                )
        if owner_id and not offices:
            self._error(
                circuit,
                PHYSICAL,
                f"Physical circuit '{name}' is owned by '{org}' but ends on no office ({_labels(locations)}): a "
                "physical circuit is shared underlay, unowned, unless it is an office's access circuit.",
            )

    # ------------------------------------------------------------------
    # Tunnel circuits
    # ------------------------------------------------------------------

    def _validate_tunnel(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        ports = _ports(circuit)
        self._validate_shape(circuit, TUNNEL, locations, ports, CIRCUIT_ENDS)

        owner = circuit.get("owner") if isinstance(circuit.get("owner"), dict) else None
        offices = [location for location in locations if location.get("typename") == OFFICE]
        owner_id = (owner or {}).get("id")
        if not owner_id or not any((office.get("owner") or {}).get("id") == owner_id for office in offices):
            owned = f"owned by '{_owner_org(owner)}'" if owner_id else "unowned"
            found = (
                "its office is " + ", ".join(f"{_label(o)} of '{_owner_org(o.get('owner'))}'" for o in offices)
                if offices
                else f"none of its locations ({_labels(locations)}) is an {OFFICE}"
            )
            self._error(
                circuit,
                TUNNEL,
                f"Tunnel circuit '{name}' is {owned} but {found}: a tunnel is owned by its office's owner.",
            )

        for port in ports:
            live = _live_carriers(port)
            if any(location.get("typename") == INTERNET for carrier in live for location in _locations(carrier)):
                continue
            found = f"its live physical circuits are {_carriers_label(live)}" if live else "it is on no live one"
            self._error(
                circuit,
                TUNNEL,
                f"Tunnel circuit '{name}' end '{_port_label(port)}' rides no live internet physical circuit (one "
                f"with an end on a {INTERNET}): {found}. Every end of a tunnel has its own internet access.",
            )

    # ------------------------------------------------------------------
    # Interconnect circuits
    # ------------------------------------------------------------------

    def _validate_interconnect(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        ports = _ports(circuit)
        if isinstance(circuit.get("owner"), dict):
            self._error(
                circuit,
                INTERCONNECT,
                f"Interconnect circuit '{name}' is owned by '{_owner_org(circuit['owner'])}': a DCI between two "
                "border gateways is shared platform, unowned.",
            )
        self._validate_shape(circuit, INTERCONNECT, locations, ports, CIRCUIT_ENDS)
        self._validate_overlay_processes(circuit, name, ports)

        uncarried = [port for port in ports if not _live_carriers(port)]
        for port in uncarried:
            self._error(
                circuit,
                INTERCONNECT,
                f"Interconnect circuit '{name}' end '{_port_label(port)}' is on no live physical circuit: terminate "
                "it on a port cabled through a physical circuit.",
            )
        if uncarried or len(locations) != CIRCUIT_ENDS:
            return

        carriers = _live_underlay(circuit)
        hops = [_locations(carrier) for carrier in carriers]
        if any(len(hop) != CIRCUIT_ENDS for hop in hops):
            return  # that physical circuit's own location count is reported against it

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
                    "physical circuit's two ends are the circuit's two ports.",
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
            f"{_label(first)} to {_label(second)}: in some order, each physical circuit shares a facility with the "
            "next, the first starts and the last finishes at the path's ends.",
        )

    def _validate_overlay_processes(self, circuit: dict[str, Any], name: str, ports: list[dict[str, Any]]) -> None:
        """Each end device runs exactly one overlay BGP process: the EVPN session is built between them."""
        devices = {str(port["device"]["id"]): port["device"] for port in ports if (port.get("device") or {}).get("id")}
        for device in devices.values():
            overlay = [
                process
                for process in device.get("capabilities") or []
                if isinstance(process, dict)
                and process.get("typename") == BGP
                and process.get("process_role") == OVERLAY
            ]
            if len(overlay) != 1:
                self._error(
                    circuit,
                    INTERCONNECT,
                    f"Interconnect circuit '{name}' end device '{device.get('name')}' has {len(overlay)} {BGP} "
                    f"process(es) of process_role '{OVERLAY}': a border gateway runs exactly one.",
                )

    # ------------------------------------------------------------------
    # Handoff circuits
    # ------------------------------------------------------------------

    def _validate_handoff(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        ports = _ports(circuit)
        self._validate_shape(circuit, HANDOFF, _locations(circuit), ports, 1)

        physical = [port for port in ports if (port.get("device") or {}).get("typename") == PHYSICAL_DEVICE]
        if physical and not any(_live_carriers(port) or port_cabled(port) for port in physical):
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' has no live physical circuit and no cable at "
                f"{', '.join(_port_label(port) for port in physical)} or their parent interfaces: a handoff on a "
                "physical device rides a cross-connect or fibre that terminates on the parent port (a cable when "
                "both ends are in one facility).",
            )

        vlans = {_vlan(port) for port in ports}
        if None in vlans:
            for port in ports:
                if _vlan(port) is None:
                    self._error(
                        circuit,
                        HANDOFF,
                        f"Handoff circuit '{name}' end '{_port_label(port)}' is not a VLAN sub-interface: a handoff "
                        "end is named <port>.<vlan>.",
                    )
            return
        if len(vlans) > 1:
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' ends carry different VLANs "
                f"({', '.join(f'{_port_label(port)} = {_vlan(port)}' for port in ports)}): one VLAN joins the two "
                "sub-interfaces.",
            )
            return
        endpoint = circuit.get("cloud_endpoint") or {}
        if vlans and endpoint.get("vlan_id") is not None and endpoint["vlan_id"] not in vlans:
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' ends are on VLAN {next(iter(vlans))} but its cloud endpoint "
                f"'{endpoint.get('name')}' states vlan_id {endpoint['vlan_id']}: the virtual interface mirrors the "
                "sub-interface VLAN.",
            )

    # ------------------------------------------------------------------
    # Rules across circuits
    # ------------------------------------------------------------------

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
        """A port is an end of at most one session circuit (Interconnect, Handoff). Circuits sharing ports are one
        finding per set of circuits, naming every shared port, so two circuits on the same pair of ports are one."""
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
                f"Circuits {names} all terminate on {shared_ports}: a port is an end of at most one Interconnect "
                "or Handoff circuit, one session per address.",
            )

    def _validate_environments(self, circuits: list[tuple[str, dict[str, Any]]]) -> None:
        """Circuits with a port on the same device are of one environment class: prod and non-prod never connect.
        One finding per conflicting device, against its first circuit by name, naming every circuit."""
        # device id -> (device name, [(kind, circuit, class, source)]), each circuit once per device
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
        self.log_error(message=message, object_id=circuit.get("id"), object_type=kind)
