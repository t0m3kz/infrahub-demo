"""The pure planner of a teardown: what to delete, in what order, and what is kept, with the reason.

Two callers, one implementation: ``add_provider_connection`` tears down the sessions it removes (a decommissioned
connection, a link that left the plan) and ``add_virtual_circuit`` tears down the outputs of a decommissioned circuit
(``generators/helpers/session_teardown.py`` reads the facts and executes this plan). Nothing here calls the client.

The order leaves no dangling reference:

1. the peerings built on the session (a BGP process is shared and never a step),
2. the addresses on the ends that are removed,
3. the link prefix the session allocated (its pool allocation goes with it, the range can be handed out again); an
   authored one (``link_prefix``, a range the provider assigned) is never a step,
4. the session circuit,
5. the end interfaces: a sub-interface ``<port>.<vlan>`` or a ``Tunnel<n>``; never a physical port, a NIC, or an
   interface another session or a sub-interface still uses,
6. (:func:`plan_carrier_teardown`, after the interfaces of the legs) the carrier the connection generated and its NIC.

Ownership is read from edges, never from a list or a name search, with one exception stated here: the carrier the
generator created is recognised by its name ``<connection>-XC`` (the name it gives, in a name space the connection
shares with nothing else), together with being the connection's own and used by no other connection. An untouched one
(status ``provisioning``, ``circuit_id`` still the placeholder) is deleted; one a person has already filled in is only
set to ``decommissioned``, it is the record of a real order. A carrier of another name was authored and stays as it is.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

DELETE = "delete"
DECOMMISSION = "decommission"

KIND_PEERING = "ManagedBGPPeering"
KIND_ADDRESS = "IpamIPAddress"
KIND_PREFIX = "IpamPrefix"
KIND_SESSION = "TopologySessionCircuit"
KIND_INTERFACE = "DcimVirtualInterface"
KIND_CARRIER = "TopologyCarrierCircuit"

_TUNNEL = re.compile(r"Tunnel\d+")
PROVISIONING = "provisioning"
DECOMMISSIONED = "decommissioned"


@dataclass(frozen=True)
class EndFacts:
    """An interface at the end of a session that is being removed, as read from the server."""

    id: str
    name: str
    kind: str  # the concrete kind: only a DcimVirtualInterface can be owned
    parent_id: str | None = None
    parent_name: str | None = None
    address_id: str | None = None
    address_prefix_id: str | None = None
    peering_ids: frozenset[str] = frozenset()  # peerings of this session on the interface (typename + name matched)
    other_session_ids: frozenset[str] = frozenset()  # session circuits other than the removed ones ending here
    child_ids: frozenset[str] = frozenset()  # interfaces riding on it (parent_interface) that are not removed here


@dataclass(frozen=True)
class PrefixFacts:
    """A prefix the removed sessions' addresses sit in, with everything inside it."""

    id: str
    address_ids: frozenset[str]
    child_ids: frozenset[str]


@dataclass(frozen=True)
class SessionFacts:
    """A session circuit to remove with its ends (both read, whatever their kind)."""

    id: str
    name: str
    link_prefix_id: str | None  # the authored range of a provider-assigned flavour: never deleted
    ends: tuple[EndFacts, ...]


@dataclass(frozen=True)
class Step:
    """One write of the teardown, in execution order."""

    action: str  # DELETE | DECOMMISSION
    kind: str
    id: str
    label: str


@dataclass(frozen=True)
class TeardownPlan:
    """The ordered steps and what is kept on purpose (id, reason), for the log and the tests."""

    steps: tuple[Step, ...] = ()
    kept: tuple[tuple[str, str], ...] = field(default=())
    # Leg interfaces the plan leaves because something else uses them: the caller keeps them in its tracking group so
    # the SDK cleanup of the run does not delete what the plan decided to keep.
    held: tuple[str, ...] = field(default=())

    def ids(self, kind: str) -> list[str]:
        return [step.id for step in self.steps if step.kind == kind]

    def __add__(self, other: TeardownPlan) -> TeardownPlan:
        return TeardownPlan(steps=self.steps + other.steps, kept=self.kept + other.kept, held=self.held + other.held)


