"""add_virtual_circuit: the BGP sessions of a TopologySessionCircuit of type ``dci`` or ``handoff``.

One generator, one flow, two circuit types. A ``tunnel`` session circuit has no session (the SD-WAN orchestrator owns
its overlay routing); it is a member of the generator's target group like the others (the created-trigger needs the
group), and a run that reaches one logs and stops.

The flow, shared by both types (what differs is a planner per ``circuit_type``: pool, family, peerings):

1. A decommissioned circuit, or one with fewer than two ends, builds nothing and CLEANS UP: the run's tracked cleanup
   removes what an earlier run built (see "Ownership"). Two ends on one device, or more than two ends, is an error.
2. It waits (concurrently: the carriers are independent) for add_circuit on every carrier circuit it rides
   (``derived_underlay``: the carrier terminating on each end port, or on a sub-interface's parent). add_circuit never
   waits for this generator, so there is no cycle. Then it decides whether the ends are carried
   (``utils.circuit_underlay.has_carrier``: a carrier circuit of any status, or a DcimCable, at the port or its
   parent; a hop inside one facility is a cable, not a circuit):

   - dci: BOTH ends need a carrier (a DCI between two border gateways, each on its own fibre);
   - handoff: AT LEAST ONE end needs one (the party end may be a stub, or a provider-hosted appliance, with
     none).

   Not enough carried ends (no carrier found, or one end only of an interconnect) builds nothing, is not an error and
   KEEPS whatever an earlier run built: the run cannot tell "not linked yet / the link is not visible yet" from "the
   link is gone", and deleting a good session on a transient read is worse than keeping a stale one (a check enforces
   carriers; add_circuit fans back out here once a port is linked to a circuit, but nothing reruns this generator
   when a cable is added later, so cable first, circuit after). A carrier that is wholly decommissioned circuits (at
   least one, all decommissioned, and no cable at any end) is explicit: it builds nothing and CLEANS UP; a cable has
   no status, so a cabled end is always live.
3. A port that is also an end of another session circuit is an error: one port, one session.
4. Under a lock per port (sorted, so two runs never wait on each other in a cycle) around the check of step 3 and
   the writes, and under a lock per circuit around the writes, it allocates a /31 (/127) from the pool the circuit type names
   (identifier ``vc-p2p__<id>``, so a re-run gets the same prefix), addresses the two ends, and
5. upserts the peering(s), named ``<vc>-bgp`` (handoff) or ``<vc>-underlay`` and ``<vc>-evpn`` (interconnect).
   Peerings share the ManagedGeneric name space with circuits, hence the suffix.

Every precondition fails before the first write, so a refused run never returns after a partial save, which its
tracked cleanup would turn into deletions.

Handoff: the two ends are the sub-interfaces (``<parent>.<vlan>``) of our edge and of the party-owned device,
inside the circuit's ``ip_namespace``. The /31 comes from the pool named after that namespace (``Handoff-<NAMESPACE>-
IPv4``, data/bootstrap/26_handoff_pools.yml): addresses on both sides come from OUR pool only. One eBGP session
(peering_role ``regular``) between the devices' BGP processes; the party's ASN is its process's ``local_as``. The
session sends no communities (standard or extended): there is nothing to strip yet, communities come in a later change.

DCI: between two EVPN Multi-Site border gateways, both ends physical ports on a device with exactly
one overlay ManagedBGP process. The /31 (/127) comes from the DCI pools (data/bootstrap/20_dci_pools.yml) by the DC
end's ``underlay_protocol``; dual-stack is refused, and so is IPv4 towards a colocation edge, whose VTEP loopback is
IPv6 only. Two sessions on the overlay processes, keyed with one generated per-circuit RoutingPassword
(``<vc>-dci-key``, created once, never rotated): the directly connected underlay eBGP session on the /31
(``dci-underlay``) and the loopback-to-loopback multihop eBGP session carrying only l2vpn evpn (``dci``). The two
physical ports get MTU 9214.

Ownership (the run's tracking group, delete_unused_nodes=True): the prefix, its addresses and the peerings belong to
the circuit. The SDK's ``update_group`` returns without any cleanup when the run tracked nothing (``if not members:
return`` precedes the deletion of the previous members that this run did not reach), and deletes every previous member
the run did not reach otherwise. Cleanup runs for EXACTLY these reasons, and the circuit (the run's target) is added to
the group only on them and on a run that builds:

- the circuit is decommissioned,
- it has fewer than two ends,
- its whole carrier is decommissioned (at least one carrier circuit, all decommissioned, no cable),

so the group is never empty there and everything an earlier run tracked and this one did not write is deleted; the
circuit is a member of both sets, so it is never deleted itself. A run that builds tracks the circuit for the same
reason (a previous member the run did not reach would be deleted, and the circuit was one). The one path that does NOT
track it is "not enough carried ends / no carrier found" (see step 2): nothing is written before that decision (the
waits and the refetch read only; locks are saved untracked), so the group is empty, ``update_group`` returns at once
and the old outputs stay. Do not move the tracking back above that decision: a run that transiently sees no carrier
(index lag) would then delete a good session.

The end interfaces come from device templates and are written untracked (only when something changed);
devices, BGP processes and the global address families are only referenced. The interconnect's key is untracked too,
deliberately: a tracked key is deleted by any run that stops reaching it, and the next full run would then generate a
NEW secret, rotating a key the far side may already use. Untracked, no cleanup ever touches it, so it is created once
and never rotated. The trade-off: a deleted circuit leaves its ``<vc>-dci-key`` behind (demo data of a circuit that no
longer exists).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any, ClassVar

from infrahub_sdk.protocols import CoreIPPrefixPool

from utils.circuit_underlay import (
    BGP_PROCESS,
    DCI_TYPE,
    DECOMMISSIONED,
    HANDOFF_TYPE,
    SESSION_CIRCUIT,
    derived_underlay,
    has_carrier,
    overlay_processes,
    port_cabled,
    session_circuits_on,
    subinterface_vlan,
)
from utils.data_cleaning import clean_data
from utils.limits import DCI_TECHNICAL_IPV4_POOL, DCI_TECHNICAL_IPV6_POOL, HANDOFF_POOL_TEMPLATE, UNDERLAY_MTU

from ..common import CommonGenerator
from ..connections import CablingMixin
from ..helpers.vrf import VrfMixin
from ..pools import PoolMixin
from ..protocols import DcimPhysicalInterface, DcimVirtualInterface
from ..routing import EVPN_AF, IPV4_UNICAST_AF, IPV6_UNICAST_AF, RoutingMixin, _dci_key_name
from .circuit import CARRIER_CIRCUIT_GENERATOR

# DCI P2P pools (data/bootstrap/20_dci_pools.yml) by address family, with the prefix length allocated from them.
_DCI_POOLS: dict[int, tuple[str, int]] = {4: (DCI_TECHNICAL_IPV4_POOL, 31), 6: (DCI_TECHNICAL_IPV6_POOL, 127)}
_UNICAST_AF: dict[int, tuple[str, str]] = {4: IPV4_UNICAST_AF, 6: IPV6_UNICAST_AF}
_DCI_UNDERLAY_MAXIMUM_ROUTES = 16
# Loopback roles an EVPN session may source from, in order of preference. Never the multisite-vip, shared by every
# border gateway of a site (the query does not select it either).
_SESSION_LOOPBACK_ROLES = ("loopback-vtep", "loopback")

# Circuit status -> the status of the interfaces it lands on (templates render a non-active interface shut).
_INTERFACE_STATUS_FOR_CIRCUIT = {
    "provisioning": "provisioning",
    "active": "active",
    "maintenance": "maintenance",
    "down": "outage",
}


def handoff_pool_name(namespace: str) -> str:
    """The /31 pool of an environment namespace (data/bootstrap/26_handoff_pools.yml)."""
    return HANDOFF_POOL_TEMPLATE.format(namespace=namespace)


@dataclass(frozen=True)
class Loopback:
    """A loopback of an end device with its address ("10.0.0.1/32")."""

    id: str
    name: str
    role: str
    address: str

    @property
    def family(self) -> int:
        return 6 if ":" in self.address else 4


@dataclass(frozen=True)
class SessionEnd:
    """One end of a session circuit, parsed from the cleaned query payload."""

    port_id: str
    port_name: str
    port_kind: str
    device_id: str
    device_name: str
    # The device's deployment; a TopologyDataCenter also gives its name and underlay_protocol.
    deployment_kind: str
    deployment_name: str
    underlay_protocol: str | None
    # (id, name) of every ManagedBGP process on the device, and the ids of those of process_role overlay.
    processes: tuple[tuple[str, str], ...]
    overlay_ids: tuple[str, ...]
    loopbacks: tuple[Loopback, ...]

    @property
    def label(self) -> str:
        return f"{self.device_name}/{self.port_name}"

    @property
    def is_dc(self) -> bool:
        return self.deployment_kind == "TopologyDataCenter"

    def overlay_process(self) -> str | None:
        """Id of the device's single overlay-role ManagedBGP process, else None."""
        return self.overlay_ids[0] if len(self.overlay_ids) == 1 else None

    def session_process(self) -> str | None:
        """Id of the process a session runs on: the single overlay one, else the device's only ManagedBGP."""
        overlay = self.overlay_process()
        if overlay is not None:
            return overlay
        return self.processes[0][0] if len(self.processes) == 1 else None

    def session_loopback(self, family: int) -> Loopback | None:
        """The first loopback of the preferred role that has an address of this family; never the multisite-vip."""
        for role in _SESSION_LOOPBACK_ROLES:
            for loopback in sorted(self.loopbacks, key=lambda lb: lb.name):
                if loopback.role == role and loopback.family == family:
                    return loopback
        return None

    @classmethod
    def parse(cls, port: dict[str, Any]) -> SessionEnd | None:
        """From a cleaned port dict; None for an empty slot."""
        if not isinstance(port, dict) or not port.get("id"):
            return None
        device = port.get("device") or {}
        deployment = device.get("deployment") or {}
        is_dc = deployment.get("typename") == "TopologyDataCenter"
        loopbacks = []
        for interface in device.get("interfaces") or []:
            address = interface.get("ip_address") or {}
            if interface.get("typename") == "DcimVirtualInterface" and address.get("address"):
                loopbacks.append(
                    Loopback(
                        id=interface["id"],
                        name=interface.get("name") or "",
                        role=interface.get("role") or "",
                        address=address["address"],
                    )
                )
        return cls(
            port_id=port["id"],
            port_name=port.get("name") or "?",
            port_kind=port.get("typename") or "",
            device_id=device.get("id") or "",
            device_name=device.get("name") or "?",
            deployment_kind=deployment.get("typename") or "",
            deployment_name=(deployment.get("name") or "") if is_dc else "",
            underlay_protocol=deployment.get("underlay_protocol") if is_dc else None,
            processes=tuple(
                (cap["id"], cap.get("name") or "")
                for cap in device.get("capabilities") or []
                if isinstance(cap, dict) and cap.get("typename") == BGP_PROCESS and cap.get("id")
            ),
            overlay_ids=tuple(process["id"] for process in overlay_processes(device) if process.get("id")),
            loopbacks=tuple(loopbacks),
        )


