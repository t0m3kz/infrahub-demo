"""The reading half of the near-port selection of add_provider_connection.

generators/helpers/near_port.py decides from facts; this mixin reads them with the SDK and hands the decision back as
:class:`Resolution`: the ends of the one dedicated link and the carrier it rides (an existing one, or None: the generator
creates it). Nothing is written here.

Where the near port comes from, first match:

1. the explicit ``near_port`` of the request (never replaced; in conflict it is an error);
2. the near end of the connection's own live carrier (an earlier run's output, so a rerun keeps its port);
3. a free port of the near facility with the interface role of the far kind (:func:`near_port.near_role`), on the device
   with the fewest connections.

The reads are made by the caller under the lock ``pc-near-<near facility id>``: a port is free when no carrier, cable,
sub-interface, LAG or session sits on it, and the carrier the generator creates is the claim that makes it busy for the
next connection. The load (connections per device) comes from the query payload: it only balances, it never decides
whether a port is free, so a stale value costs balance, not correctness.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from utils.circuit_underlay import DECOMMISSIONED, SESSION_CIRCUIT, SESSION_CIRCUIT_TYPES

from ..protocols import DcimInterface, DcimPhysicalInterface, DcimVirtualInterface, TopologyCarrierCircuit
from .near_port import (
    CarrierFacts,
    NearPortError,
    PortFacts,
    carrier_type_for,
    choose_far_port,
    choose_near_port,
    connection_load,
    find_reusable_carrier,
    near_role,
)
from .provider_connection import (
    NIC_FAR_KINDS,
    ConnectionSpec,
    DedicatedEnds,
    Port,
    choose_stub,
    nic_name,
    own_carriers,
    port_of,
    reused_ends,
)

_PORT_INCLUDE = ["device", "cable", "lag"]


def _peer_id(rel: Any) -> str | None:
    """The peer id of a one-relationship, None when unset."""
    return rel.id if rel is not None and getattr(rel, "initialized", True) and getattr(rel, "id", None) else None


def _peer_ids(rel: Any) -> list[str]:
    """The peer ids of a many-relationship that was included."""
    return [peer.id for peer in getattr(rel, "peers", None) or []]


@dataclass(frozen=True)
class Resolution:
    """The ends of the dedicated link and its carrier."""

    ends: DedicatedEnds
    carrier_id: str | None  # None: the generator creates the carrier (and the far NIC when ``ends.far`` is None)
    carrier_type: str


class NearPortFactsMixin:
    """Host: a CommonGenerator (``client``)."""

    client: Any

    async def resolve_dedicated_ends(self, spec: ConnectionSpec) -> Resolution:
        """The near port, the far device and port, and the carrier of a dedicated connection; ValueError (a
        :class:`NearPortError` for the port choice) when none can be found. Reads only."""
        own = own_carriers(spec)
        if spec.near_port_id:
            return await self._from_explicit_port(spec)
        if len(own) > 1:
            names = ", ".join(sorted(carrier.name for carrier, _, _ in own))
            raise NearPortError(
                f"the connection lists several live carrier circuits ({names}); set near_port to the one it rides"
            )
        if own:
            carrier, near, far = own[0]
            return Resolution(reused_ends(spec, carrier.name, [near, far]), carrier.id, carrier.circuit_type)
        if not spec.creates_carrier_ends:
            raise NearPortError(
                f"the far facility is provisioned outside this model ({spec.far_kind}): author its carrier circuit "
                "and set near_port to the near end of it"
            )
        near = await self._select_near_port(spec)
        return await self._created(spec, near)

    # -- the three ways to a near port ----------------------------------------------------------------------------

    async def _from_explicit_port(self, spec: ConnectionSpec) -> Resolution:
        assert spec.near_port_id
        own_end_ids = self._own_end_ids(spec)
        (facts,) = await self._port_facts(spec, [spec.near_port_id], own_end_ids, strict=True)
        near_device = next((d for d in spec.near_devices if d[0] == facts.device_id), None)
        if near_device is None:
            raise NearPortError(f"near_port {facts.label} is not on a device of the near facility")
        near = port_of(spec, facts.id, facts.name, facts.device_id, facts.device_name)
        choose_near_port((), {}, explicit=facts)  # raises on a conflict
        carriers = await self._carriers_on_port(spec, near.id)
        reusable = find_reusable_carrier(
            [facts_ for facts_, _ in carriers], far_facility_id=spec.far_id or "", connection_id=spec.id
        )
        if reusable is None:
            if not spec.creates_carrier_ends:
                raise NearPortError(
                    f"no carrier circuit on near_port {facts.label} reaches the far facility ({spec.far_kind} is "
                    "provisioned outside this model: author the carrier)"
                )
            return await self._created(spec, near)
        ends = reused_ends(spec, reusable.name, dict(carriers)[reusable])
        return Resolution(ends, reusable.id, reusable.circuit_type)

    async def _select_near_port(self, spec: ConnectionSpec) -> Port:
        """The free port of the right role on the least loaded device of the near facility."""
        role = near_role(spec.far_kind)
        device_ids = sorted(spec.near_device_ids)
        nodes = (
            await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=device_ids, role__value=role, include=_PORT_INCLUDE
            )
            if device_ids
            else []
        )
        facts = await self._port_facts(spec, [], self._own_end_ids(spec), nodes=nodes)
        chosen = choose_near_port(facts, connection_load(spec.near_load))
        return port_of(spec, chosen.id, chosen.name, chosen.device_id, chosen.device_name)

    async def _created(self, spec: ConnectionSpec, near: Port) -> Resolution:
        """No carrier to reuse: the far port the new carrier will end on."""
        stub = choose_stub(spec)
        carrier_type = carrier_type_for(spec.far_kind)
        if spec.far_kind in NIC_FAR_KINDS:
            found = await self.client.filters(
                kind=DcimVirtualInterface, device__ids=[stub.id], name__values=[nic_name(spec.name)]
            )
            far = port_of(spec, found[0].id, found[0].name.value, stub.id, stub.name) if found else None
        else:
            nodes = await self.client.filters(kind=DcimPhysicalInterface, device__ids=[stub.id], include=_PORT_INCLUDE)
            chosen = choose_far_port(await self._port_facts(spec, [], self._own_end_ids(spec), nodes=nodes))
            far = port_of(spec, chosen.id, chosen.name, stub.id, stub.name)
        return Resolution(DedicatedEnds(near, stub, far), None, carrier_type)

    # -- facts ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _own_end_ids(spec: ConnectionSpec) -> frozenset[str]:
        return frozenset(end.id for session in spec.sessions for end in session.ends)

    async def _port_facts(
        self,
        spec: ConnectionSpec,
        port_ids: list[str],
        own_end_ids: frozenset[str],
        *,
        nodes: list[Any] | None = None,
        strict: bool = False,
    ) -> list[PortFacts]:
        """The :class:`PortFacts` of the given ports (by id, or already read as ``nodes``).

        Everything that belongs to this connection (its carrier, its sub-interfaces and sessions) is left out of the
        counts: a rerun sees its own port as free.
        """
        if nodes is None:
            nodes = await self.client.filters(kind=DcimPhysicalInterface, ids=port_ids, include=_PORT_INCLUDE)
            if not nodes:  # a virtual NIC (a provider-hosted edge): no cable, no LAG
                nodes = await self.client.filters(kind=DcimInterface, ids=port_ids, include=["device"])
            if strict and len(nodes) != len(port_ids):
                raise NearPortError(f"near_port {port_ids[0]} not found")
        if not nodes:
            return []
        ids = [node.id for node in nodes]
        carriers = await self.client.filters(
            kind=TopologyCarrierCircuit, capabilities__ids=ids, include=["capabilities", "provider_connections"]
        )
        foreign_carriers: dict[str, int] = defaultdict(int)
        for carrier in carriers:
            if carrier.status.value == DECOMMISSIONED or spec.id in _peer_ids(carrier.provider_connections):
                continue
            for end_id in _peer_ids(carrier.capabilities):
                foreign_carriers[end_id] += 1
        foreign_subs: dict[str, int] = defaultdict(int)
        for sub in await self.client.filters(kind=DcimVirtualInterface, parent_interface__ids=ids):
            parent = _peer_id(getattr(sub, "parent_interface", None))
            if parent and sub.id not in own_end_ids:
                foreign_subs[parent] += 1
        foreign_sessions: dict[str, int] = defaultdict(int)
        for session in await self.client.filters(
            kind=SESSION_CIRCUIT,
            capabilities__ids=ids,
            circuit_type__values=list(SESSION_CIRCUIT_TYPES),
            include=["capabilities", "connection"],
        ):
            if _peer_id(getattr(session, "connection", None)) == spec.id:
                continue
            for end_id in _peer_ids(session.capabilities):
                foreign_sessions[end_id] += 1
        names = dict(spec.near_devices) | {stub.id: stub.name for stub in spec.stubs}
        facts = []
        for node in nodes:
            device = getattr(node, "device", None)
            device_id = _peer_id(device) or ""
            role = getattr(node, "role", None)
            facts.append(
                PortFacts(
                    id=node.id,
                    name=node.name.value,
                    device_id=device_id,
                    device_name=names.get(device_id) or getattr(device, "display_label", None) or device_id,
                    role=getattr(role, "value", None),
                    cabled=bool(_peer_id(getattr(node, "cable", None))),
                    lag_member=bool(_peer_id(getattr(node, "lag", None))),
                    foreign_carriers=foreign_carriers[node.id],
                    foreign_sub_interfaces=foreign_subs[node.id],
                    foreign_sessions=foreign_sessions[node.id],
                )
            )
        return facts

    async def _carriers_on_port(self, spec: ConnectionSpec, port_id: str) -> list[tuple[CarrierFacts, list[Port]]]:
        """Every carrier circuit with an end on the port, with its ends as ports of the near and far facility."""
        carriers = await self.client.filters(
            kind=TopologyCarrierCircuit, capabilities__ids=[port_id], include=["capabilities", "provider_connections"]
        )
        end_ids = sorted({end_id for carrier in carriers for end_id in _peer_ids(carrier.capabilities)})
        interfaces = (
            {node.id: node for node in await self.client.filters(kind=DcimInterface, ids=end_ids, include=["device"])}
            if end_ids
            else {}
        )
        names = dict(spec.near_devices) | {stub.id: stub.name for stub in spec.stubs}
        far_id = spec.far_id or ""
        found = []
        for carrier in carriers:
            ends = []
            for end_id in _peer_ids(carrier.capabilities):
                node = interfaces.get(end_id)
                device_id = _peer_id(getattr(node, "device", None)) if node is not None else None
                if node is not None and device_id:
                    ends.append(port_of(spec, end_id, node.name.value, device_id, names.get(device_id, device_id)))
            facts = CarrierFacts(
                id=carrier.id,
                name=carrier.name.value,
                circuit_type=carrier.circuit_type.value,
                live=carrier.status.value != DECOMMISSIONED,
                far_end_deployments=frozenset({far_id}) if far_id in {p.deployment_id for p in ends} else frozenset(),
                claimed_by=frozenset(_peer_ids(carrier.provider_connections)),
            )
            found.append((facts, ends))
        return found
