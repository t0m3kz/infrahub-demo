"""The pure half of add_provider_connection: parse the query payload, validate it, plan the links.

Nothing here calls the client. ``parse_connection`` reads the cleaned payload of
``queries/topology/add/provider_connection.gql``, ``plan_links`` decides everything that can be decided from it (the
flavour, the ends of each link, the link prefix of each session) and raises ``ValueError`` for what is wrong, so the
generator refuses a spec before its first write. The VLAN and the tunnel names depend on what the devices already
hold, so the generator reads that under its locks and asks :func:`choose_vlan` / :func:`choose_tunnel_name`.

The connection is a service request: it names the facilities, never the ports. Where the two ends of a link come from:

- dedicated: :class:`DedicatedEnds`, decided by the generator (generators/helpers/near_port_facts.py, which reads the
  facts the choice needs and calls generators/helpers/near_port.py): the near port (the one the connection's own
  carrier ends on, else the explicit ``near_port``, else a free port of the right role), the provider device of the far
  facility (:func:`choose_stub`) and its far port (the far end of the reused carrier, or ``None``: a virtual NIC the
  generator creates). One link: a dedicated connection is ONE leg (a primary and a secondary leg are two connections).
- internet_vpn: no sub-interface, no carrier. The near device and port are an end of one of the near facility's own
  internet carrier circuits: the one whose port is the explicit ``near_port``, else (no ``near_port``) the one device
  that ends one (several edges need ``near_port`` to say which one the tunnels leave by). The tunnel rides on that
  carrier's port (``parent_interface``); ``near_port`` is never a dedicated port selection for a VPN. The far device is the stub of the far facility (:func:`choose_stub`).

The stub carries ONE ManagedBGP process. An authored one is reused (its AS must be ``provider_as``); a stub without
one gets it from the generator (generators/provider_stub.py), which needs the connection's ``provider_as`` for the AS:
such a link plans ``far_process_id = None``. The stub is the one whose process speaks ``provider_as`` when several are
deployed in the far facility, else the one without a process.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any

from utils.circuit_underlay import DECOMMISSIONED, HANDOFF_TYPE, subinterface_vlan
from utils.provider_services import (
    ADDRESS_SOURCE_PROVIDER,
    PATH_DEDICATED,
    PATH_TYPES,
    ConnectionFlavour,
    connection_provider,
    flavour_for,
)

VPN_TYPE = "vpn"
INTERNET_CARRIER = "internet"
# The far facility kinds the generator can land a carrier on by creating the far end itself: a virtual NIC on the
# service-edge appliance of the region. A virtual colocation is ordered by us elsewhere: its carrier must exist.
NIC_FAR_KINDS = ("TopologyCloudRegion", "TopologySaasRegion")
ORDERED_FAR_KIND = "TopologyVirtualColocation"
NIC_PREFIX = "vnic-"
# A VLAN a generated sub-interface may take: 1 is the default VLAN, 4095 is reserved.
VLAN_MIN = 2
VLAN_MAX = 4094
# The tunnel interfaces a device may carry for provider connections: Tunnel1 .. Tunnel{TUNNEL_MAX}.
TUNNEL_MAX = 256
_TUNNEL_NAME = re.compile(r"Tunnel(\d+)")


def _id(node: Any) -> str | None:
    """The id of a cleaned relationship: a node that selected only ``id`` is the id string itself."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return node.get("id")
    return None


def _dict(value: Any) -> dict[str, Any]:
    """A cleaned relationship as a dict: a node that selected only ``id`` (or is empty) has no fields to read."""
    return value if isinstance(value, dict) else {}


def _ids(nodes: Any) -> frozenset[str]:
    """The ids of a cleaned relationship list."""
    return frozenset(i for i in (_id(node) for node in nodes or []) if i)


@dataclass(frozen=True)
class BgpProcess:
    """A ManagedBGP process of a device."""

    id: str
    name: str
    local_as_id: str | None


