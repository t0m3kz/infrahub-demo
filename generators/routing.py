"""Routing mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

from .helpers import PendingASRef, RoutingPlanInput, RoutingPlanner, RoutingStrategy
from .helpers.common import retry_delay, save_with_node_not_found_retry
from .helpers.routing import LISP_OVERLAY, _safe_device_name, overlay_control_plane_of
from .protocols import (
    DcimVirtualInterface,
    ManagedBGP,
    ManagedBGPPeering,
    ManagedMLAG,
    ManagedOSPF,
    ManagedOSPFPeering,
    RoutingAutonomousSystem,
    RoutingBGPAddressFamily,
    RoutingOSPFArea,
    RoutingOSPFInterface,
    RoutingPassword,
)
from .types import RoutingOptions

_SHARED_OBJECT_MAX_RETRIES = 10
_SHARED_OBJECT_RETRY_DELAY = 3.0

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


# Names/descriptions of the fabric-wide shared routing objects. They are the
# idempotency keys: the DC generator creates by them and pod/rack generators
# look them up by them, so each is spelled once, here.
def _underlay_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-underlay-key"


def _overlay_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-overlay-key"


def _super_spine_as_description(fabric_name: str) -> str:
    return f"{fabric_name} super-spine underlay ASN"


def _lisp_site_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-lisp-site-key"


def _overlay_as_description(fabric_name: str) -> str:
    return f"{fabric_name} overlay ASN for iBGP EVPN"


def _ospf_area_name(fabric_name: str) -> str:
    return f"{fabric_name}-ospf-area-0"


class RoutingMixin:
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
    data: Any
    # PoolMixin.resource_lock — a Callable attribute, not a stub, so it never shadows the real one.
    resource_lock: Callable[[str], AbstractAsyncContextManager[None]]
    # PoolMixin.attach_refs — attaches the fabric AS (evpn_rt_as) or LISP site key (lisp_site_key) to the deployment.
    attach_refs: Callable[..., Awaitable[None]]

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
        # The fabric has no overlay BGP, no overlay AS and no EVPN address family
        # — only the OSPF underlay below: an office fabric (options no_overlay),
        # or a data center whose overlay is LISP.
        lisp = bool(options.get("no_overlay")) or overlay_control_plane_of(design) == LISP_OVERLAY

        if not routing_strategy:
            self.logger.warning("No design or routing strategy provided")
            return
        if routing_strategy not in {s.value for s in RoutingStrategy}:
            self.logger.warning(f"Routing strategy '{routing_strategy}' not supported")
            return

        if lisp and options.get("skip_underlay"):
            self.logger.info("LISP overlay and no underlay requested: no routing to create")
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
            not lisp
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
        needs_overlay_password = not lisp and not options.get("overlay_password_id")

        overlay_as_id = options.get("overlay_as_id")
        ospf_area_id = options.get("ospf_area_id")
        underlay_password_id = options.get("underlay_password_id")
        overlay_password_id = options.get("overlay_password_id")

        top_device_set = set(top_devices)
        all_bgp: list[Any] = []
        loopback_interfaces: list[Any] = []

        for attempt in range(_SHARED_OBJECT_MAX_RETRIES):
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
            missing_overlay_bgp = set() if lisp else top_device_set - overlay_device_names

            still_missing = (
                (needs_overlay_as and not overlay_as_id)
                or (needs_ospf_area and not ospf_area_id)
                or bool(missing_overlay_bgp)
            )
            if not still_missing:
                break
            if attempt < _SHARED_OBJECT_MAX_RETRIES - 1:
                waiting_on = []
                if needs_overlay_as and not overlay_as_id:
                    waiting_on.append("shared overlay AS")
                if needs_ospf_area and not ospf_area_id:
                    waiting_on.append("shared OSPF area")
                if missing_overlay_bgp:
                    waiting_on.append(f"overlay BGP for {sorted(missing_overlay_bgp)}")
                delay = retry_delay(_SHARED_OBJECT_RETRY_DELAY, attempt)
                self.logger.info(
                    f"Waiting on: {', '.join(waiting_on)} — retrying in {delay:.2f}s "
                    f"(attempt {attempt + 1}/{_SHARED_OBJECT_MAX_RETRIES})"
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

        evpn_af_id = "" if lisp else await self.ensure_address_family(*EVPN_AF)

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
                deployment_name=self.fabric_name,
                evpn_af_id=evpn_af_id,
                underlay_password_id=options.get("underlay_password_id") or "",
                overlay_password_id=options.get("overlay_password_id") or "",
                mlag_pairs=mlag_pairs,
                overlay_control_plane=LISP_OVERLAY if lisp else overlay_control_plane_of(design),
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

    async def _ensure_routing_password(self, *, name: str, description: str) -> str | None:
        """Create or reuse a shared RoutingPassword node by deterministic name.

        Serialized per name: add_dc and dc_pod_cascade can reach this together,
        both find nothing and both create the key, and the loser either fails
        the uniqueness constraint or leaves a second, untracked key behind.
        """
        async with self.resource_lock(f"routing-password-{name}"):
            return await self._ensure_routing_password_locked(name=name, description=description)

    async def _ensure_routing_password_locked(self, *, name: str, description: str) -> str | None:
        try:
            existing = await self.client.get(kind=RoutingPassword, name__value=name, raise_when_missing=False)
            if existing:
                self.client.group_context.related_node_ids.append(existing.id)
                return existing.id
        except Exception as exc:
            # Fatal (logger.error raises): add_dc owns this key, and a run that
            # could not re-track it would delete it in its own cleanup.
            self.logger.error(f"Error querying RoutingPassword {name}: {exc}")
            return None

        try:
            obj = await self.client.create(
                kind=RoutingPassword,
                data={
                    "name": name,
                    "description": description,
                    "password": secrets.token_urlsafe(32),
                },
            )
            await obj.save(allow_upsert=True)
            self.client.group_context.related_node_ids.append(obj.id)
            self.logger.info(f"Created shared RoutingPassword: {name}")
            return obj.id
        except Exception as exc:
            self.logger.error(f"Could not create RoutingPassword {name}: {exc}")
            return None

    async def _create_shared_routing_objects(
        self,
        overlay_asn: int,
        deployment_id: str,
        asn_pool_id: str | None = None,
    ) -> str | None:
        """Create shared DC-level routing state used by pod and rack generators.

        add_dc/dc_pod_cascade own all of it and track every object on every
        run; pod/rack runs only reference it (see create_routing). Every
        failure here is fatal (logger.error raises): a run that silently
        skipped one would not re-track it, and its own cleanup would then
        delete an object every pod still uses.

        Returns the shared super-spine underlay AS id (None when the strategy
        has no eBGP underlay, or it could not be found or allocated).

        ``asn_pool_id``: the DC's own fabric_asn_pool, used ONLY to allocate
        the single shared super-spine underlay AS (see below) — every other
        shared object here (overlay AS, OSPF area, passwords) needs no pool.

        ``deployment_id``: the fabric this state belongs to. Used to attach the
        fabric AS as ``evpn_rt_as`` so transforms can reach it — without that
        relationship the AS is only findable by description, which the config
        transforms cannot query.
        """

        await self._ensure_routing_password(
            name=_underlay_key_name(self.fabric_name),
            description=f"Shared eBGP/OSPF underlay auth key for {self.fabric_name}",
        )
        lisp = overlay_control_plane_of(self.data) == LISP_OVERLAY
        if lisp:
            await self.ensure_lisp_site_key(deployment_id=deployment_id)
        else:
            await self._ensure_routing_password(
                name=_overlay_key_name(self.fabric_name),
                description=f"Shared BGP overlay/EVPN auth key for {self.fabric_name}",
            )

        strategy = self.data.get("routing_strategy", "ebgp-ebgp")

        # Super-spine underlay is eBGP whenever underlay_type == "ebgp" (both
        # ebgp-ebgp and ebgp-ibgp — only ospf-ibgp skips underlay for
        # super-spine entirely, see dc.py's skip_underlay branch). All
        # super-spines fabric-wide share ONE underlay ASN (.dev/bgp.txt) —
        # same find-or-create-by-deterministic-description idiom as the
        # overlay AS below.
        super_spine_as_id: str | None = None
        if strategy.split("-")[0] == "ebgp":
            try:
                super_spine_as_id = await self.ensure_shared_as(
                    description=_super_spine_as_description(self.fabric_name), asn_pool_id=asn_pool_id
                )
            except Exception as exc:
                self.logger.error(f"Failed to create shared super-spine AS: {exc}")

        # The fabric AS is created for EVERY strategy, not just the iBGP ones.
        # Under ebgp-ibgp/ospf-ibgp it is the actual overlay BGP local-as. Under
        # ebgp-ebgp no device uses it as a local-as — but the fabric still needs
        # exactly one administrative ASN to stamp into EVPN route-targets, or
        # each VTEP derives the RT from its own per-device ASN and two leaves
        # never import each other's routes for the same segment. Gating this on
        # strategy is what left ebgp-ebgp (9 of 10 demo topologies) with no
        # fabric-constant RT source at all.
        #
        # The description string is the idempotency key and deliberately
        # unchanged, so existing fabrics keep their AS object rather than
        # silently getting a second one.
        #
        # Not under LISP: no RT is derived and no overlay BGP exists, so there is
        # no fabric AS to stamp anywhere.
        if not lisp:
            overlay_desc = _overlay_as_description(self.fabric_name)
            try:
                as_id = await self._ensure_overlay_as(description=overlay_desc, overlay_asn=overlay_asn)
                await self.attach_refs(kind="TopologySegmentHosting", node_id=deployment_id, refs={"evpn_rt_as": as_id})
            except Exception as exc:
                self.logger.error(f"Failed to create shared overlay AS: {exc}")

        if strategy == RoutingStrategy.OSPF_IBGP.value:
            await self.ensure_ospf_area()

        return super_spine_as_id

    async def ensure_lisp_site_key(self, *, deployment_id: str) -> str | None:
        """Find or create the deployment's shared LISP site key and attach it as ``lisp_site_key``.

        The one site key a LISP fabric's map-servers authenticate the edge ETRs
        with; reached by transforms as the deployment's lisp_site_key, the way
        the overlay AS is as evpn_rt_as. Shared by the DC (a fabric with no BGP
        overlay has no overlay key either) and the office fabric generator,
        which attach it to their own TopologySegmentHosting node. Returns the
        key's id (None when it could not be found or created).
        """
        site_key_id = await self._ensure_routing_password(
            name=_lisp_site_key_name(self.fabric_name),
            description=f"Shared LISP map-server site key for {self.fabric_name}",
        )
        await self.attach_refs(
            kind="TopologySegmentHosting", node_id=deployment_id, refs={"lisp_site_key": site_key_id}
        )
        return site_key_id

    async def ensure_ospf_area(self) -> str | None:
        """Find or create the fabric's OSPF backbone area (area 0) and track it in this run's group.

        Returns the area's id, None when it could not be found or created (the
        failure is logged as an error). Shared by the DC (ospf-ibgp) and the
        office fabric generator; both own the area and track it on every run.
        """
        area_name = _ospf_area_name(self.fabric_name)
        try:
            area_obj = await self.client.get(kind=RoutingOSPFArea, name__value=area_name, raise_when_missing=False)
            if area_obj:
                self.client.group_context.related_node_ids.append(area_obj.id)
            else:
                area_obj = await self.client.create(
                    kind=RoutingOSPFArea,
                    data={
                        "name": area_name,
                        "area": 0,
                        "area_type": "standard",
                        "description": f"OSPF backbone area for {self.fabric_name}",
                    },
                )
                await area_obj.save(allow_upsert=True)
                self.client.group_context.related_node_ids.append(area_obj.id)
                self.logger.info(f"Created shared OSPF area: {area_name}")
            return area_obj.id
        except Exception as exc:
            self.logger.error(f"Failed to create shared OSPF area: {exc}")
            return None

    async def _ensure_overlay_as(self, *, description: str, overlay_asn: int) -> str:
        """Find the fabric's overlay AS by its deterministic description (pinning
        its ASN to ``overlay_asn``) or create it, and track it in this run's group.

        Same race as ensure_shared_as, so the same per-description lock key: the
        query and the create both run inside it.
        """
        async with self.resource_lock(f"shared-as-{description}"):
            existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=description)
            if existing:
                as_obj = existing[0]
                as_obj.asn.value = overlay_asn
                await as_obj.save(allow_upsert=True)
                self.logger.info(f"Updated shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
            else:
                as_obj = await self.client.create(
                    kind=RoutingAutonomousSystem,
                    data={"asn": overlay_asn, "description": description},
                )
                await as_obj.save(allow_upsert=True)
                self.logger.info(f"Created shared overlay AS: AS{as_obj.asn.value} ({as_obj.id})")
            self.client.group_context.related_node_ids.append(as_obj.id)
            return as_obj.id

    async def ensure_shared_as(self, *, description: str, asn_pool_id: str | None) -> str | None:
        """Find the AS carrying this deterministic description, or draw a new
        one from asn_pool_id, and track it in this run's group.

        The description is the idempotency key: from_pool on a new node would
        allocate a fresh ASN on every run. Returns None when no such AS exists
        and there is no pool to draw one from.

        Serialized per description: add_dc and dc_pod_cascade (and sibling pod
        runs) can reach this at the same moment, both find nothing, and both
        draw an AS. The untracked twin then breaks the next run's
        delete_unused_nodes, since a ManagedBGP still uses it as local_as.
        """
        async with self.resource_lock(f"shared-as-{description}"):
            return await self._ensure_shared_as_locked(description=description, asn_pool_id=asn_pool_id)

    async def _ensure_shared_as_locked(self, *, description: str, asn_pool_id: str | None) -> str | None:
        existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=description)
        if existing:
            as_obj = existing[0]
        elif asn_pool_id:
            as_obj = await self.client.create(
                kind=RoutingAutonomousSystem,
                data={"asn": {"from_pool": {"id": asn_pool_id}}, "description": description},
            )
            await as_obj.save(allow_upsert=True)
            self.logger.info(f"Created shared AS{as_obj.asn.value} '{description}' ({as_obj.id})")
        else:
            return None
        self.client.group_context.related_node_ids.append(as_obj.id)
        return as_obj.id

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

        One global node every fabric, circuit and metro run references, so no
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
    ) -> None:
        """Upsert one directly connected (ttl 1, BFD, extended communities) ManagedBGPPeering.

        The shared core of the DCI session (add_circuit) and the metro's edge
        FABRIC session (add_colocation_metro). Peerings share the
        ManagedGeneric name space with circuits: callers prefix the name
        (DCI-<circuit>, FABRIC-<a>-<b>). Processes are passed by id, never
        looked up by name here (search-index lag on same-run objects).

        ``password_id`` keys the session; a session between non-DC ends (an
        SD-WAN edge to its gateway) has no fabric key, so no password is
        written at all.
        """
        peering = await self.client.create(
            kind=ManagedBGPPeering,
            data={
                "name": name,
                "description": description,
                "status": status,
                "session_type": session_type,
                "peering_role": peering_role,
                "ttl": 1,
                "bfd_enabled": True,
                "send_community": True,
                "send_extended_community": True,
                "address_families": [{"id": af_id} for af_id in address_family_ids],
                "bgp_processes": [{"id": process_id} for process_id in process_ids],
                **({"password": {"id": password_id}} if password_id else {}),
                "interface_capabilities": [{"id": interface_id} for interface_id in interface_ids],
            },
        )
        await save_with_node_not_found_retry(peering, self.logger)

    async def _resolve_shared_passwords(self) -> tuple[str | None, str | None]:
        """Find the fabric's shared underlay/overlay RoutingPassword IDs, by deterministic name.

        These are created once by the DC generator (``_create_shared_routing_objects``);
        pod/rack layers only look them up here — one-shot lookup, no retry: the
        caller (``create_routing``'s readiness loop) retries this alongside every
        other shared-object lookup in one combined polling loop. A missing
        password is non-fatal even after that loop exhausts: it just means auth
        wasn't configured (e.g. DC generator hasn't run yet, or ran before this
        feature existed) — routing creation proceeds without it.
        """
        password_ids: list[str | None] = []
        for name in (_underlay_key_name(self.fabric_name), _overlay_key_name(self.fabric_name)):
            password_id: str | None = None
            try:
                existing = await self.client.get(kind=RoutingPassword, name__value=name, raise_when_missing=False)
                if existing:
                    password_id = existing.id
            except Exception as e:
                self.logger.debug(f"Error querying RoutingPassword {name}: {e}")
            password_ids.append(password_id)
        return password_ids[0], password_ids[1]

    async def _resolve_shared_objects(self, routing_strategy: str) -> tuple[str | None, str | None]:
        """Find shared DC-level overlay AS and OSPF area. Returns (overlay_as_id, ospf_area_id).

        One-shot lookup, no retry: the caller (``create_routing``'s readiness
        loop) retries this alongside every other shared-object lookup in one
        combined polling loop — see that loop's docstring for the race this
        closes.

        The resolved IDs are only referenced, never tracked, by this caller:
        add_dc owns them (see _create_shared_routing_objects).
        """
        overlay_as_id: str | None = None
        ospf_area_id: str | None = None

        if routing_strategy in (RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value):
            overlay_desc = _overlay_as_description(self.fabric_name)
            try:
                existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=overlay_desc)
                if existing:
                    overlay_as_id = existing[0].id
                    self.logger.info(f"Found existing overlay AS: AS{existing[0].asn.value} ({overlay_as_id})")
            except Exception as e:
                self.logger.warning(f"Error querying overlay AS for {self.fabric_name}: {e}")

        if routing_strategy == RoutingStrategy.OSPF_IBGP.value:
            area_name = _ospf_area_name(self.fabric_name)
            try:
                area = await self.client.get(kind=RoutingOSPFArea, name__value=area_name)
                if area:
                    ospf_area_id = area.id
                    self.logger.info(f"Found existing OSPF area: {area_name}")
            except Exception as e:
                self.logger.warning(f"Error querying OSPF area {area_name}: {e}")

        return overlay_as_id, ospf_area_id
