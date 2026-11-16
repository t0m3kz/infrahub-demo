"""The path of a cloud connection down to L1, over clean_data payloads (pure: no client, no logging).

From a footprint's virtual network: the attachment to it, the gateway chain (``peer_gateway`` hops between gateways),
the ``CloudPeeringInterface`` that lands on a gateway of that chain (its ``gateway``), the port or LAG it rides on (its
``parent``, a port of a ``TopologyProviderConnect``), the LAG's member ports, the ``DcimCable`` or carrier circuit on
a port, our port at the far end, and our interface on it (the port itself, a sub-interface, or our LAG the port is a
member of and its sub-interfaces) that ends the peering's session circuit. A trace returns its hops, or the hops up to the first one that is missing with the reason.

``physical_ports`` (an interface, its parent, and the member ports of a LAG among them) is also what
utils/circuit_underlay.py reads a port's carriers and cables through.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

LAG_KIND = "DcimLAGInterface"
CARRIER_KIND = "TopologyCarrierCircuit"
SESSION_KIND = "TopologySessionCircuit"
DECOMMISSIONED = "decommissioned"
# The session circuits a cloud peering ends (a tunnel is the SaaS one: no session, still the connection's end)
SESSION_TYPES = frozenset({"handoff", "vpn", "tunnel"})


@dataclass(frozen=True)
class Hop:
    """One element of the path: what it is and its name (``device:interface`` for an interface)."""

    kind: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind} '{self.name}'"


@dataclass(frozen=True)
class Trace:
    """The hops of a path; ``missing`` is why it stops after the last hop (None: it reaches our session end)."""

    hops: tuple[Hop, ...]
    missing: str | None = None

    @property
    def ok(self) -> bool:
        return self.missing is None

    @property
    def stop(self) -> str:
        """Where the path stops, as a message names it."""
        return str(self.hops[-1]) if self.hops else "the start"

    def describe(self) -> str:
        """The path and, when it stops, where and why."""
        path = " -> ".join(str(hop) for hop in self.hops)
        return path if self.ok else f"{path} stops at {self.stop}: {self.missing}"


def _ref(value: Any) -> str | None:
    """The id of a cleaned reference (clean_data collapses an object with only an ``id`` to the bare id)."""
    if isinstance(value, Mapping):
        value = value.get("id")
    return str(value) if value else None


def _dicts(values: Any) -> list[dict[str, Any]]:
    return [value for value in values or [] if isinstance(value, dict)]


def interface_label(interface: Mapping[str, Any]) -> str:
    """``device:interface`` of a cleaned interface."""
    device = interface.get("device")
    device_name = device.get("name") if isinstance(device, Mapping) else None
    return f"{device_name or '?'}:{interface.get('name') or interface.get('id') or '?'}"


def physical_ports(interface: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The interface, its parent (one level), and the member ports of whichever of them is a LAG: where a cable or a
    carrier circuit of the interface may terminate. Deduplicated, in that order.
    """
    if not isinstance(interface, Mapping):
        return []
    parent = interface.get("parent")
    found: dict[str, dict[str, Any]] = {}
    for candidate in (interface, parent if isinstance(parent, Mapping) else None):
        if not isinstance(candidate, dict):
            continue
        found.setdefault(_ref(candidate) or str(id(candidate)), candidate)
        if candidate.get("typename") == LAG_KIND:
            for member in _dicts(candidate.get("member_interfaces")):
                found.setdefault(_ref(member) or str(id(member)), member)
    return list(found.values())


