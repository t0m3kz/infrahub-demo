"""The pure half of add_provider_connection: parse the query payload, validate it, plan the links.

Nothing here calls the client. ``parse_connection`` reads the cleaned payload of
``queries/topology/add/provider_connection.gql``, ``plan_links`` decides everything that can be decided from it (the
flavour, the ends of each link, the link prefix of each session) and raises ``ValueError`` for what is wrong, so the
generator refuses a spec before its first write. The VLAN and the tunnel names depend on what the devices already
hold, so the generator reads that under its locks and asks :func:`choose_vlan` / :func:`choose_tunnel_name`.

Where the two ends of a link come from (the decision the schema leaves open):

- dedicated: the connection's authored ``carrier_circuits``. A carrier end whose device is deployed in the near
  facility is a near port, one deployed in the far facility is a far port; each side needs exactly one port per link
  of the flavour, paired in carrier order. The far port's device IS the provider stub (a stub shared per provider and
  facility, authored in data). A provider-hosted near end (a Network Edge NIC) is found the same way, through a
  virtual-connection carrier that ends on it.
- internet_vpn: no sub-interface. The near device is the one device deployed in the near facility that ends an
  internet carrier circuit: the connection's ``carrier_circuits`` when it lists any (they must be internet carriers;
  this is how a facility with several internet-fed edges says which one the tunnels leave by), else the near
  facility's own internet carriers (then exactly one device may end one). The tunnel rides on that carrier's port
  (``parent_interface``). The far device is the stub of the far facility: the one whose BGP process speaks
  ``provider_as`` when several are deployed there, so two stubs of a facility need two distinct ASes.

The stub carries ONE ManagedBGP process. An authored one is reused (its AS must be ``provider_as``); a stub without
one gets it from the generator (generators/provider_stub.py), which needs the connection's ``provider_as`` for the AS:
such a link plans ``far_process_id = None``. A VPN picks its stub among those whose process speaks ``provider_as``,
else among those with no process at all.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from dataclasses import dataclass
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
    service: str
    status: str
    vlan_id: int | None
    namespace_id: str | None
    provider_id: str | None
    provider_name: str | None
    profile: str | None
    provider_as_id: str | None
    password_id: str | None
    near_id: str | None
    far_id: str | None
    link_prefixes: tuple[tuple[str, str], ...]
    carriers: tuple[Carrier, ...]
    near_internet: tuple[Carrier, ...]
    stubs: tuple[Stub, ...]
    sessions: tuple[ExistingSession, ...]


@dataclass(frozen=True)
class Side:
    """One side of a link: its device and, for a dedicated link, the port the sub-interface rides."""

    device_id: str
    device_name: str
    port: Port | None


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
        service=raw.get("service") or "",
        status=raw.get("status") or "",
        vlan_id=raw.get("vlan_id"),
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


def plan_links(spec: ConnectionSpec) -> tuple[ConnectionFlavour, list[LinkPlan]]:
    """The flavour and the links of a connection; ValueError for anything that cannot be built.

    Every failure here precedes the first write of the generator.
    """
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
    prefix_ids = pair_link_prefixes(flavour, spec.link_prefixes)
    circuit_type = circuit_type_of(spec.path_type)
    if spec.path_type == PATH_DEDICATED:
        sides = _dedicated_sides(spec, flavour)
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


def _dedicated_sides(spec: ConnectionSpec, flavour: ConnectionFlavour) -> list[tuple[Side, Side, BgpProcess | None]]:
    """The (near, far, far process) of each link, from the authored carriers' ends."""
    assert spec.near_id and spec.far_id
    if not spec.carriers:
        raise ValueError("a dedicated connection needs its carrier_circuits")
    near_ports = _carrier_ends(spec.carriers, spec.near_id)
    far_ports = _carrier_ends(spec.carriers, spec.far_id)
    if len(near_ports) != flavour.links or len(far_ports) != flavour.links:
        raise ValueError(
            f"the flavour has {flavour.links} link(s): the carriers must end on exactly that many ports in the near "
            f"facility and in the far facility, found {len(near_ports)} near and {len(far_ports)} far"
        )
    stub_ids = {stub.id for stub in spec.stubs}
    if not stub_ids:
        raise ValueError("the far facility holds no provider appliance or device to land the connection on")
    off_stub = sorted({port.label for port in far_ports if port.device_id not in stub_ids})
    if off_stub:
        raise ValueError(
            f"far carrier end(s) {', '.join(off_stub)} are not on a provider appliance or device of the far facility"
        )
    sides = []
    for near, far in zip(near_ports, far_ports, strict=True):
        process = _single_process(far.device_name, far.processes, spec.provider_as_id)
        sides.append((Side(near.device_id, near.device_name, near), Side(far.device_id, far.device_name, far), process))
    return sides


def _vpn_sides(spec: ConnectionSpec, flavour: ConnectionFlavour) -> list[tuple[Side, Side, BgpProcess | None]]:
    """The (near, far, far process) of each tunnel: one near device and one stub shared by every tunnel.

    The near side keeps the internet carrier's port it leaves by: the tunnel interface rides on it
    (``parent_interface``), which is how the path from the tunnel to the internet carrier is a graph path.
    """
    assert spec.near_id
    internet = spec.carriers or spec.near_internet
    wrong = sorted(carrier.name for carrier in internet if carrier.circuit_type != INTERNET_CARRIER)
    if wrong:
        raise ValueError(f"a VPN rides internet carrier circuits, not {', '.join(wrong)}")
    devices = {port.device_id: port for port in _carrier_ends(internet, spec.near_id)}
    if len(devices) != 1:
        names = ", ".join(sorted(port.device_name for port in devices.values())) or "none"
        raise ValueError(
            f"a VPN leaves the near facility by exactly one device that ends an internet carrier circuit there "
            f"(list the carrier in carrier_circuits when the facility has several), found: {names}"
        )
    near_port = next(iter(devices.values()))
    if not spec.stubs:
        raise ValueError("the far facility holds no provider appliance or device to terminate the tunnels on")
    candidates = [stub for stub in spec.stubs if stub.processes]
    if spec.provider_as_id:
        candidates = [stub for stub in candidates if any(p.local_as_id == spec.provider_as_id for p in stub.processes)]
        if not candidates:  # no stub speaks the AS yet: the one stub without a process gets it
            candidates = [stub for stub in spec.stubs if not stub.processes]
    if len(candidates) != 1:
        names = ", ".join(sorted(stub.name for stub in candidates)) or "none"
        raise ValueError(
            f"the far facility must hold exactly one provider stub whose ManagedBGP process speaks provider_as (or, "
            f"with no such stub, exactly one stub without a process), found: {names}"
        )
    stub = candidates[0]
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