def owns_interface(end: EndFacts) -> bool:
    """A leg interface of a connection: a ``<parent>.<vlan>`` sub-interface or a ``Tunnel<n>`` of a virtual interface."""
    if end.kind != KIND_INTERFACE:
        return False
    if _TUNNEL.fullmatch(end.name):
        return True
    return bool(end.parent_id and end.parent_name and end.name.startswith(f"{end.parent_name}."))


def plan_session_teardown(
    sessions: Iterable[SessionFacts], prefixes: dict[str, PrefixFacts] | None = None
) -> TeardownPlan:
    """The steps that remove ``sessions`` and what they own; what another user holds is kept and reported.

    ``prefixes`` has the facts of every prefix an end's address sits in. A prefix is a step only when it is not the
    session's authored range and everything inside it is an address of the removed ends (nothing else is addressed in
    it, no child prefix).
    """
    sessions = list(sessions)
    prefixes = prefixes or {}
    kept: list[tuple[str, str]] = []
    held: list[str] = []
    peerings: dict[str, str] = {}
    addresses: dict[str, str] = {}
    prefix_ids: dict[str, str] = {}
    interfaces: dict[str, str] = {}
    removed_addresses: set[str] = set()

    removed_ends: dict[str, EndFacts] = {}
    for session in sessions:
        for end in session.ends:
            removed_ends.setdefault(end.id, end)
    owned = {end_id: end for end_id, end in removed_ends.items() if owns_interface(end)}

    for session in sessions:
        for end in session.ends:
            if end.id not in owned:
                kept.append((end.id, f"{end.name} is not a leg interface of the connection (physical port or NIC)"))
                continue
            if end.other_session_ids or end.child_ids:
                kept.append((end.id, f"{end.name} is still used by another session or a sub-interface"))
                held.append(end.id)
                continue
            for peering_id in sorted(end.peering_ids):
                peerings.setdefault(peering_id, f"peering on {end.name} of {session.name}")
            if end.address_id:
                addresses.setdefault(end.address_id, f"address of {end.name}")
                removed_addresses.add(end.address_id)
            interfaces.setdefault(end.id, f"{end.name} of {session.name}")
        for end in session.ends:
            prefix = prefixes.get(end.address_prefix_id or "")
            if prefix is None or end.id not in owned or end.address_id not in removed_addresses:
                continue
            if prefix.id == session.link_prefix_id:
                kept.append((prefix.id, f"link prefix of {session.name} is authored (assigned by the provider)"))
            elif prefix.child_ids or not prefix.address_ids <= removed_addresses:
                kept.append((prefix.id, f"link prefix of {session.name} still holds addresses of something else"))
            else:
                prefix_ids.setdefault(prefix.id, f"link prefix of {session.name}")

    steps = (
        [Step(DELETE, KIND_PEERING, i, label) for i, label in peerings.items()]
        + [Step(DELETE, KIND_ADDRESS, i, label) for i, label in addresses.items()]
        + [Step(DELETE, KIND_PREFIX, i, label) for i, label in prefix_ids.items()]
        + [Step(DELETE, KIND_SESSION, session.id, session.name) for session in sessions]
        + [Step(DELETE, KIND_INTERFACE, i, label) for i, label in interfaces.items()]
    )
    return TeardownPlan(steps=tuple(steps), kept=tuple(dict.fromkeys(kept)), held=tuple(dict.fromkeys(held)))


def plan_stale_end_teardown(ends: Iterable[EndFacts]) -> TeardownPlan:
    """The interfaces a KEPT session no longer ends on (a changed path type, VLAN or port): only the interface goes.

    The addresses stay on purpose: the session is kept, so add_virtual_circuit reuses the same link addresses for its
    new ends and the same address node may be the one it assigns next; deleting it here could remove the new end's.
    """
    steps: list[Step] = []
    kept: list[tuple[str, str]] = []
    held: list[str] = []
    for end in ends:
        if not owns_interface(end):
            continue
        if end.other_session_ids or end.child_ids:
            kept.append((end.id, f"{end.name} is still used by another session or a sub-interface"))
            held.append(end.id)
            continue
        steps.append(Step(DELETE, KIND_INTERFACE, end.id, f"stale end {end.name}"))
    return TeardownPlan(steps=tuple(steps), kept=tuple(kept), held=tuple(held))