def carriers_on(port: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The live carrier circuits that end on the port."""
    return [
        circuit
        for circuit in _dicts(port.get("capabilities"))
        if circuit.get("typename") == CARRIER_KIND and circuit.get("status") != DECOMMISSIONED
    ]


def l1_links(port: Mapping[str, Any]) -> list[tuple[Hop, list[dict[str, Any]]]]:
    """The cable and the live carrier circuits of a port, each with its far ends (an empty dict is a patch panel port:
    a reader of a cable end meets it as ``{}``).
    """
    port_id = _ref(port)
    links: list[tuple[Hop, list[dict[str, Any]]]] = []
    cable = port.get("cable")
    if isinstance(cable, Mapping):
        far = [end for end in cable.get("endpoints") or [] if isinstance(end, dict) and _ref(end) != port_id]
        links.append((Hop("cable", str(cable.get("name") or cable.get("id"))), far))
    for circuit in carriers_on(port):
        far = [end for end in _dicts(circuit.get("capabilities")) if _ref(end) != port_id]
        links.append((Hop("carrier circuit", str(circuit.get("name") or circuit.get("id"))), far))
    return links


def session_circuits(peering: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The session circuits that end on a cleaned peering."""
    return [
        circuit
        for circuit in _dicts(peering.get("capabilities"))
        if circuit.get("typename") == SESSION_KIND and circuit.get("circuit_type") in SESSION_TYPES
    ]


def _our_ends(peering: Mapping[str, Any]) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Our interface id -> (it, its session circuit): the other ends of the peering's session circuits."""
    peering_id = _ref(peering)
    ends: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for circuit in session_circuits(peering):
        for end in _dicts(circuit.get("capabilities")):
            if (end_id := _ref(end)) and end_id != peering_id:
                ends.setdefault(end_id, (end, circuit))
    return ends


def _our_side(far: dict[str, Any]) -> list[dict[str, Any]]:
    """What may end the session at our port: the port, its sub-interfaces, and its LAG (``lag``) with the LAG's."""
    lag = far.get("lag")
    lags = [lag] if isinstance(lag, dict) else []
    return [
        far,
        *_dicts(far.get("sub_interfaces")),
        *lags,
        *(s for each in lags for s in _dicts(each.get("sub_interfaces"))),
    ]


def trace_peering(peering: Mapping[str, Any], before: Iterable[Hop] = ()) -> Trace:
    """From a peering to our interface that ends its session circuit, through its parent, the LAG members, and the
    cable or carrier on a port.
    """
    hops = [*before, Hop("peering", interface_label(peering))]
    ours = _our_ends(peering)
    if not ours:
        return Trace(tuple(hops), "no session circuit (handoff, vpn or tunnel) ends on it")
    parent = peering.get("parent")
    if not isinstance(parent, dict):
        return Trace(tuple(hops), "it rides on no port or LAG (set its parent and vlan_id)")
    hops.append(Hop("LAG" if parent.get("typename") == LAG_KIND else "port", interface_label(parent)))
    ports = physical_ports(parent)
    if parent.get("typename") == LAG_KIND and len(ports) == 1:
        return Trace(tuple(hops), "the LAG has no member port")
    reached: list[str] = []
    patch_panel = False
    for port in ports:
        for link, far_ends in l1_links(port):
            for far in far_ends:
                if not far:
                    patch_panel = True
                    continue
                for candidate in _our_side(far):
                    if (candidate_id := _ref(candidate)) in ours:
                        end, circuit = ours[candidate_id]
                        path = [Hop("port", interface_label(port))] if port is not parent else []
                        return Trace(
                            (
                                *hops,
                                *path,
                                link,
                                Hop("our port", interface_label(far)),
                                Hop("interface", interface_label(end)),
                                Hop("session circuit", str(circuit.get("name") or circuit.get("id"))),
                            )
                        )
                reached.append(interface_label(far))
    if reached:
        circuits = ", ".join(sorted({str(c.get("name")) for _end, c in ours.values()}))
        return Trace(
            tuple(hops),
            f"its L1 reaches {', '.join(sorted(set(reached)))}, which carries no end of its session circuit "
            f"({circuits}): the session's end must be that port or a sub-interface of it",
        )
    if patch_panel:
        return Trace(tuple(hops), "its cable ends on a patch panel port, which the trace does not follow")
    names = ", ".join(interface_label(port) for port in ports)
    return Trace(tuple(hops), f"no cable or live carrier circuit on {names}")


def gateway_chain(start: Any, attachments: Iterable[Mapping[str, Any]] | None) -> list[str]:
    """The gateway ``start`` and every gateway joined to it by a gateway-to-gateway attachment, in BFS order."""
    start_id = _ref(start)
    if not start_id:
        return []
    links: dict[str, set[str]] = {}
    for attachment in attachments or []:
        hub, peer = _ref(attachment.get("gateway")), _ref(attachment.get("peer_gateway"))
        if hub and peer:
            links.setdefault(hub, set()).add(peer)
            links.setdefault(peer, set()).add(hub)
    chain, queue = [start_id], deque([start_id])
    while queue:
        for neighbour in sorted(links.get(queue.popleft(), ())):
            if neighbour not in chain:
                chain.append(neighbour)
                queue.append(neighbour)
    return chain


def trace_gateway(
    gateway: Mapping[str, Any],
    attachments: Iterable[Mapping[str, Any]] | None,
    peerings: Iterable[Mapping[str, Any]] | None,
    before: Iterable[Hop] = (),
) -> Trace:
    """From a gateway through its chain to a peering that lands on it, then ``trace_peering``: the first path that
    reaches our session end, else the one that goes furthest.
    """
    attachment_list = list(attachments or [])
    chain = gateway_chain(gateway, attachment_list)
    hops = [*before, Hop("gateway", str(gateway.get("name") or _ref(gateway)))]
    landing = [p for p in peerings or [] if _ref(p.get("gateway")) in chain]
    if not landing:
        return Trace(tuple(hops), "no peering lands on it or on a gateway attached to it (a peering's gateway)")
    best: Trace | None = None
    for peering in sorted(landing, key=lambda p: interface_label(p)):
        trace = trace_peering(peering, hops)
        if trace.ok:
            return trace
        if best is None or len(trace.hops) > len(best.hops):
            best = trace
    return best or Trace(tuple(hops), "no peering to trace")


def trace_network(
    network: Mapping[str, Any],
    attachments: Iterable[Mapping[str, Any]] | None,
    peerings: Iterable[Mapping[str, Any]] | None,
) -> Trace:
    """From a virtual network through its attachments to L1 (``trace_gateway`` for each attachment's gateway)."""
    attachment_list = list(attachments or [])
    hops = [Hop("virtual network", str(network.get("name") or _ref(network)))]
    own = [a for a in attachment_list if _ref(a.get("virtual_network")) == _ref(network)]
    if not own:
        return Trace(tuple(hops), "no gateway attachment targets it")
    best: Trace | None = None
    for attachment in sorted(own, key=lambda a: str(a.get("name"))):
        gateway = attachment.get("gateway")
        gateway = gateway if isinstance(gateway, Mapping) else {"id": gateway}
        trace = trace_gateway(
            gateway, attachment_list, peerings, [*hops, Hop("attachment", str(attachment.get("name")))]
        )
        if trace.ok:
            return trace
        if best is None or len(trace.hops) > len(best.hops):
            best = trace
    return best or Trace(tuple(hops), "no attachment to trace")