@dataclass(frozen=True)
class PeeringPlan:
    """One ManagedBGPPeering to upsert; the first ``process_ids`` of the plan are its processes."""

    suffix: str
    description: str
    peering_role: str
    session_type: str
    # (afi, safi) of the one address family the session carries.
    address_family: tuple[str, str]
    interface_ids: tuple[str, ...]
    # Directly connected eBGP by default; only the EVPN multihop session overrides.
    ttl: int = 1
    bfd_enabled: bool = True
    send_community: bool = True
    send_extended_community: bool = False
    maximum_routes: int | None = None


@dataclass(frozen=True)
class SessionPlan:
    """What a kind builds, decided before any write."""

    # The word the interface descriptions start with ("DCI", "Handoff").
    label: str
    # Lower address first.
    first: SessionEnd
    second: SessionEnd
    port_kind: type[DcimPhysicalInterface] | type[DcimVirtualInterface]
    pool_name: str
    prefix_length: int
    process_ids: tuple[str, str]
    peerings: tuple[PeeringPlan, ...]
    # The environment namespace the /31 lives in when it is not ``default`` (a handoff's): both ends need an instance
    # of it before the addresses are written.
    namespace: str | None = None
    # Interconnect: the per-circuit key to ensure. Handoff: the circuit's own optional key, by id.
    key_name: str | None = None
    password_id: str | None = None
    mtu: int | None = None