@dataclass(frozen=True)
class CarrierEndFacts:
    """An end of the generated carrier: the port or NIC, with what else sits on it."""

    id: str
    name: str
    kind: str
    other_carrier_ids: frozenset[str] = frozenset()  # carriers other than this one with an end here
    child_ids: frozenset[str] = frozenset()  # interfaces riding on it that survive the teardown
    session_ids: frozenset[str] = frozenset()  # session circuits that survive the teardown and end here


@dataclass(frozen=True)
class CarrierFacts:
    """A carrier listed by the connection, with its ends and the connections that list it."""

    id: str
    name: str
    circuit_id: str
    status: str
    connection_ids: frozenset[str]
    ends: tuple[CarrierEndFacts, ...]


def generated_carrier_name(connection_name: str) -> str:
    """The name the connection generator gives the carrier it creates."""
    return f"{connection_name}-XC"


def nic_name(connection_name: str) -> str:
    """The name of the service-edge NIC the generator creates (the far end of the carrier it creates)."""
    return f"vnic-{connection_name}"


def plan_carrier_teardown(connection_id: str, connection_name: str, carriers: Iterable[CarrierFacts]) -> TeardownPlan:
    """What happens to the carrier the connection generated, once the interfaces of its legs are gone.

    Only a carrier named ``<connection>-XC`` that is listed by this connection alone is touched. It is left alone
    when any of its ends is an end of another carrier, or carries a sub-interface or a session that survives: shared
    underlay. An untouched placeholder (status ``provisioning``, ``circuit_id`` equal to the name) is deleted with its
    NIC (``vnic-<connection>``, the only end that is a NIC the generator created); a carrier a person has filled in is
    set to ``decommissioned`` and its NIC stays with it. The near port is never a step: its claim is the carrier's
    end, which a deleted carrier releases and a decommissioned one no longer holds.
    """
    steps: list[Step] = []
    kept: list[tuple[str, str]] = []
    wanted = generated_carrier_name(connection_name)
    for carrier in carriers:
        if carrier.name != wanted:
            kept.append((carrier.id, f"{carrier.name} was authored, not generated by {connection_name}"))
            continue
        if carrier.connection_ids - {connection_id}:
            kept.append((carrier.id, f"{carrier.name} is listed by another connection"))
            continue
        busy = [end for end in carrier.ends if end.other_carrier_ids or end.child_ids or end.session_ids]
        if busy:
            kept.append((carrier.id, f"{carrier.name}: {busy[0].name} is still in use (another carrier or session)"))
            continue
        if carrier.status == DECOMMISSIONED:
            kept.append((carrier.id, f"{carrier.name} is already decommissioned"))
            continue
        if carrier.status == PROVISIONING and carrier.circuit_id == carrier.name:
            steps.append(Step(DELETE, KIND_CARRIER, carrier.id, carrier.name))
            steps.extend(
                Step(DELETE, KIND_INTERFACE, end.id, f"NIC {end.name} of {carrier.name}")
                for end in carrier.ends
                if end.kind == KIND_INTERFACE and end.name == nic_name(connection_name)
            )
        else:
            steps.append(Step(DECOMMISSION, KIND_CARRIER, carrier.id, carrier.name))
    return TeardownPlan(steps=tuple(steps), kept=tuple(kept))


def sessions_to_remove(
    existing: Iterable[tuple[str, str]], wanted_names: Iterable[str], decommissioned: bool
) -> list[str]:
    """The ids of the existing sessions (id, name) the run removes: all when decommissioned, else those off the plan."""
    keep = set() if decommissioned else set(wanted_names)
    return [session_id for session_id, name in existing if name not in keep]