@dataclass(frozen=True)
class Port:
    """An interface with its device, the device's deployment and BGP processes."""

    id: str
    name: str
    device_id: str
    device_name: str
    deployment_id: str | None
    processes: tuple[BgpProcess, ...]

    @property
    def label(self) -> str:
        return f"{self.device_name}/{self.name}"


@dataclass(frozen=True)
class Stub:
    """A device deployed in the far facility (the provider's kit) with its BGP processes."""

    id: str
    name: str
    processes: tuple[BgpProcess, ...]


@dataclass(frozen=True)
class Carrier:
    """A carrier circuit of the connection (or an internet carrier of its near facility) with its ends."""

    id: str
    name: str
    circuit_type: str
    status: str
    ends: tuple[Port, ...]


@dataclass(frozen=True)
class SessionEnd:
    """An end of a session circuit that already exists: the interface, its name, its device."""

    id: str
    name: str
    device_id: str | None


@dataclass(frozen=True)
class ExistingSession:
    """A session circuit already on the connection, as the query read it."""

    id: str
    name: str
    circuit_type: str
    status: str
    provider_id: str | None
    namespace_id: str | None
    connection_id: str | None
    link_prefix_id: str | None
    password_id: str | None
    location_ids: frozenset[str]
    ends: tuple[SessionEnd, ...]
    groups: frozenset[str]


@dataclass(frozen=True)
class ConnectionSpec:
    """The connection as the generator reads it."""

    id: str
    name: str
    path_type: str
    status: str
    vlan_id: int | None
    bandwidth: int | None
    near_port_id: str | None
    far_kind: str
    namespace_id: str | None
    provider_id: str | None
    provider_name: str | None
    profile: str | None
    provider_as_id: str | None
    password_id: str | None
    near_id: str | None
    far_id: str | None
    link_prefixes: tuple[tuple[str, str], ...]
    carriers: tuple[Carrier, ...]  # the connection's own carrier_circuits (its output of an earlier run)
    near_internet: tuple[Carrier, ...]
    near_devices: tuple[tuple[str, str], ...]  # (id, name) of the devices the near facility hosts
    near_load: tuple[frozenset[str], ...]  # per OTHER connection of the near facility: the ids of its end devices
    stubs: tuple[Stub, ...]
    sessions: tuple[ExistingSession, ...]

    @property
    def near_device_ids(self) -> frozenset[str]:
        return frozenset(device_id for device_id, _ in self.near_devices)

    @property
    def stub_ids(self) -> frozenset[str]:
        return frozenset(stub.id for stub in self.stubs)

    @property
    def creates_carrier_ends(self) -> bool:
        """The generator can create the far end (and so the carrier) itself; False: the carrier must already exist."""
        return self.far_kind != ORDERED_FAR_KIND


@dataclass(frozen=True)
class Side:
    """One side of a link: its device and, for a dedicated link, the port the sub-interface rides."""

    device_id: str
    device_name: str
    port: Port | None


@dataclass(frozen=True)
class DedicatedEnds:
    """The ends of a dedicated link, as decided by the generator.

    ``far`` is None while the virtual NIC of the service edge is still to be created (:func:`nic_name`).
    """

    near: Port
    far_device: Stub
    far: Port | None


@dataclass(frozen=True)
class LinkPlan:
    """One session circuit of the connection."""

    index: int
    name: str
    circuit_type: str
    near: Side
    far: Side
    link_prefix_id: str | None
    far_process_id: str | None  # None: the stub has no process yet, the generator builds it (provider_stub.py)


def _processes(device: dict[str, Any]) -> tuple[BgpProcess, ...]:
    """The ManagedBGP processes among a cleaned device's ``capabilities``."""
    return tuple(
        BgpProcess(id=cap["id"], name=cap.get("name") or "", local_as_id=_id(cap.get("local_as")))
        for cap in device.get("capabilities") or []
        if isinstance(cap, dict) and cap.get("typename") == "ManagedBGP" and cap.get("id")
    )