class VirtualCircuitSessionGenerator(CablingMixin, PoolMixin, RoutingMixin, VrfMixin, CommonGenerator):
    """add_virtual_circuit: the BGP session(s) of an interconnect or handoff circuit."""

    client: Any

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Devices on the ends of a handoff circuit, now or before: their VRF instances are converged once the run's
        # tracked cleanup has removed what the circuit no longer holds (see run()). One instance is one run.
        self._vrf_device_ids: set[str] = set()

    async def run(self, identifier: str, data: dict[str, Any] | None = None) -> None:
        """Run the generator, then converge the VRF instances of the handoff's end devices.

        The /31 a decommissioned or shrunk circuit held is deleted by the run's cleanup, after ``generate()``; only
        then does the device no longer demand its namespace, so the pass that prunes the instance runs here. It also
        follows a circuit whose namespace changed. An interconnect is in ``default``: no demand, no pass. Accepted
        gap: a port taken off the circuit is no longer listed, so its device is reconciled by its next pass
        (a segment run, or a handoff) and the check reports the leftover instance until then.
        """
        await super().run(identifier, data)
        await self.reconcile_vrfs(self._vrf_device_ids)

    def _note_end_devices(self, circuit: dict[str, Any]) -> None:
        """Remember the devices on the circuit's listed ports (a handoff only)."""
        if circuit.get("circuit_type") != HANDOFF_TYPE:
            return
        self._vrf_device_ids.update(
            device_id
            for port in circuit.get("capabilities") or []
            if isinstance(port, dict) and (device_id := (port.get("device") or {}).get("id"))
        )

    async def generate(self, data: dict[str, Any]) -> None:
        circuits = clean_data(data).get(SESSION_CIRCUIT) or []
        if not circuits:
            self.logger.error(f"No {SESSION_CIRCUIT} data in GraphQL response")
            return
        circuit = circuits[0]
        name: str = circuit.get("name") or ""
        if not name:
            self.logger.error(f"{SESSION_CIRCUIT} missing name")
            return
        circuit_type = circuit.get("circuit_type") or ""
        planner = self._PLANNERS.get(circuit_type)
        if planner is None:
            self.logger.info(f"Virtual circuit {name} ({circuit_type}): no session is generated for this circuit type")
            return
        self.logger.info(
            f"Processing virtual circuit {name} ({circuit_type}, status={circuit.get('status') or '?'}); "
            f"carrier: {self._carrier_label(circuit)}"
        )

        self._note_end_devices(circuit)
        ends = self._ends_or_none(circuit, name)
        if ends is None:
            self._track_circuit(circuit["id"])
            return
        # The carriers are independent: wait for all of them at once. The data built from is the last refetch, in
        # carrier order (every refetch re-collects the whole query, so the last one is the freshest).
        refetched = await asyncio.gather(
            *(
                self.wait_for_parent_generator_and_refetch(CARRIER_CIRCUIT_GENERATOR, carrier["id"])
                for carrier in derived_underlay(circuit)
            )
        )
        refreshed = next((result for result in reversed(refetched) if result), None)
        if refreshed is not None:
            circuits = clean_data(refreshed).get(SESSION_CIRCUIT) or []
            if not circuits:
                self.logger.error(f"{name}: not found when refetched after its carrier's generator ran")
                return
            circuit = circuits[0]
            self._note_end_devices(circuit)
            ends = self._ends_or_none(circuit, name)
            if ends is None:
                self._track_circuit(circuit["id"])
                return
        ports = [port for port in circuit.get("capabilities") or [] if isinstance(port, dict) and port.get("id")]
        carried = [has_carrier(port) for port in ports]
        if not (all(carried) if circuit_type == DCI_TYPE else any(carried)):
            # Not tracked, on purpose: see "Ownership". The run tracks nothing, so the SDK cleans nothing.
            self.logger.info(
                f"  {name}: ports not linked to a physical carrier yet - nothing built, any session an earlier run "
                "built is kept; built when they are (add_circuit reruns it for a circuit)"
            )
            return
        carriers = derived_underlay(circuit)
        if (
            carriers
            and all(carrier.get("status") == DECOMMISSIONED for carrier in carriers)
            and not any(port_cabled(port) for port in ports)
        ):
            self._track_circuit(circuit["id"])
            self.logger.info(f"  {name}: its whole carrier is decommissioned - no session; the old one is cleaned up")
            return

        self._track_circuit(circuit["id"])
        plan = planner(self, circuit, name, ends)
        async with self._hold_ports(ends):
            await self._refuse_shared_ports(circuit["id"], name, ends)
            pool = await self.client.get(kind=CoreIPPrefixPool, name__value=plan.pool_name, raise_when_missing=False)
            if pool is None:
                self.fail(f"{name}: pool '{plan.pool_name}' not found (data/bootstrap/)")
            await self._write_session(circuit, name, plan, pool)
        self.logger.info(f"Virtual circuit {name} — completed")

    def _track_circuit(self, circuit_id: str) -> None:
        """Make the circuit a member of the run's group, so the SDK's cleanup runs (see "Ownership" above).

        infrahub_sdk's update_group() does no cleanup when a run tracked nothing, so every path that must clean up an
        earlier run's outputs tracks the circuit first.
        """
        self.client.group_context.related_node_ids.append(circuit_id)

    @asynccontextmanager
    async def _hold_ports(self, ends: list[SessionEnd]) -> AsyncGenerator[None]:
        """A lock per end port, taken in sorted order, around the shared-port check AND the writes.

        The check reads other circuits and the writes address the ports: two circuits created together on one port
        must not both pass the check before either has written. Sorted keys mean two runs never hold one port each
        while waiting for the other's.
        """
        async with AsyncExitStack() as stack:
            for port_id in sorted({end.port_id for end in ends}):
                await stack.enter_async_context(self.resource_lock(f"vc-port-{port_id}"))
            yield

    # -- shared preconditions ------------------------------------------------------------------------------------

    @staticmethod
    def _carrier_label(circuit: dict[str, Any]) -> str:
        """The carrier circuits and the cabled ports (``device:port``) under the circuit's ends, for the log."""
        circuits = [c.get("circuit_id") or c.get("name") or c["id"] for c in derived_underlay(circuit)]
        cables = [
            f"cable {(port.get('device') or {}).get('name') or '?'}:{port.get('name') or '?'}"
            for port in circuit.get("capabilities") or []
            if isinstance(port, dict) and port_cabled(port)
        ]
        return ", ".join([*circuits, *cables]) or "none"

    def _ends_or_none(self, circuit: dict[str, Any], name: str) -> list[SessionEnd] | None:
        """The circuit's two ends, or None when there is nothing to build (decommissioned, or an end missing).

        Not an error: the tracked cleanup then removes what an earlier run built. Two ends on one device, or more
        than two ends, is.
        """
        if circuit.get("status") == DECOMMISSIONED:
            self.logger.info(f"  {name} is decommissioned - no session; the one an earlier run built is cleaned up")
            return None
        ends: dict[str, SessionEnd] = {}
        for port in circuit.get("capabilities") or []:
            end = SessionEnd.parse(port)
            if end is not None:
                ends.setdefault(end.port_id, end)
        if len(ends) < 2:
            self.logger.info(
                f"  {name}: {len(ends)} end(s), a session needs 2 - nothing built, the old one is cleaned up"
            )
            return None
        labels = ", ".join(end.label for end in ends.values())
        if len(ends) > 2:
            self.fail(f"{name}: a session needs exactly 2 ends, found {len(ends)}: {labels}")
        first, second = ends.values()
        if first.device_id == second.device_id:
            self.fail(f"{name}: the 2 ends must be on 2 different devices, both are on {first.device_name}")
        return list(ends.values())

    async def _refuse_shared_ports(self, circuit_id: str, name: str, ends: list[SessionEnd]) -> None:
        """One port, one session: refuse an end that is also an end of another live interconnect or handoff circuit.

        The read is inside the per-port lock on purpose (race guard), only the gather of the two session kinds is
        shared (session_circuits_on). A decommissioned circuit that kept its ports does not hold them.
        """
        port_ids = [end.port_id for end in ends]
        others = {
            other.name.value
            for other in await session_circuits_on(self.client, port_ids)
            if other.id != circuit_id and other.status.value != DECOMMISSIONED
        }
        if others:
            self.fail(
                f"{name}: {', '.join(end.label for end in ends)} are also ends of {', '.join(sorted(others))}; "
                "a port carries one interconnect or handoff session"
            )

    def _process_ids(self, name: str, first: SessionEnd, second: SessionEnd) -> tuple[str, str]:
        """The two ends' session processes: the single overlay one, else the device's only ManagedBGP."""
        ids = []
        for end in (first, second):
            process_id = end.session_process()
            if process_id is None:
                names = ", ".join(proc for _, proc in end.processes) or "none"
                self.fail(
                    f"{name}: {end.device_name} needs one overlay ManagedBGP process, or only one ManagedBGP "
                    f"process; has: {names}"
                )
            ids.append(process_id)
        return ids[0], ids[1]

    # -- planners (pure: no client call) -------------------------------------------------------------------------

    def _plan_handoff(self, circuit: dict[str, Any], name: str, ends: list[SessionEnd]) -> SessionPlan:
        """One eBGP session over a /31 of the circuit's namespace pool; our end (overlay process) takes the lower address."""
        namespace = circuit.get("ip_namespace")
        if not isinstance(namespace, dict) or not namespace.get("name"):
            self.fail(f"{name}: a handoff circuit needs its ip_namespace")
        for end in ends:
            if end.port_kind != DcimVirtualInterface.__name__ or subinterface_vlan(end.port_name) is None:
                self.fail(f"{name}: {end.label} must be a DcimVirtualInterface named <parent>.<vlan>")
        first, second = sorted(ends, key=lambda e: (e.overlay_process() is None, e.device_name, e.port_name))
        return SessionPlan(
            label="Handoff",
            first=first,
            second=second,
            port_kind=DcimVirtualInterface,
            pool_name=handoff_pool_name(namespace["name"]),
            prefix_length=31,
            process_ids=self._process_ids(name, first, second),
            namespace=namespace["name"],
            peerings=(
                PeeringPlan(
                    suffix="bgp",
                    description=f"Handoff eBGP {first.label} <-> {second.label} over {name}",
                    peering_role="regular",
                    session_type="EBGP",
                    address_family=IPV4_UNICAST_AF,
                    send_community=False,
                    maximum_routes=circuit.get("maximum_routes"),
                    interface_ids=(first.port_id, second.port_id),
                ),
            ),
            password_id=(circuit.get("password") or {}).get("id"),
        )

    def _plan_interconnect(self, circuit: dict[str, Any], name: str, ends: list[SessionEnd]) -> SessionPlan:
        """The DCI: underlay eBGP on a DCI-pool /31 plus the loopback-to-loopback EVPN session, one per-circuit key."""
        for end in ends:
            if end.port_kind != DcimPhysicalInterface.__name__:
                self.fail(f"{name}: {end.label} is a {end.port_kind or 'unknown kind'}, a DCI ends on physical ports")
            if end.overlay_process() is None:
                self.fail(
                    f"{name}: {end.device_name} needs exactly one overlay ManagedBGP process for the DCI session "
                    "(add_dc/add_pod, add_colocation_zone or add_virtual_colocation creates it)"
                )
        family = self._dci_family(name, ends)
        if family == 4:
            for end in ends:
                if not end.is_dc:
                    self.fail(
                        f"{name}: {end.device_name} is a colocation edge, whose VTEP loopback is IPv6 only; "
                        "an IPv4 DCI cannot reach it"
                    )
        loopbacks: list[Loopback] = []
        for end in ends:
            loopback = end.session_loopback(family)
            if loopback is None:
                self.fail(
                    f"{name}: {end.device_name} has no loopback-vtep or loopback with an IPv{family} address for the "
                    "EVPN session (the multisite-vip is never used)"
                )
            loopbacks.append(loopback)
        # A colocation edge before a DC end (the facility end takes the even address), then by device name.
        first, second = sorted(ends, key=lambda e: (e.is_dc, e.device_name, e.port_name))
        by_port = {end.port_id: lb for end, lb in zip(ends, loopbacks, strict=True)}
        pool_name, prefix_length = _DCI_POOLS[family]
        return SessionPlan(
            label="DCI",
            first=first,
            second=second,
            port_kind=DcimPhysicalInterface,
            pool_name=pool_name,
            prefix_length=prefix_length,
            process_ids=self._process_ids(name, first, second),
            peerings=(
                PeeringPlan(
                    suffix="underlay",
                    description=f"DCI underlay eBGP {first.label} <-> {second.label} over {name}",
                    peering_role="dci-underlay",
                    session_type="EBGP",
                    address_family=_UNICAST_AF[family],
                    maximum_routes=_DCI_UNDERLAY_MAXIMUM_ROUTES,
                    interface_ids=(first.port_id, second.port_id),
                ),
                PeeringPlan(
                    suffix="evpn",
                    description=f"DCI EVPN multihop {first.device_name} <-> {second.device_name} over {name}",
                    peering_role="dci",
                    session_type="EBGP_MULTIHOP",
                    address_family=EVPN_AF,
                    ttl=2,
                    bfd_enabled=False,
                    send_extended_community=True,
                    interface_ids=(by_port[first.port_id].id, by_port[second.port_id].id),
                ),
            ),
            key_name=_dci_key_name(name),
            mtu=UNDERLAY_MTU,
        )

    # circuit_type -> its planner. Keys are exactly SESSION_CIRCUIT_TYPES (a test pins it).
    _PLANNERS: ClassVar[dict[str, Callable[..., SessionPlan]]] = {
        DCI_TYPE: _plan_interconnect,
        HANDOFF_TYPE: _plan_handoff,
    }

    def _dci_family(self, name: str, ends: list[SessionEnd]) -> int:
        """4 or 6, from the DC end's underlay_protocol; dual-stack and disagreeing DCs are refused."""
        dc_ends = [end for end in ends if end.is_dc]
        if not dc_ends:
            self.fail(
                f"{name}: a DCI needs a DC fabric device at one end "
                f"({', '.join(end.device_name for end in ends)} belong to no TopologyDataCenter)"
            )
        protocols = {end.underlay_protocol or "ipv6" for end in dc_ends}
        if len(protocols) > 1:
            self.fail(f"{name}: the DC ends disagree on underlay_protocol ({', '.join(sorted(protocols))})")
        protocol = protocols.pop()
        if protocol == "dual_stack":
            self.fail(
                f"{name}: {dc_ends[0].deployment_name} is dual_stack, a DCI needs one address family: "
                "set its underlay_protocol to ipv4 or ipv6"
            )
        if protocol not in ("ipv4", "ipv6"):
            self.fail(f"{name}: unknown underlay_protocol '{protocol}' on {dc_ends[0].deployment_name}")
        return 4 if protocol == "ipv4" else 6

    # -- the writes ----------------------------------------------------------------------------------------------

    async def _write_session(self, circuit: dict[str, Any], name: str, plan: SessionPlan, pool: Any) -> None:
        """Prefix, addresses, end interfaces, key and peerings, under one lock per circuit."""
        status: str = circuit.get("status") or ""
        async with self.resource_lock(f"vc-session-{circuit['id']}"):
            ports = [plan.first.port_id, plan.second.port_id]
            nodes = {node.id: node for node in await self.client.filters(kind=plan.port_kind, ids=ports)}
            missing = [end.label for end in (plan.first, plan.second) if end.port_id not in nodes]
            if missing:
                self.fail(f"{name}: interface(s) {', '.join(missing)} not found")

            # Every precondition holds - the writes start here. The instance comes first and its device locks are held
            # until the addresses are saved: the transforms refuse a session in a VRF its device lacks, and a pruning
            # pass must not see the instance without its demand.
            needs = {end.device_id: {plan.namespace} for end in (plan.first, plan.second)} if plan.namespace else {}
            async with self.vrfs_held(needs):
                prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=pool,
                    identifier=f"vc-p2p__{circuit['id']}",
                    prefix_length=plan.prefix_length,
                    member_type="address",
                    data={"role": "technical", "is_pool": True, "description": f"{plan.label} {name}"},
                )
                if prefix is None:
                    self.fail(f"{name}: pool '{plan.pool_name}' returned no prefix - exhausted?")
                # Owned by this circuit. allocate_next_ip_prefix() is a pool mutation, not a save(), so the prefix joins
                # the run's tracking group only here.
                self.client.group_context.related_node_ids.append(prefix.id)
                self.logger.info(f"  {plan.label} prefix {prefix.prefix.value} for {name}")

                addresses = await self.upsert_p2p_addresses(prefix, description=f"{plan.label} {name}")
                interface_status = _INTERFACE_STATUS_FOR_CIRCUIT.get(status, "provisioning")
                for end, peer, address in (
                    (plan.first, plan.second, addresses[0]),
                    (plan.second, plan.first, addresses[1]),
                ):
                    await self._update_interface(
                        nodes[end.port_id],
                        address_id=address.id,
                        description=f"{plan.label} {name} to {peer.device_name} {peer.port_name}",
                        status=interface_status,
                        mtu=plan.mtu,
                    )
                    self.logger.info(f"  {end.label} = {address.address.value}")

            password_id = plan.password_id
            if plan.key_name:
                password_id = await self._ensure_routing_password(
                    name=plan.key_name, description=f"Session key of {name}, generated once, never rotated", track=False
                )
            for peering in plan.peerings:
                await self.upsert_bgp_peering(
                    name=f"{name}-{peering.suffix}",
                    description=peering.description,
                    status="active" if status == "active" else "provisioning",
                    session_type=peering.session_type,
                    peering_role=peering.peering_role,
                    process_ids=list(plan.process_ids),
                    address_family_ids=[await self.ensure_address_family(*peering.address_family)],
                    interface_ids=list(peering.interface_ids),
                    password_id=password_id,
                    ttl=peering.ttl,
                    bfd_enabled=peering.bfd_enabled,
                    send_community=peering.send_community,
                    send_extended_community=peering.send_extended_community,
                    maximum_routes=peering.maximum_routes,
                )

    async def _update_interface(
        self, node: Any, *, address_id: str, description: str, status: str, mtu: int | None
    ) -> None:
        """Address an end interface, untracked, and only if something changed.

        The interface comes from the device's template, not from this run: tracked, a later run that stopped
        addressing it would delete the port. A plain save() on the fetched node sends only the modified fields.
        """
        changed = False
        if getattr(getattr(node, "ip_address", None), "id", None) != address_id:
            node.ip_address = address_id
            changed = True
        if node.description.value != description:
            node.description.value = description
            changed = True
        if node.status.value != status:
            node.status.value = status
            changed = True
        if mtu is not None and node.mtu.value != mtu:
            node.mtu.value = mtu
            changed = True
        if changed:
            await node.save(update_group_context=False)
