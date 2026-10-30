"""Validate where a circuit terminates and what carries it: locations, owner, carrier and environment.

The schema holds the kinds (``locations`` peers TopologyFacility on both); what it cannot hold is held here. A
carrier circuit is a pure carrier (fibre, cross-connect, internet, a provider virtual connection). A session circuit
is one kind whose role is its ``circuit_type`` (dci, handoff, tunnel), so each type has its own rules, including the
fields only it may carry (the schema leaves them all optional).

Carrier circuits
    1. exactly two distinct locations (the authored cage plus the ends add_circuit derives),
    2. each location is the ``deployment`` of one end's device, one end per location: a DC, ISP or cloud region,
       or for colocation kit the zone (or virtual colocation) the device is deployed to. A sibling zone of the
       same metro is not the end: add_circuit (``plan_locations``) refuses an authored location that differs from
       the end device's deployment. The metro is no facility and no device is deployed to it,
    3. owned exactly when it is an office's access circuit: one ending on a TopologyCustomerOffice is owned by
       that office's owner (add_sdwan_edge's INET-<office>); every other one is shared underlay, unowned.

Tunnel circuit (``circuit_type`` tunnel: office edge to SD-WAN gateway; inventory only, the orchestrator owns the
overlay routing)
    1. exactly two distinct locations and two end ports,
    2. owned by the office's owner: one location is a TopologyCustomerOffice of that owner; the only type with an
       owner and with ``registered_prefixes`` (the others are unowned and have none),
    3. none of the handoff-only fields (``ip_namespace``, ``password``, ``maximum_routes``, ``cloud_endpoint``),
    4. every end port rides a live internet carrier circuit of its own (one with an end on a
       TopologyExternalProviders): the office edge uplink its INET-<office>, the gateway's public port its own
       internet access. The ends share no path, so there is no contiguity rule.

Interconnect circuit (``circuit_type`` dci: between two EVPN Multi-Site border gateways; an underlay and an EVPN
session are built)
    0. none of the handoff-only fields (see the tunnel) and no ``registered_prefixes``,
    1. unowned, exactly two distinct locations and two end ports,
    2. every end port is on a live carrier circuit,
    3. contiguity over the port-derived carrier: the carriers found at the two ports (each once) form one path
       between the two locations, in some order each shares a facility with the next (relationship peers carry no
       order, so every order is tried),
    4. pure fibre: when the carrier is one circuit, its two ends are exactly the circuit's two ports (a carrier that
       terminates on other ports than the circuit names is drift),
    5. each end device carries exactly one ManagedBGP process of role ``overlay`` (keeps a Network Edge router out
       of a DCI: the EVPN session runs between those processes).

Handoff circuit (``circuit_type`` handoff: our edge sub-interface to a party device sub-interface, in one
environment namespace)
    0. unowned, with an ``ip_namespace`` (the schema leaves it optional) and no ``registered_prefixes``; the
       ``cloud_endpoint`` is held by this type only (the schema peer is the session kind),
    1. one or two distinct locations (both ends may sit in one cage) and two end ports; the locations are exactly the
       facilities the two end devices are deployed in (nothing derives them, so a location no end device is deployed
       in, or an end device deployed elsewhere, is drift),
    2. at least one end on a physical device is carried at its port or its parent interface: by a live carrier
       circuit, or by a DcimCable (a hop inside one facility, such as the gateway and the cage edge in one cage, is a
       cable, not a circuit; a cable has no status). An end on a Network Edge appliance or other virtual device has
       none and is not required to,
    3. both end ports are sub-interfaces named ``<port>.<vlan>`` with the same VLAN, and when the circuit has a
       cloud endpoint (a VIF) its ``vlan_id`` is that VLAN.

All session circuits (dci and handoff)
    A port is an end of at most one of them, so two circuits never share the same pair of ports: one session per
    address. A Tunnel is exempt: one gateway port serves many tunnels.

    A decommissioned session circuit is exempt from all of it (its own rules, the one-session-per-port rule and the
    environment rule): add_virtual_circuit builds nothing for it, only cleans what an earlier run built, so it holds
    no port against a live circuit, meets no device and is not reported. A Tunnel is not exempt: add_sdwan_edge
    always writes it ``active`` and no generator reads its status, so it stays an inventory record that is validated
    whatever its status.

Environment class
    Prod and non-prod never connect: circuits meeting at a device share a class, one finding per device. A
    Handoff's class is that of its ``ip_namespace`` (``namespace_type`` prod or non_prod; INTERNET, MANAGEMENT or
    none state no class and are skipped). A Tunnel is PROD: its office is always production. An Interconnect runs
    in the underlay and has none. Several handoffs of one class sharing an on-ramp edge pass.

The Interconnect and Tunnel rules read circuits only: a DCI is a pure-fibre circuit by construction, and a tunnel
rides an internet access circuit.

Contiguity is over the port-derived carrier: the carrier circuits found in the ``capabilities`` of each end port,
or of its parent interface when the port is a sub-interface (``utils.circuit_underlay``); a decommissioned one is
ignored (it neither satisfies "rides a circuit" nor counts in a path). A virtual circuit's underlay is derived,
never listed.

Known limitations (the check sees the final state of the data, nothing more):

- a port removed from a carrier circuit reruns nothing: the virtual circuits that sat on it keep their session
  until their next run,
- a logical port further than one ``parent_interface`` level (a LAG, a sub-interface of a sub-interface) derives
  nothing,
- a virtual circuit created before its ports are linked to a carrier circuit (add_sdwan_edge) is reported if the
  check runs in between,
- a carrier circuit's end count and port kind are checks/carrier_circuit_ports.py's.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import permutations
from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.circuit_underlay import (
    BGP_PROCESS,
    DCI_TYPE,
    DECOMMISSIONED,
    HANDOFF_TYPE,
    OVERLAY_ROLE,
    SESSION_CIRCUIT,
    SESSION_CIRCUIT_TYPES,
    TUNNEL_TYPE,
    derived_underlay,
    overlay_processes,
    port_cabled,
    port_underlay,
    subinterface_vlan,
)
from utils.data_cleaning import clean_data
from utils.exchange_transit import PROD_TYPE, TENANT_NAMESPACE_TYPES
from utils.limits import CIRCUIT_ENDS

CARRIER = "TopologyCarrierCircuit"
# The session circuit types, keyed by their ``circuit_type`` value.
TUNNEL = TUNNEL_TYPE
INTERCONNECT = DCI_TYPE
HANDOFF = HANDOFF_TYPE
CIRCUIT_TYPES = (TUNNEL, INTERCONNECT, HANDOFF)
NOUNS = {TUNNEL: "Tunnel", INTERCONNECT: "Interconnect", HANDOFF: "Handoff"}
# The fields of a handoff alone; the schema leaves them optional on the one session kind.
HANDOFF_ONLY_FIELDS = ("ip_namespace", "password", "maximum_routes", "cloud_endpoint")

INTERNET = "TopologyExternalProviders"
OFFICE = "TopologyCustomerOffice"
PHYSICAL_DEVICE = "DcimPhysicalDevice"


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
    """(``device:port``, the device's deployment) per end port of a carrier circuit."""
    ends = []
    for port in _ports(circuit):
        deployment = (port.get("device") or {}).get("deployment")
        ends.append((_port_label(port), deployment if isinstance(deployment, dict) else None))
    return ends


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


def _vlan(port: dict[str, Any]) -> int | None:
    """The VLAN of an end port named ``<port>.<vlan>``, None for any other name."""
    return subinterface_vlan(str(port.get("name") or ""))


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


def _environment_class(circuit_type: str, circuit: dict[str, Any]) -> tuple[str, str] | None:
    """(class, where it comes from) of a circuit, None when it states none.

    A handoff takes the ``namespace_type`` of its ``ip_namespace`` when that is a tenant VRF (PROD, NON-PROD); a
    tunnel is PROD because its office is always production."""
    if circuit_type == TUNNEL:
        office = next((location for location in _locations(circuit) if location.get("typename") == OFFICE), None)
        return (PROD_TYPE, f"office '{office.get('name')}'") if office else None
    if circuit_type == HANDOFF:
        namespace = circuit.get("ip_namespace") or {}
        if namespace.get("namespace_type") in TENANT_NAMESPACE_TYPES:
            return namespace["namespace_type"], f"namespace '{namespace.get('name')}'"
    return None


class CheckCircuitPlacement(InfrahubCheck):
    """Carrier and session circuits terminate on the right places, owned by the right customer, on the right
    carrier; session circuits meeting at a device share an environment class (prod never meets non-prod)."""

    query = "circuit_placement"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for circuit in cleaned.get(CARRIER) or []:
            self._validate_carrier(circuit)
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
        # add_virtual_circuit builds nothing for a decommissioned dci or handoff: it is not validated, holds no port
        # and meets no device. A tunnel is not generated on either, so it is always validated.
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
        self._validate_session_ports(
            [(circuit_type, circuit) for circuit_type in SESSION_CIRCUIT_TYPES for circuit in live[circuit_type]]
        )
        self._validate_environments(
            [(circuit_type, circuit) for circuit_type in CIRCUIT_TYPES for circuit in live[circuit_type]]
        )

    # ------------------------------------------------------------------
    # Carrier circuits
    # ------------------------------------------------------------------

    def _validate_carrier(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        self._validate_carrier_owner(circuit, name, locations)
        if len(locations) != CIRCUIT_ENDS:
            self._error(
                circuit,
                CARRIER,
                f"Carrier circuit '{name}' terminates on {len(locations)} location(s) ({_labels(locations)}): a "
                f"carrier circuit has exactly {CIRCUIT_ENDS} distinct locations, one facility per end.",
            )
            return

        ends = _ends(circuit)
        if len(ends) != CIRCUIT_ENDS:
            return  # checks/carrier_circuit_ports.py reports the end count
        undeployed = [port for port, deployment in ends if deployment is None]
        for port in undeployed:
            self._error(
                circuit,
                CARRIER,
                f"Carrier circuit '{name}' end '{port}' is on a device with no deployment: set the device's "
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
            CARRIER,
            f"Carrier circuit '{name}' terminates on {_labels(locations)} but its ends are {_ends_label(ends)}: "
            "each location is the deployment of one end's device (a DC, ISP, cloud region, or for colocation kit the "
            "zone or virtual colocation the device is deployed to; not a sibling zone of its metro).",
        )

    def _validate_carrier_owner(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """An office's access circuit is owned by the office's owner; any other carrier circuit is unowned."""
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
                    CARRIER,
                    f"Carrier circuit '{name}' ends on {_label(office)} of '{_owner_org(office_owner)}' but is "
                    f"{ownership}: an office's access circuit is owned by the office's owner.",
                )
        if owner_id and not offices:
            self._error(
                circuit,
                CARRIER,
                f"Carrier circuit '{name}' is owned by '{org}' but ends on no office ({_labels(locations)}): a "
                "carrier circuit is shared underlay, unowned, unless it is an office's access circuit.",
            )

    # ------------------------------------------------------------------
    # Tunnel circuits
    # ------------------------------------------------------------------

    def _validate_tunnel(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        locations = _locations(circuit)
        ports = _ports(circuit)
        self._validate_shape(circuit, TUNNEL, locations, ports, CIRCUIT_ENDS)
        self._validate_fields(circuit, TUNNEL, HANDOFF_ONLY_FIELDS)

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
            found = f"its live carrier circuits are {_carriers_label(live)}" if live else "it is on no live one"
            self._error(
                circuit,
                TUNNEL,
                f"Tunnel circuit '{name}' end '{_port_label(port)}' rides no live internet carrier circuit (one "
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

    # ------------------------------------------------------------------
    # Handoff circuits
    # ------------------------------------------------------------------

    def _validate_handoff(self, circuit: dict[str, Any]) -> None:
        name = _name(circuit)
        ports = _ports(circuit)
        locations = _locations(circuit)
        self._validate_shape(circuit, HANDOFF, locations, ports, 1)
        self._validate_fields(circuit, HANDOFF, ("registered_prefixes",))
        if isinstance(circuit.get("owner"), dict):
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' is owned by '{_owner_org(circuit['owner'])}': a handoff is shared "
                "platform, unowned (the owner of a prefix is the object that registers it).",
            )
        if not (circuit.get("ip_namespace") or {}).get("name"):
            self._error(
                circuit,
                HANDOFF,
                f"Handoff circuit '{name}' has no ip_namespace: its /31 is allocated and its session runs in an "
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

    def _validate_handoff_placement(self, circuit: dict[str, Any], name: str, locations: list[dict[str, Any]]) -> None:
        """The handoff's locations are exactly the facilities its two end devices are deployed in.

        Skipped when the shape is already reported (not 1 or 2 locations, not 2 ports). Unlike a carrier circuit
        nothing derives a handoff's locations, so they are authored and must agree with the devices; both ends may be
        deployed in one cage, which is one location.
        """
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

    # ------------------------------------------------------------------
    # Rules across circuits
    # ------------------------------------------------------------------

    def _validate_fields(self, circuit: dict[str, Any], circuit_type: str, forbidden: tuple[str, ...]) -> None:
        """None of the ``forbidden`` fields is set: the schema leaves every field of the one session kind
        optional, so what only another ``circuit_type`` may carry is refused here."""
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
        """Log one finding against the circuit; ``kind`` is CARRIER or a session circuit_type."""
        object_type = CARRIER if kind == CARRIER else SESSION_CIRCUIT
        self.log_error(message=message, object_id=circuit.get("id"), object_type=object_type)
