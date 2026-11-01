"""The client side of a teardown: read the facts, take the locks, run the plan of ``provider_teardown``.

Used by ``add_provider_connection`` only (a decommissioned connection, a link that left the plan). The pure decisions are
``generators/helpers/provider_teardown.py``; this module reads what they need from the server by EDGES (the end
interfaces of a session, their address, the peerings on them, the prefix the address sits in) and executes the
ordered steps, idempotently: a node a step names that is already gone is skipped, so a half-torn state converges and a
rerun writes nothing.

Why ``add_virtual_circuit`` does not call it (a deliberate limit, not an oversight): that generator's own cleanup (its
tracking group, ``delete_unused_nodes``) removes the prefix, addresses and peerings of a decommissioned or shrunk
circuit, and it knows by CREATION what it owns. An edge-based teardown cannot tell an address it assigned from an
authored one on the end interface of a hand-made handoff, so running it there could delete data. The connection
generator may use edges because the interfaces it removes are its own (a ``<port>.<vlan>`` or ``Tunnel<n>`` it
created, with no address until add_virtual_circuit assigned one). The gap it closes: a session circuit that is DELETED
(or removed here) never runs add_virtual_circuit again, so that cleanup never happens.

Locks: the ``vc-port-<end id>`` locks of add_virtual_circuit (sorted), then its ``vc-session-<circuit id>`` locks
(sorted), the order that generator takes them, so the two never wait on each other in a cycle. The connection generator's
own ``pc-*`` locks are held by the caller and never taken by add_virtual_circuit.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from infrahub_sdk.exceptions import GraphQLError, NodeNotFoundError

from utils.circuit_underlay import SESSION_CIRCUIT, SESSION_CIRCUIT_TYPES

from ..protocols import DcimInterface, DcimVirtualInterface
from .provider_teardown import (
    DECOMMISSION,
    DELETE,
    KIND_PEERING,
    CarrierEndFacts,
    CarrierFacts,
    EndFacts,
    PrefixFacts,
    SessionFacts,
    TeardownPlan,
    plan_carrier_teardown,
    plan_session_teardown,
    plan_stale_end_teardown,
)

_CARRIER_KIND = "TopologyCarrierCircuit"
_DECOMMISSIONED = "decommissioned"


@dataclass(frozen=True)
class SessionRef:
    """A session circuit to remove as the connection query read it."""

    id: str
    name: str
    link_prefix_id: str | None
    end_ids: tuple[str, ...]


async def _sessions_on(client: Any, port_ids: list[str]) -> list[Any]:
    """The session circuits (dci, handoff, vpn) with one of ``port_ids`` among their ends, ends loaded."""
    return await client.filters(
        kind=SESSION_CIRCUIT,
        capabilities__ids=port_ids,
        circuit_type__values=list(SESSION_CIRCUIT_TYPES),
        include=["capabilities"],
    )


def _ids(rel: Any) -> list[str]:
    """The peer ids of a loaded cardinality-many relationship."""
    return [peer.id for peer in getattr(rel, "peers", None) or [] if getattr(peer, "id", None)]


class SessionTeardownMixin:
    """Read the facts of sessions and carriers, plan, and execute (see the module docstring)."""

    client: Any
    logger: Any

    # -- facts ---------------------------------------------------------------------------------------------------

    async def read_session_facts(
        self, refs: Iterable[SessionRef]
    ) -> tuple[list[SessionFacts], dict[str, PrefixFacts], dict[str, str]]:
        """The sessions with their ends, the prefixes their addresses sit in, and the device of each end.

        Only virtual interfaces are read: a physical port at the end of a session is never ours to touch.
        """
        refs = list(refs)
        removed = {ref.id for ref in refs}
        end_ids = sorted({end_id for ref in refs for end_id in ref.end_ids})
        if not end_ids:
            return [SessionFacts(r.id, r.name, r.link_prefix_id, ()) for r in refs], {}, {}
        nodes = {
            node.id: node
            for node in await self.client.filters(
                kind=DcimVirtualInterface,
                ids=end_ids,
                include=["ip_address", "parent_interface", "capabilities", "device"],
            )
        }
        parent_ids = sorted({str(pid) for n in nodes.values() if (pid := getattr(n.parent_interface, "id", None))})
        parents: dict[str, str] = {}
        if parent_ids:
            parents = {n.id: n.name.value for n in await self.client.filters(kind=DcimInterface, ids=parent_ids)}
        peering_ids = sorted(
            {
                p
                for n in nodes.values()
                for peer in n.capabilities.peers
                if peer.typename == KIND_PEERING and (p := peer.id)
            }
        )
        peering_names = (
            {p.id: p.name.value for p in await self.client.filters(kind=KIND_PEERING, ids=peering_ids)}
            if peering_ids
            else {}
        )
        others: dict[str, set[str]] = {end_id: set() for end_id in end_ids}
        for circuit in await _sessions_on(self.client, end_ids):
            if circuit.id in removed:
                continue
            for end in set(_ids(circuit.capabilities)) & set(end_ids):
                others[end].add(circuit.id)
        children: dict[str, set[str]] = {end_id: set() for end_id in end_ids}
        for child in await self.client.filters(kind=DcimVirtualInterface, parent_interface__ids=end_ids):
            if child.id not in end_ids:
                children[child.parent_interface.id].add(child.id)
        address_ids = sorted({a for n in nodes.values() if (a := getattr(n.ip_address, "id", None))})
        prefix_of: dict[str, str] = {}
        if address_ids:
            for address in await self.client.filters(kind="IpamIPAddress", ids=address_ids, include=["ip_prefix"]):
                if prefix_id := getattr(address.ip_prefix, "id", None):
                    prefix_of[address.id] = prefix_id
        prefixes: dict[str, PrefixFacts] = {}
        if prefix_of:
            for prefix in await self.client.filters(
                kind="IpamPrefix", ids=sorted(set(prefix_of.values())), include=["ip_addresses", "children"]
            ):
                prefixes[prefix.id] = PrefixFacts(
                    id=prefix.id,
                    address_ids=frozenset(_ids(prefix.ip_addresses)),
                    child_ids=frozenset(_ids(prefix.children)),
                )
        devices: dict[str, str] = {}
        sessions = []
        for ref in refs:
            ends = []
            for end_id in ref.end_ids:
                node = nodes.get(end_id)
                if node is None:
                    continue
                address_id = getattr(node.ip_address, "id", None)
                parent_id = getattr(node.parent_interface, "id", None)
                if device_id := getattr(node.device, "id", None):
                    devices[end_id] = device_id
                ends.append(
                    EndFacts(
                        id=end_id,
                        name=node.name.value,
                        kind=node.typename,
                        parent_id=parent_id,
                        parent_name=parents.get(parent_id or ""),
                        address_id=address_id,
                        address_prefix_id=prefix_of.get(address_id or ""),
                        peering_ids=frozenset(
                            peer.id
                            for peer in node.capabilities.peers
                            if peer.typename == KIND_PEERING
                            and peering_names.get(peer.id, "").startswith(f"{ref.name}-")
                        ),
                        other_session_ids=frozenset(others[end_id]),
                        child_ids=frozenset(children[end_id]),
                    )
                )
            sessions.append(SessionFacts(ref.id, ref.name, ref.link_prefix_id, tuple(ends)))
        return sessions, prefixes, devices

    async def read_carrier_facts(self, connection_name: str, carriers: Iterable[Any]) -> list[CarrierFacts]:
        """The connection's carriers with, per end, what else still sits on it (read AFTER the legs are gone)."""
        facts: list[CarrierFacts] = []
        for carrier in carriers:
            if carrier.name != f"{connection_name}-XC":
                facts.append(CarrierFacts(carrier.id, carrier.name, "", carrier.status, frozenset(), ()))
                continue
            node = await self.client.get(
                kind=_CARRIER_KIND, id=carrier.id, include=["provider_connections"], raise_when_missing=False
            )
            if node is None:
                continue
            end_ids = sorted(end.id for end in carrier.ends)
            kinds = (
                {n.id: n.typename for n in await self.client.filters(kind=DcimInterface, ids=end_ids)}
                if end_ids
                else {}
            )
            on_ends: dict[str, set[str]] = {end_id: set() for end_id in end_ids}
            children: dict[str, set[str]] = {end_id: set() for end_id in end_ids}
            sessions: dict[str, set[str]] = {end_id: set() for end_id in end_ids}
            if end_ids:
                for other in await self.client.filters(
                    kind=_CARRIER_KIND, capabilities__ids=end_ids, include=["capabilities"]
                ):
                    if other.id != carrier.id:
                        for end in set(_ids(other.capabilities)) & set(end_ids):
                            on_ends[end].add(other.id)
                for child in await self.client.filters(kind=DcimVirtualInterface, parent_interface__ids=end_ids):
                    children[child.parent_interface.id].add(child.id)
                for circuit in await _sessions_on(self.client, end_ids):
                    for end in set(_ids(circuit.capabilities)) & set(end_ids):
                        sessions[end].add(circuit.id)
            facts.append(
                CarrierFacts(
                    id=carrier.id,
                    name=carrier.name,
                    circuit_id=node.circuit_id.value or "",
                    status=node.status.value,
                    connection_ids=frozenset(_ids(node.provider_connections)),
                    ends=tuple(
                        CarrierEndFacts(
                            id=end.id,
                            name=end.name,
                            kind=kinds.get(end.id, ""),
                            other_carrier_ids=frozenset(on_ends[end.id]),
                            child_ids=frozenset(children[end.id]),
                            session_ids=frozenset(sessions[end.id]),
                        )
                        for end in carrier.ends
                    ),
                )
            )
        return facts

    # -- execution -----------------------------------------------------------------------------------------------

    async def execute_teardown(self, plan: TeardownPlan) -> None:
        """Run the steps in order; a node already gone is skipped (a rerun, a half-torn state)."""
        for step in plan.steps:
            try:
                if step.action == DELETE:
                    await self.client.delete(kind=step.kind, id=step.id)
                    self.logger.info(f"  deleted {step.kind} {step.label}")
                elif step.action == DECOMMISSION:
                    node = await self.client.get(kind=step.kind, id=step.id, raise_when_missing=False)
                    if node is not None and node.status.value != _DECOMMISSIONED:
                        node.status.value = _DECOMMISSIONED
                        await node.save(update_group_context=False)
                        self.logger.info(f"  decommissioned {step.kind} {step.label}")
            except (NodeNotFoundError, GraphQLError) as exc:
                if isinstance(exc, GraphQLError) and "not found" not in str(exc).lower():
                    raise
                self.logger.warning(f"  {step.kind} {step.label}: already gone ({exc})")
        for node_id, reason in plan.kept:
            self.logger.info(f"  kept {node_id}: {reason}")

    async def teardown_sessions(self, refs: list[SessionRef]) -> tuple[TeardownPlan, set[str]]:
        """Remove the sessions and what they own; returns the plan and the ids of the devices their ends sit on.

        The locks are taken before the facts are read, so a concurrent add_virtual_circuit run for the same circuit
        finishes (or has not started) while the edges are followed.
        """
        if not refs:
            return TeardownPlan(), set()
        async with AsyncExitStack() as stack:
            for end_id in sorted({end_id for ref in refs for end_id in ref.end_ids}):
                await stack.enter_async_context(self.resource_lock(f"vc-port-{end_id}"))  # ty: ignore[unresolved-attribute]
            for ref in sorted(refs, key=lambda r: r.id):
                await stack.enter_async_context(self.resource_lock(f"vc-session-{ref.id}"))  # ty: ignore[unresolved-attribute]
            sessions, prefixes, devices = await self.read_session_facts(refs)
            plan = plan_session_teardown(sessions, prefixes)
            await self.execute_teardown(plan)
        return plan, set(devices.values())

    async def teardown_stale_ends(self, refs: list[SessionRef]) -> tuple[TeardownPlan, set[str]]:
        """Delete the leg interfaces a kept session no longer ends on (``SessionRef.end_ids`` are the stale ones)."""
        refs = [ref for ref in refs if ref.end_ids]
        if not refs:
            return TeardownPlan(), set()
        async with AsyncExitStack() as stack:
            for end_id in sorted({end_id for ref in refs for end_id in ref.end_ids}):
                await stack.enter_async_context(self.resource_lock(f"vc-port-{end_id}"))  # ty: ignore[unresolved-attribute]
            sessions, _, devices = await self.read_session_facts(refs)
            plan = plan_stale_end_teardown(end for session in sessions for end in session.ends)
            await self.execute_teardown(plan)
        return plan, set(devices.values())

    async def teardown_carriers(
        self, connection_id: str, connection_name: str, carriers: Iterable[Any]
    ) -> TeardownPlan:
        """Delete or decommission the carrier the connection generated (and its NIC), when nothing else uses it."""
        facts = await self.read_carrier_facts(connection_name, list(carriers))
        plan = plan_carrier_teardown(connection_id, connection_name, facts)
        await self.execute_teardown(plan)
        return plan