def _port(raw: Any) -> Port | None:
    """A cleaned interface with its device; None for an empty slot or an interface without a device."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    device = raw.get("device")
    if not isinstance(device, dict) or not device.get("id"):
        return None
    return Port(
        id=raw["id"],
        name=raw.get("name") or "?",
        device_id=device["id"],
        device_name=device.get("name") or "?",
        deployment_id=_id(device.get("deployment")),
        processes=_processes(device),
    )


def _carrier(raw: dict[str, Any]) -> Carrier:
    ends: dict[str, Port] = {}
    for end in raw.get("capabilities") or []:
        port = _port(end)
        if port is not None:
            ends.setdefault(port.id, port)
    return Carrier(
        id=raw["id"],
        name=raw.get("name") or raw["id"],
        circuit_type=raw.get("circuit_type") or "",
        status=raw.get("status") or "",
        ends=tuple(ends.values()),
    )


def _session(raw: dict[str, Any]) -> ExistingSession:
    ends = tuple(
        SessionEnd(id=end["id"], name=end.get("name") or "", device_id=_id(end.get("device")))
        for end in raw.get("capabilities") or []
        if isinstance(end, dict) and end.get("id")
    )
    groups = frozenset(
        group["name"] for group in raw.get("member_of_groups") or [] if isinstance(group, dict) and group.get("name")
    )
    return ExistingSession(
        id=raw["id"],
        name=raw.get("name") or "",
        circuit_type=raw.get("circuit_type") or "",
        status=raw.get("status") or "",
        provider_id=_id(raw.get("provider")),
        namespace_id=_id(raw.get("ip_namespace")),
        connection_id=_id(raw.get("connection")),
        link_prefix_id=_id(raw.get("link_prefix")),
        password_id=_id(raw.get("password")),
        location_ids=_ids(raw.get("locations")),
        ends=ends,
        groups=groups,
    )


def _stubs(far: dict[str, Any]) -> tuple[Stub, ...]:
    """The appliances (a cloud or SaaS region, a virtual colocation) and the devices (an external provider, a
    colocation zone) deployed in the far facility; a region has no ``devices`` key."""
    stubs: dict[str, Stub] = {}
    for key in ("appliances", "devices"):
        for device in far.get(key) or []:
            if isinstance(device, dict) and device.get("id"):
                stubs.setdefault(
                    device["id"],
                    Stub(
                        id=device["id"],
                        name=device.get("name") or "?",
                        processes=_processes(device),
                    ),
                )
    return tuple(stubs.values())


def parse_connection(raw: dict[str, Any]) -> ConnectionSpec:
    """The spec of a cleaned TopologyProviderConnection payload; ValueError when it lacks its required edges."""
    name = raw.get("name") or ""
    near = _dict(raw.get("near"))
    far = _dict(raw.get("far"))
    provider = connection_provider(raw) or {}
    namespace = raw.get("ip_namespace")
    carriers = tuple(_carrier(c) for c in raw.get("carrier_circuits") or [] if isinstance(c, dict) and c.get("id"))
    stubs = _stubs(far)
    near_load = tuple(
        frozenset(
            device_id
            for key in ("carrier_circuits", "session_circuits")
            for circuit in other.get(key) or []
            if isinstance(circuit, dict)
            for end in circuit.get("capabilities") or []
            if isinstance(end, dict) and (device_id := _id(end.get("device")))
        )
        for other in near.get("near_connections") or []
        if isinstance(other, dict) and other.get("id") != raw["id"]
    )
    near_circuits = [
        circuit
        for circuit in near.get("circuits") or []
        if isinstance(circuit, dict) and circuit.get("typename") == "TopologyCarrierCircuit"
    ]
    prefixes = tuple(
        (prefix["id"], prefix.get("prefix") or "")
        for prefix in raw.get("link_prefixes") or []
        if isinstance(prefix, dict) and prefix.get("id")
    )
    return ConnectionSpec(
        id=raw["id"],
        name=name,
        path_type=raw.get("path_type") or "",
        status=raw.get("status") or "",
        vlan_id=raw.get("vlan_id"),
        bandwidth=raw.get("bandwidth"),
        near_port_id=_id(raw.get("near_port")),
        far_kind=far.get("typename") or "",
        namespace_id=_id(namespace),
        provider_id=provider.get("id"),
        provider_name=provider.get("name"),
        profile=provider.get("connection_profile"),
        provider_as_id=_id(raw.get("provider_as")),
        password_id=_id(raw.get("password")),
        near_id=_id(near),
        far_id=_id(far),
        link_prefixes=prefixes,
        carriers=carriers,
        near_internet=tuple(
            _carrier(c) for c in near_circuits if c.get("id") and c.get("circuit_type") == INTERNET_CARRIER
        ),
        near_devices=tuple(
            (device["id"], device.get("name") or "?")
            for device in near.get("devices") or []
            if isinstance(device, dict) and device.get("id")
        ),
        near_load=near_load,
        stubs=stubs,
        sessions=tuple(_session(s) for s in raw.get("session_circuits") or [] if isinstance(s, dict) and s.get("id")),
    )


def _port_order(port: Port) -> tuple[str, str]:
    return (port.device_name, port.name)


def _carrier_ends(carriers: Iterable[Carrier], facility_id: str) -> list[Port]:
    """The ends, in carrier-name order, of the live carriers whose device is deployed in the facility."""
    seen: dict[str, Port] = {}
    for carrier in sorted(carriers, key=lambda c: c.name):
        if carrier.status == DECOMMISSIONED:
            continue
        for port in sorted(carrier.ends, key=_port_order):
            if port.deployment_id == facility_id:
                seen.setdefault(port.id, port)
    return list(seen.values())


def _single_process(
    device_name: str, processes: tuple[BgpProcess, ...], provider_as_id: str | None
) -> BgpProcess | None:
    """The one ManagedBGP process of a provider stub, consistent with the connection's ``provider_as``.

    None when the stub has none and the connection names the AS to build it with.
    """
    if not processes:
        if not provider_as_id:
            raise ValueError(
                f"{device_name} has no ManagedBGP process and the connection has no provider_as to build it with: "
                "set provider_as or author the stub's process"
            )
        return None
    if len(processes) > 1:
        raise ValueError(f"{device_name} has {len(processes)} ManagedBGP processes, the stub needs exactly one")
    process = processes[0]
    if provider_as_id and process.local_as_id != provider_as_id:
        raise ValueError(f"{device_name}: the process {process.name} does not speak the connection's provider_as")
    return process


def pair_link_prefixes(flavour: ConnectionFlavour, prefixes: tuple[tuple[str, str], ...]) -> list[str | None]:
    """The link prefix id of each link: provider-assigned ranges sorted by address, none for our pool's flavour.

    ValueError when the number of ranges is not the flavour's link count or a range is not of the flavour's length.
    """
    if flavour.address_source != ADDRESS_SOURCE_PROVIDER:
        return [None] * flavour.links
    if len(prefixes) != flavour.links:
        raise ValueError(f"{flavour.links} link_prefixes expected, found {len(prefixes)}")
    ordered = sorted(prefixes, key=lambda item: ipaddress.ip_network(item[1], strict=False))
    for _, text in ordered:
        if ipaddress.ip_network(text, strict=False).prefixlen != flavour.prefix_length:
            raise ValueError(f"link prefix {text} is not a /{flavour.prefix_length}")
    return [prefix_id for prefix_id, _ in ordered]


def circuit_type_of(path_type: str) -> str:
    """The session circuit type of a path: ``handoff`` for a dedicated path, ``vpn`` over the internet."""
    return HANDOFF_TYPE if path_type == PATH_DEDICATED else VPN_TYPE


def stubs_to_build(links: Iterable[LinkPlan]) -> list[Side]:
    """The far sides that have no BGP process yet, once per device, in device id order."""
    return sorted(
        {link.far.device_id: link.far for link in links if link.far_process_id is None}.values(),
        key=lambda s: s.device_id,
    )


def validate_spec(spec: ConnectionSpec) -> tuple[ConnectionFlavour, list[str | None]]:
    """The flavour of the connection and the link prefix id of each of its links; ValueError for a spec that cannot
    be built whatever the ports are."""
    if spec.path_type not in PATH_TYPES:
        raise ValueError(f"unknown path_type '{spec.path_type}'")
    if not spec.near_id or not spec.far_id:
        raise ValueError("a connection needs its near and far facility")
    if spec.near_id == spec.far_id:
        raise ValueError("near and far are the same facility")
    if not spec.namespace_id:
        raise ValueError("a connection needs its ip_namespace")
    if not spec.provider_id:
        raise ValueError(
            "no provider: set the account (a cloud), or deploy the far facility under a provider "
            "(external provider, SaaS region) or bill it to an account"
        )
    flavour = flavour_for(spec.profile, spec.path_type)
    if spec.path_type == PATH_DEDICATED and flavour.links != 1:
        raise ValueError(
            f"a dedicated connection is one leg, the provider flavour has {flavour.links} links: "
            "model each leg as its own connection"
        )
    return flavour, pair_link_prefixes(flavour, spec.link_prefixes)


def plan_links(spec: ConnectionSpec, ends: DedicatedEnds | None = None) -> tuple[ConnectionFlavour, list[LinkPlan]]:
    """The flavour and the links of a connection; ValueError for anything that cannot be built.

    A dedicated connection needs the ``ends`` the generator decided (:class:`DedicatedEnds`); a VPN finds its own.
    Every failure here precedes the first write of the generator.
    """
    flavour, prefix_ids = validate_spec(spec)
    circuit_type = circuit_type_of(spec.path_type)
    if spec.path_type == PATH_DEDICATED:
        if ends is None:
            raise ValueError("a dedicated connection needs its near and far ends")
        sides = _dedicated_sides(spec, ends)
    else:
        sides = _vpn_sides(spec, flavour)
    links = []
    for index, (near, far, process) in enumerate(sides, start=1):
        if near.device_id == far.device_id:
            raise ValueError(f"link {index}: both sides are on {near.device_name}")
        links.append(
            LinkPlan(
                index=index,
                name=f"{spec.name}-L{index}",
                circuit_type=circuit_type,
                near=near,
                far=far,
                link_prefix_id=prefix_ids[index - 1],
                far_process_id=process.id if process else None,
            )
        )
    return flavour, links


def _dedicated_sides(spec: ConnectionSpec, ends: DedicatedEnds) -> list[tuple[Side, Side, BgpProcess | None]]:
    """The (near, far, far process) of the one link, from the ends the generator decided."""
    stub = ends.far_device
    process = _single_process(stub.name, stub.processes, spec.provider_as_id)
    return [(Side(ends.near.device_id, ends.near.device_name, ends.near), Side(stub.id, stub.name, ends.far), process)]


def bind_far_port(links: list[LinkPlan], port: Port) -> list[LinkPlan]:
    """The links with the far port that was still to be created."""
    return [replace(link, far=replace(link.far, port=port)) for link in links]


def nic_name(connection_name: str) -> str:
    """The virtual NIC of the service edge that ends the carrier of a connection."""
    return f"{NIC_PREFIX}{connection_name}"


def choose_stub(spec: ConnectionSpec) -> Stub:
    """The provider device of the far facility that terminates the connection.

    Among the devices whose ManagedBGP process speaks ``provider_as`` when the connection names it (else among those with
    a process); with no such device, the one device without a process (the generator builds it). ValueError unless
    exactly one is left.
    """
    if not spec.stubs:
        raise ValueError("the far facility holds no provider appliance or device to terminate the connection on")
    candidates = [stub for stub in spec.stubs if stub.processes]
    if spec.provider_as_id:
        candidates = [stub for stub in candidates if any(p.local_as_id == spec.provider_as_id for p in stub.processes)]
        if not candidates:  # no stub speaks the AS yet: the one stub without a process gets it
            candidates = [stub for stub in spec.stubs if not stub.processes]
    elif not candidates and len(spec.stubs) == 1:
        candidates = list(spec.stubs)  # the plan then says it needs provider_as to build the process
    if len(candidates) != 1:
        names = ", ".join(sorted(stub.name for stub in candidates)) or "none"
        raise ValueError(
            f"the far facility must hold exactly one provider stub whose ManagedBGP process speaks provider_as (or, "
            f"with no such stub, exactly one stub without a process), found: {names}"
        )
    return candidates[0]


def port_of(spec: ConnectionSpec, interface_id: str, name: str, device_id: str, device_name: str) -> Port:
    """A port of the near facility or of a provider device of the far one, as the generator read it."""
    stub = next((stub for stub in spec.stubs if stub.id == device_id), None)
    if stub is not None:
        deployment = spec.far_id
    elif device_id in spec.near_device_ids:
        deployment = spec.near_id
    else:
        deployment = None
    return Port(
        id=interface_id,
        name=name,
        device_id=device_id,
        device_name=device_name,
        deployment_id=deployment,
        processes=stub.processes if stub else (),
    )


def carrier_ends(spec: ConnectionSpec, ends: Iterable[Port]) -> tuple[Port, Port] | None:
    """(the end on a device of the near facility, the end on a device of the far one) of a carrier; None unless it
    has exactly one of each."""
    ends = list(ends)
    near = [end for end in ends if end.device_id in spec.near_device_ids]
    far = [end for end in ends if end.device_id in spec.stub_ids]
    return (near[0], far[0]) if len(near) == 1 and len(far) == 1 else None


def reused_ends(spec: ConnectionSpec, carrier_name: str, ends: Iterable[Port]) -> DedicatedEnds:
    """The ends of the link when an existing carrier is reused: its end on the near facility and its end on the far
    one. ValueError unless it has exactly one of each."""
    pair = carrier_ends(spec, ends)
    if pair is None:
        raise ValueError(
            f"carrier circuit {carrier_name} must end once on a device of the near facility and once on a device "
            "of the far one"
        )
    far = pair[1]
    stub = next(stub for stub in spec.stubs if stub.id == far.device_id)
    return DedicatedEnds(pair[0], stub, far)


def own_carriers(spec: ConnectionSpec) -> list[tuple[Carrier, Port, Port]]:
    """The live carriers the connection already lists (its own output) that join the near and the far facility, with
    their (near end, far end)."""
    found = []
    for carrier in spec.carriers:
        if carrier.status == DECOMMISSIONED:
            continue
        pair = carrier_ends(spec, carrier.ends)
        if pair is not None:
            found.append((carrier, *pair))
    return found


def _vpn_sides(spec: ConnectionSpec, flavour: ConnectionFlavour) -> list[tuple[Side, Side, BgpProcess | None]]:
    """The (near, far, far process) of each tunnel: one near device and one stub shared by every tunnel.

    The near side keeps the internet carrier's port it leaves by: the tunnel interface rides on it
    (``parent_interface``), which is how the path from the tunnel to the internet carrier is a graph path. With
    ``near_port`` set, it must be the end port of one of the near facility's internet carriers (it picks the edge);
    without it, exactly one device may end an internet carrier there.
    """
    assert spec.near_id
    ends = _carrier_ends(spec.near_internet, spec.near_id)
    if spec.near_port_id:
        matching = [port for port in ends if port.id == spec.near_port_id]
        if not matching:
            raise ValueError(
                f"near_port {spec.near_port_id} is not the end port of an internet carrier circuit of the near "
                "facility: a VPN leaves by the port of one of them"
            )
        near_port = matching[0]
    else:
        devices = {port.device_id: port for port in ends}
        if len(devices) != 1:
            names = ", ".join(sorted(port.device_name for port in devices.values())) or "none"
            raise ValueError(
                f"a VPN leaves the near facility by exactly one device that ends an internet carrier circuit there "
                f"(a connection lists no carrier: fix the internet carriers of the near facility, or set near_port "
                f"to the internet carrier port the tunnels leave by), found: {names}"
            )
        near_port = next(iter(devices.values()))
    stub = choose_stub(spec)
    process = _single_process(stub.name, stub.processes, spec.provider_as_id)
    near = Side(near_port.device_id, near_port.device_name, near_port)
    far = Side(stub.id, stub.name, None)
    return [(near, far, process) for _ in range(flavour.links)]


def choose_vlan(requested: int | None, reuse: int | None, used: Iterable[int]) -> int:
    """The VLAN of a dedicated connection.

    The requested ``vlan_id`` wins; else the VLAN its existing sessions already ride (a rerun never renumbers);
    else the lowest VLAN free on every parent port (``used`` = the VLANs of their sub-interfaces). ValueError when
    the range is exhausted.
    """
    if requested:
        return requested
    if reuse:
        return reuse
    taken = set(used)
    for vlan in range(VLAN_MIN, VLAN_MAX + 1):
        if vlan not in taken:
            return vlan
    raise ValueError("no free VLAN left on the parent port(s)")


def choose_tunnel_parent(nic_names: Iterable[str]) -> str | None:
    """The NIC a tunnel of a provider appliance rides on: the first by name that is no loopback, tunnel or
    sub-interface; None when the appliance has no NIC (the tunnel then hangs on nothing)."""
    nics = sorted(n for n in nic_names if "." not in n and not n.startswith(("Loopback", "Tunnel")))
    return nics[0] if nics else None


def choose_tunnel_name(taken: Iterable[str]) -> str:
    """The lowest ``Tunnel<n>`` not in ``taken``; ValueError when all TUNNEL_MAX are in use."""
    used = {int(m.group(1)) for name in taken if (m := _TUNNEL_NAME.fullmatch(name))}
    for number in range(1, TUNNEL_MAX + 1):
        if number not in used:
            return f"Tunnel{number}"
    raise ValueError(f"all {TUNNEL_MAX} tunnel interface names are taken")


def tunnel_names(limit: int = TUNNEL_MAX) -> list[str]:
    """Every name :func:`choose_tunnel_name` may return, for one ``name__values`` filter."""
    return [f"Tunnel{number}" for number in range(1, limit + 1)]


def existing_vlan(session: ExistingSession | None, near_device_id: str) -> int | None:
    """The VLAN of the near end of an existing dedicated session (its ``<port>.<vlan>`` name), else None."""
    if session is None:
        return None
    for end in session.ends:
        if end.device_id == near_device_id:
            return subinterface_vlan(end.name)
    return None


def session_is_current(
    session: ExistingSession | None, *, desired: dict[str, Any], end_ids: frozenset[str], group: str
) -> bool:
    """The existing session already holds everything the generator would write (so a rerun saves nothing).

    ``desired`` has circuit_type, status, provider_id, namespace_id, connection_id, link_prefix_id and password_id
    (None: not compared, the generator never clears one) and location_ids. The description is never compared: a hand
    edit of it is not reverted, and it is not sent when nothing else changed.
    """
    if session is None:
        return False
    if session.circuit_type != desired["circuit_type"] or session.status != desired["status"]:
        return False
    if session.provider_id != desired["provider_id"] or session.namespace_id != desired["namespace_id"]:
        return False
    if session.connection_id != desired["connection_id"] or session.location_ids != desired["location_ids"]:
        return False
    if desired["link_prefix_id"] is not None and session.link_prefix_id != desired["link_prefix_id"]:
        return False
    if desired["password_id"] is not None and session.password_id != desired["password_id"]:
        return False
    return frozenset(end.id for end in session.ends) == end_ids and group in session.groups
