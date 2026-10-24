"""Routing mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

from utils.limits import WAIT_FOR_PEER_GENERATOR

from .helpers import PendingASRef, RoutingPlanInput, RoutingPlanner, RoutingStrategy
from .helpers.common import retry_delay, save_with_node_not_found_retry
from .helpers.routing import _safe_device_name
from .protocols import (
    DcimVirtualInterface,
    ManagedBGP,
    ManagedBGPPeering,
    ManagedMLAG,
    ManagedOSPF,
    ManagedOSPFPeering,
    RoutingAutonomousSystem,
    RoutingBGPAddressFamily,
    RoutingOSPFInterface,
)
from .shared_routing import SharedRoutingObjectsMixin
from .types import RoutingOptions

EVPN_AF = ("l2vpn", "evpn")
IPV4_UNICAST_AF = ("ipv4", "unicast")
IPV6_UNICAST_AF = ("ipv6", "unicast")
# Attributes a missing address family is created with — the same values the
# bootstrap data file gives them.
_ADDRESS_FAMILY_DATA: dict[tuple[str, str], dict[str, Any]] = {
    IPV4_UNICAST_AF: {"description": "IPv4 unicast"},
    IPV6_UNICAST_AF: {"description": "IPv6 unicast"},
    EVPN_AF: {"description": "L2VPN EVPN overlay", "advertise_all_vni": True, "advertise_default_gw": True},
}


def _dci_key_name(circuit_name: str) -> str:
    """The per-circuit key of a DCI (generators/topology/virtual_circuit.py), not a fabric-wide one."""
    return f"{circuit_name}-dci-key"


class RoutingMixin(SharedRoutingObjectsMixin):
    """Mixin providing routing configuration methods for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``,
    ``deployment_id``, and ``fabric_name`` attributes (all present on
    ``CommonGenerator`` via ``InfrahubGenerator``), and PoolMixin's
    ``resource_lock``.
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    deployment_id: str
    fabric_name: str
    # PoolMixin.resource_lock — a Callable attribute, not a stub, so it never shadows the real one.
    resource_lock: Callable[[str], AbstractAsyncContextManager[None]]

    async def create_routing(
        self,
        bottom_devices: list[str],
        top_devices: list[str],
        options: RoutingOptions | None = None,
        p2p_interfaces: list[tuple[Any, Any]] | None = None,
        bottom_role: str = "",
        top_role: str = "",
    ) -> None:
        """Create routing configuration for a layer pair.

        ``p2p_interfaces`` is the list of ``(src, dst)`` interface pairs returned by
        ``create_cabling``.  Pass ``[]`` when there are no cables yet (process-only
        calls such as the pre-cable spine BGP seeding in pod.py).

        ``bottom_role``/``top_role`` are the caller's known role for every
        device in ``bottom_devices``/``top_devices`` (e.g. "leaf"/"spine").
        Every caller creates or selects these devices for exactly one role,
        so pass it — it's used as the authoritative role source instead of
        querying ``DcimDevice.role``, which has been observed to
        intermittently resolve to None when queried from a different worker
        process than the one that wrote it.
        """
        if options is None:
            options = RoutingOptions()
        if p2p_interfaces is None:
            p2p_interfaces = []
        design = options.get("design") or {}
        routing_strategy = design.get("routing_strategy")
        # An office fabric (options no_overlay) has no overlay BGP, no overlay AS
        # and no EVPN address family — only the OSPF underlay below.
        no_overlay = bool(options.get("no_overlay"))

        if not routing_strategy:
            self.logger.warning("No design or routing strategy provided")
            return
        if routing_strategy not in {s.value for s in RoutingStrategy}:
            self.logger.warning(f"Routing strategy '{routing_strategy}' not supported")
            return

        if no_overlay and options.get("skip_underlay"):
            self.logger.info("No overlay and no underlay requested: no routing to create")
            return

        all_device_names = bottom_devices + top_devices
        self.logger.info(
            f"Creating routing: {len(bottom_devices)} bottom ↔ {len(top_devices)} top device(s) "
            f"[strategy={routing_strategy}]"
        )

        # ================================================================
        # RESOLVE SHARED/CROSS-GENERATOR STATE + DATA COLLECTION
        # ================================================================
        # One combined polling loop for everything this call needs but doesn't
        # own: dc.py-level shared objects (overlay AS, OSPF area, auth keys)
        # and a sibling pod's overlay BGP (pod.py's decentralized mesh cabling
        # queries a sibling pod's spines directly, no dc.py-level wait). A
        # pod's/rack's own task-list check for an in-flight add_dc only catches
        # "still running" — not the narrow window between add_dc's task
        # completing and this call's own queries landing, nor a sibling
        # generator's independent in-flight run. Every such lookup shares one
        # retry loop and one backoff per round instead of each waiting (and
        # re-sleeping) independently in sequence.
        interfaces = [iface for pair in p2p_interfaces for iface in pair]

        needs_overlay_as = (
            not no_overlay
            and not options.get("overlay_as_id")
            and routing_strategy
            in (
                RoutingStrategy.EBGP_IBGP,
                RoutingStrategy.OSPF_IBGP,
            )
        )
        needs_ospf_area = (
            not options.get("ospf_area_id")
            and not options.get("skip_underlay")
            and routing_strategy == RoutingStrategy.OSPF_IBGP
        )
        # Shared underlay/overlay BGP/OSPF auth keys — created once by the DC generator.
        # Unlike overlay AS / OSPF area, a missing key is non-fatal: auth is a hardening
        # add-on, not required for connectivity, so routing creation proceeds without it
        # even if the retry loop below never finds them.
        needs_underlay_password = not options.get("underlay_password_id") and not options.get("skip_underlay")
        needs_overlay_password = not no_overlay and not options.get("overlay_password_id")

        overlay_as_id = options.get("overlay_as_id")
        ospf_area_id = options.get("ospf_area_id")
        underlay_password_id = options.get("underlay_password_id")
        overlay_password_id = options.get("overlay_password_id")

        top_device_set = set(top_devices)
        all_bgp: list[Any] = []
        loopback_interfaces: list[Any] = []

        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            if (needs_overlay_as and not overlay_as_id) or (needs_ospf_area and not ospf_area_id):
                resolved_overlay_as_id, resolved_ospf_area_id = await self._resolve_shared_objects(routing_strategy)
                overlay_as_id = overlay_as_id or resolved_overlay_as_id
                ospf_area_id = ospf_area_id or resolved_ospf_area_id

            if (needs_underlay_password and not underlay_password_id) or (
                needs_overlay_password and not overlay_password_id
            ):
                resolved_underlay_password_id, resolved_overlay_password_id = await self._resolve_shared_passwords()
                underlay_password_id = underlay_password_id or resolved_underlay_password_id
                overlay_password_id = overlay_password_id or resolved_overlay_password_id

            all_bgp, loopback_interfaces, mlag_domains = await asyncio.gather(
                self.client.filters(
                    kind=ManagedBGP,
                    capabilities__name__values=all_device_names,
                    include=["local_as", "capabilities"],
                    prefetch_relationships=True,
                ),
                self.client.filters(
                    kind=DcimVirtualInterface,
                    device__name__values=all_device_names,
                    role__value="loopback",
                    include=["device", "ip_address"],
                    prefetch_relationships=True,
                ),
                self.client.filters(
                    kind=ManagedMLAG,
                    capabilities__name__values=all_device_names,
                    include=["capabilities"],
                    prefetch_relationships=True,
                ),
            )
            overlay_device_names = {
                name for b in all_bgp if b.process_role.value == "overlay" and (name := _safe_device_name(b))
            }
            missing_overlay_bgp = set() if no_overlay else top_device_set - overlay_device_names

            still_missing = (
                (needs_overlay_as and not overlay_as_id)
                or (needs_ospf_area and not ospf_area_id)
                or bool(missing_overlay_bgp)
            )
            if not still_missing:
                break
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                waiting_on = []
                if needs_overlay_as and not overlay_as_id:
                    waiting_on.append("shared overlay AS")
                if needs_ospf_area and not ospf_area_id:
                    waiting_on.append("shared OSPF area")
                if missing_overlay_bgp:
                    waiting_on.append(f"overlay BGP for {sorted(missing_overlay_bgp)}")
                delay = retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"Waiting on: {', '.join(waiting_on)} — retrying in {delay:.2f}s "
                    f"(attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)

        if needs_overlay_as:
            if not overlay_as_id:
                self.logger.error(
                    f"Shared overlay AS not found for {self.fabric_name}. The DC generator must run first to create it."
                )
                return
            options["overlay_as_id"] = overlay_as_id

        if needs_ospf_area:
            if not ospf_area_id:
                self.logger.error(
                    f"Shared OSPF area not found for {self.fabric_name}. The DC generator must run first to create it."
                )
                return
            options["ospf_area_id"] = ospf_area_id

        if needs_underlay_password and underlay_password_id:
            options["underlay_password_id"] = underlay_password_id
        if needs_overlay_password and overlay_password_id:
            options["overlay_password_id"] = overlay_password_id

        # The shared DC-level objects (overlay AS, OSPF area, auth keys) are
        # only referenced here, never tracked: add_dc/dc_pod_cascade own them
        # and track them on every run (_create_shared_routing_objects). Tracked
        # by a pod or rack run too, that run's cleanup would delete them the
        # run it stops reaching them.

        underlay_type = routing_strategy.split("-")[0]

        if underlay_type == "ospf":
            underlay = await self.client.filters(
                kind=ManagedOSPF,
                capabilities__name__values=all_device_names,
            )
        else:
            underlay = [b for b in all_bgp if b.process_role.value == "underlay"]

        # Overlay is always iBGP/eBGP (ManagedBGP) — only the underlay can be OSPF.
        overlay = [b for b in all_bgp if b.process_role.value == "overlay"]

        # MLAG-paired devices among the ones being routed share ONE underlay
        # ASN (.dev/bgp.txt) — build device name -> MLAG domain name, used
        # by _plan_ebgp_underlay to group AS allocation. Only devices with
        # exactly 2 member names resolve (a ManagedMLAG's capabilities are
        # its 2 physical peers, see generators/devices.py's _ensure_mlag_pairs).
        mlag_pairs: dict[str, str] = {}
        for domain in mlag_domains:
            domain_name = domain.name.value
            member_names = [peer.display_label for peer in domain.capabilities.peers]
            if len(member_names) == 2:
                for member_name in member_names:
                    mlag_pairs[member_name] = domain_name

        self.logger.info(
            f"Collected: {len(interfaces)} P2P interface(s), "
            f"{len(loopback_interfaces)} loopback(s), "
            f"{len(underlay)} existing underlay, {len(overlay)} existing overlay, "
            f"{len(mlag_pairs)} MLAG-paired device(s)"
        )

        # ================================================================
        # PHASE 2: BUILD ROUTING PLAN (pure helper, no DB access)
        # ================================================================

        # strict=True: refuse to skip a device out of routing. The planner's four
        # "no router-id / no loopback IP" branches otherwise warn and continue,
        # leaving a device created, cabled and addressed but with no BGP or OSPF
        # process — the DC12 shape: indistinguishable from a healthy device
        # unless you count processes, and the task still reports success. Strict
        # mode was already implemented and unit-tested (tests/unit/
        # test_routing_strict_mode.py) but no caller had ever opted in, so every
        # production run took the tolerant path. A full 30_all load hits none of
        # the four branches, so this changes no current outcome — it only stops
        # the next regression from being invisible.
        planner = RoutingPlanner(deployment_id=self.deployment_id, logger=self.logger, strict=True)

        evpn_af_id = "" if no_overlay else await self.ensure_address_family(*EVPN_AF)

        plan = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=bottom_devices,
                top_devices=top_devices,
                bottom_role=bottom_role,
                top_role=top_role,
                underlay=underlay,
                overlay=overlay,
                interfaces=interfaces,
                loopback_interfaces=loopback_interfaces,
                options={**options},
                routing_strategy=routing_strategy,
                evpn_af_id=evpn_af_id,
                underlay_password_id=options.get("underlay_password_id") or "",
                overlay_password_id=options.get("overlay_password_id") or "",
                mlag_pairs=mlag_pairs,
            )
        )

        # ================================================================
        # PHASE 3: CREATE SDK OBJECTS, SAVE IN DEPENDENCY ORDER
        # ================================================================

        # Step 1: Create + save AS objects, build device -> AS ID mapping
        device_to_as_id = await self._save_autonomous_systems(plan.autonomous_systems)

        # Step 2: Resolve PendingASRef placeholders in BGP processes
        for bgp in plan.bgp_processes:
            local_as = bgp.get("local_as")
            if isinstance(local_as, PendingASRef):
                as_id = device_to_as_id.get(local_as.device)
                if as_id:
                    bgp["local_as"] = {"id": as_id}
                else:
                    self.logger.error(f"Cannot resolve AS for {bgp.get('name')}")

        # Step 3: Create BGP + OSPF SDK objects, save sequentially.
        # Every process is upserted by its deterministic HFID (name). Re-saving an
        # existing ManagedBGP is safe: local_as is cardinality-one and upserts cleanly
        # (replaces, never duplicates — verified on Infrahub 1.9.6), so the upsert
        # also (re)registers it in the group context with no new/existing split.
        plan.bgp_processes = [await self.client.create(kind=ManagedBGP, data=d) for d in plan.bgp_processes]
        plan.ospf_processes = [await self.client.create(kind=ManagedOSPF, data=d) for d in plan.ospf_processes]
        for obj in plan.bgp_processes + plan.ospf_processes:
            await obj.save(allow_upsert=True)
            self.logger.info(f"  Saved: {getattr(getattr(obj, 'name', None), 'value', obj.id)}")

        # Step 4: Resolve HFID refs to processes saved in this same run into direct id
        # refs. HFID lookups go through the search index, which can lag just behind a
        # write that landed a moment earlier in this run (NODE_NOT_FOUND). Refs to
        # processes from earlier generator runs are left as HFID — no race there,
        # those have had plenty of time to be indexed.
        process_id_by_name = {obj.name.value: obj.id for obj in plan.bgp_processes + plan.ospf_processes}

        def _resolve_hfid(ref: dict) -> dict:
            fresh_id = process_id_by_name.get(ref.get("hfid"))
            return {"id": fresh_id} if fresh_id else ref

        for peering in plan.bgp_peerings:
            peering["bgp_processes"] = [_resolve_hfid(ref) for ref in peering["bgp_processes"]]
        for ospf_peering in plan.ospf_peerings:
            ospf_peering["ospf_process"] = [_resolve_hfid(ref) for ref in ospf_peering["ospf_process"]]

        # Step 5: Create peering + OSPF interface SDK objects, save sequentially.
        # ospf_interfaces are created before ospf_peerings so the latter's
        # ospf_interface HFID refs can be resolved to direct ids the same way
        # process refs are resolved above — they reference objects created in
        # this same run, so the same NODE_NOT_FOUND-avoidance applies.
        # Peerings additionally retry on save (save_with_node_not_found_retry): unlike
        # processes, a peering can reference a process id created by a DIFFERENT,
        # concurrently-running create_routing() call against the same shared
        # device (e.g. every pod's spines peering to the same super-spines) — a
        # write-contention race the HFID resolution above doesn't cover.
        plan.bgp_peerings = [await self.client.create(kind=ManagedBGPPeering, data=d) for d in plan.bgp_peerings]
        plan.ospf_interfaces = [
            await self.client.create(kind=RoutingOSPFInterface, data=d) for d in plan.ospf_interfaces
        ]
        for obj in plan.bgp_peerings:
            await save_with_node_not_found_retry(obj, self.logger)
        for obj in plan.ospf_interfaces:
            await obj.save(allow_upsert=True)
            self.logger.info(f"  Saved: {getattr(getattr(obj, 'name', None), 'value', obj.id)}")

        ospf_iface_id_by_name = {obj.name.value: obj.id for obj in plan.ospf_interfaces}
        for ospf_peering in plan.ospf_peerings:
            ospf_peering["ospf_interface"] = [
                {"id": ospf_iface_id_by_name[ref["hfid"]]} if ref.get("hfid") in ospf_iface_id_by_name else ref
                for ref in ospf_peering["ospf_interface"]
            ]

        plan.ospf_peerings = [await self.client.create(kind=ManagedOSPFPeering, data=d) for d in plan.ospf_peerings]
        for obj in plan.ospf_peerings:
            await save_with_node_not_found_retry(obj, self.logger)

        total = (
            len(plan.autonomous_systems)
            + len(plan.bgp_processes)
            + len(plan.ospf_processes)
            + len(plan.ospf_interfaces)
            + len(plan.bgp_peerings)
            + len(plan.ospf_peerings)
        )
        self.logger.info(f"Routing completed: {total} object(s) saved")

    # ----------------------------------------------------------------
    # Batch save helpers
    # ----------------------------------------------------------------

    async def _save_autonomous_systems(self, as_dicts: list[dict]) -> dict[str, str]:
        """Save AS objects. Returns device_name -> AS ID mapping.

        Existing AS (with _existing_id): register in group context for tracking — no DB write needed.
        New AS (with from_pool): allocate from pool and save.
        """
        device_to_as_id: dict[str, str] = {}
        if not as_dicts:
            return device_to_as_id

        for as_dict in as_dicts:
            device_name = as_dict.get("_for_device", "")
            existing_id = as_dict.get("_existing_id")

            if existing_id:
                self.client.group_context.related_node_ids.append(existing_id)
                device_to_as_id[device_name] = existing_id
                self.logger.info(f"  Tracked existing AS for {device_name}")
            else:
                data = {k: v for k, v in as_dict.items() if not k.startswith("_")}
                obj = await self.client.create(kind=RoutingAutonomousSystem, data=data)
                await obj.save(allow_upsert=True)
                device_to_as_id[device_name] = obj.id
                self.logger.info(f"  Created AS{obj.asn.value}")

        return device_to_as_id

    async def bgp_processes_ready(self, device_names: list[str], process_role: str) -> bool:
        """Whether every named device already has its own ManagedBGP process of this role."""
        processes = await self.client.filters(
            kind=ManagedBGP,
            capabilities__name__values=device_names,
            include=["capabilities"],
            prefetch_relationships=True,
        )
        ready_devices = {
            process.capabilities.peers[0].display_label
            for process in processes
            if process.process_role.value == process_role and len(process.capabilities.peers) == 1
        }
        return set(device_names).issubset(ready_devices)

    async def ensure_address_family(self, afi: str, safi: str) -> str:
        """Find or create a RoutingBGPAddressFamily and return its id, untracked, under a lock.

        data/bootstrap/15_bgp_address_families.yml provides them on main;
        creating one here is only a fallback for a branch cut from a main
        without bootstrap — two such branches each create their own and the
        second merge fails (uniqueness [afi, safi]).

        One global node every fabric, circuit and colocation run references, so no
        run may own it (a tracked one is deleted by whichever run stops
        reaching it). The lock serializes concurrent runs racing to create the
        same one.
        """
        async with self.resource_lock(f"bgp-af-{afi}-{safi}"):
            existing = await self.client.filters(kind=RoutingBGPAddressFamily, afi__value=afi, safi__value=safi)
            if existing:
                return existing[0].id
            family = await self.client.create(
                kind=RoutingBGPAddressFamily,
                data={"afi": afi, "safi": safi, **_ADDRESS_FAMILY_DATA.get((afi, safi), {})},
            )
            await family.save(allow_upsert=True, update_group_context=False)
            self.logger.info(f"Created RoutingBGPAddressFamily {afi}/{safi}")
            return family.id

    async def upsert_bgp_peering(
        self,
        *,
        name: str,
        description: str,
        status: str,
        session_type: str,
        peering_role: str,
        process_ids: list[str],
        address_family_ids: list[str],
        interface_ids: list[str],
        password_id: str | None = None,
        ttl: int = 1,
        bfd_enabled: bool = True,
        send_community: bool = True,
        send_extended_community: bool = True,
        maximum_routes: int | None = None,
    ) -> None:
        """Upsert one ManagedBGPPeering, by default directly connected (ttl 1, BFD, standard and extended communities).

        The shared core of the sessions of a virtual circuit (generators/topology/virtual_circuit.py), the metro's
        edge FABRIC session (add_colocation_zone, add_virtual_colocation) and the office fabric. Peerings share
        the ManagedGeneric name space with circuits: callers suffix or prefix the name (<vc>-evpn, FABRIC-<a>-<b>).
        Processes are passed by id, never looked up by name here (search-index lag on same-run objects).

        ``ttl``, ``bfd_enabled``, ``send_community``, ``send_extended_community`` and ``maximum_routes`` default to
        the directly connected shape (both community flags on; a handoff session turns them off); ``maximum_routes`` is written only when given. ``password_id`` keys the session; a
        session without a fabric key (an SD-WAN edge to its gateway) writes no password at all.
        """
        peering = await self.client.create(
            kind=ManagedBGPPeering,
            data={
                "name": name,
                "description": description,
                "status": status,
                "session_type": session_type,
                "peering_role": peering_role,
                "ttl": ttl,
                "bfd_enabled": bfd_enabled,
                "send_community": send_community,
                "send_extended_community": send_extended_community,
                **({"maximum_routes": maximum_routes} if maximum_routes is not None else {}),
                "address_families": [{"id": af_id} for af_id in address_family_ids],
                "bgp_processes": [{"id": process_id} for process_id in process_ids],
                **({"password": {"id": password_id}} if password_id else {}),
                "capabilities": [{"id": interface_id} for interface_id in interface_ids],
            },
        )
        await save_with_node_not_found_retry(peering, self.logger)
