"""Routing mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING, Any

from infrahub_sdk.exceptions import GraphQLError

if TYPE_CHECKING:
    import logging

from .helpers import PendingASRef, RoutingPlanInput, RoutingPlanner, RoutingStrategy
from .helpers.routing import _safe_device_name
from .protocols import (
    DcimVirtualInterface,
    ManagedBGP,
    ManagedBGPPeering,
    ManagedOSPF,
    ManagedOSPFPeering,
    RoutingAutonomousSystem,
    RoutingOSPFArea,
    RoutingOSPFInterface,
    RoutingPassword,
)
from .types import RoutingOptions

_PEERING_SAVE_MAX_RETRIES = 5
_PEERING_SAVE_RETRY_DELAY = 2.0
_OVERLAY_BGP_MAX_RETRIES = 5
_OVERLAY_BGP_RETRY_DELAY = 3.0


def _retry_delay(base: float, attempt: int, cap: float = 20.0, jitter: float = 0.25) -> float:
    """Jittered exponential backoff used by transient race-retry loops."""
    return min(base * (2**attempt), cap) + random.uniform(0, jitter)


async def _save_peering_with_retry(obj: Any, logger: logging.Logger) -> None:
    """Save a peering/interface object, retrying on a NODE_NOT_FOUND write race.

    Peerings reference sibling processes/interfaces by direct id where possible
    (see ``_resolve_hfid`` below) specifically to avoid the search-index lag a
    plain HFID lookup would hit. But when several devices' ``create_routing()``
    calls run concurrently and write onto the SAME shared node (e.g. every pod's
    spines peering to the same 2 super-spine ManagedBGP processes — pod.py's
    pre-seed/post-cable calls), that write contention can make a just-created
    process transiently NODE_NOT_FOUND to a peering save issued a moment later
    in a different concurrent call. Retrying resolves it without needing to
    serialize those calls. Mirrors the interface-readiness retry already in
    ``CommonGenerator.create_cabling``.
    """
    name = getattr(getattr(obj, "name", None), "value", obj.id)
    for attempt in range(_PEERING_SAVE_MAX_RETRIES):
        try:
            await obj.save(allow_upsert=True)
            logger.info(f"  Saved: {name}")
            return
        except GraphQLError as exc:
            if not any(e.get("extensions", {}).get("code") == "NODE_NOT_FOUND" for e in exc.errors):
                raise
            if attempt == _PEERING_SAVE_MAX_RETRIES - 1:
                raise
            delay = _retry_delay(_PEERING_SAVE_RETRY_DELAY, attempt)
            logger.info(
                f"  NODE_NOT_FOUND saving {name} (referenced node not yet visible — "
                f"likely concurrent write contention on a shared process) — "
                f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_PEERING_SAVE_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)


class RoutingMixin:
    """Mixin providing routing configuration methods for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``,
    ``deployment_id``, and ``fabric_name`` attributes (all present on
    ``CommonGenerator`` via ``InfrahubGenerator``).
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    deployment_id: str
    fabric_name: str
    data: Any

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
        design = options.get("design")

        if not design or not hasattr(design, "routing_strategy"):
            self.logger.warning("No design or routing strategy provided")
            return

        routing_strategy = design.routing_strategy
        if routing_strategy not in {s.value for s in RoutingStrategy}:
            self.logger.warning(f"Routing strategy '{routing_strategy}' not supported")
            return

        all_device_names = bottom_devices + top_devices
        self.logger.info(
            f"Creating routing: {len(bottom_devices)} bottom ↔ {len(top_devices)} top device(s) "
            f"[strategy={routing_strategy}]"
        )

        # ================================================================
        # RESOLVE SHARED OBJECTS (created by dc.py, reused by pod/rack)
        # ================================================================

        needs_overlay_as = not options.get("overlay_as_id") and routing_strategy in (
            RoutingStrategy.EBGP_IBGP,
            RoutingStrategy.OSPF_IBGP,
        )
        needs_ospf_area = (
            not options.get("ospf_area_id")
            and not options.get("skip_underlay")
            and routing_strategy == RoutingStrategy.OSPF_IBGP
        )

        if needs_overlay_as or needs_ospf_area:
            overlay_as_id, ospf_area_id = await self._resolve_shared_objects(routing_strategy)

            if needs_overlay_as:
                if not overlay_as_id:
                    self.logger.error(
                        f"Shared overlay AS not found for {self.fabric_name}. "
                        "The DC generator must run first to create it."
                    )
                    return
                options["overlay_as_id"] = overlay_as_id

            if needs_ospf_area:
                if not ospf_area_id:
                    self.logger.error(
                        f"Shared OSPF area not found for {self.fabric_name}. "
                        "The DC generator must run first to create it."
                    )
                    return
                options["ospf_area_id"] = ospf_area_id

        # Shared underlay/overlay BGP/OSPF auth keys — created once by the DC generator.
        # Unlike overlay AS / OSPF area, a missing key is non-fatal: auth is a hardening
        # add-on, not required for connectivity, so routing creation proceeds without it.
        needs_underlay_password = not options.get("underlay_password_id") and not options.get("skip_underlay")
        needs_overlay_password = not options.get("overlay_password_id")
        if needs_underlay_password or needs_overlay_password:
            underlay_password_id, overlay_password_id = await self._resolve_shared_passwords()
            if needs_underlay_password and underlay_password_id:
                options["underlay_password_id"] = underlay_password_id
            if needs_overlay_password and overlay_password_id:
                options["overlay_password_id"] = overlay_password_id

        # Protect shared DC-level objects from generator group cleanup
        for shared_id in [
            options.get("overlay_as_id"),
            options.get("ospf_area_id"),
            options.get("underlay_password_id"),
            options.get("overlay_password_id"),
        ]:
            if shared_id and shared_id not in self.client.group_context.related_node_ids:
                self.client.group_context.related_node_ids.append(shared_id)

        # ================================================================
        # PHASE 1: DATA COLLECTION (4 parallel queries)
        # ================================================================

        interfaces = [iface for pair in p2p_interfaces for iface in pair]

        # Retry the overlay BGP query if any top_devices are still missing their
        # overlay process. Callers like pod.py's decentralized mesh cabling query
        # a sibling pod's spines directly (no dc.py-level wait) — the sibling's own
        # spines can exist while its overlay-BGP pre-seed (a separate create_routing
        # call inside that sibling's own generator run) hasn't landed yet. Retrying
        # here closes that narrow gap without serializing pod generation.
        top_device_set = set(top_devices)
        for attempt in range(_OVERLAY_BGP_MAX_RETRIES):
            all_bgp, loopback_interfaces = await asyncio.gather(
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
            )
            if not top_device_set:
                break
            overlay_device_names = {
                name for b in all_bgp if b.name.value.endswith("-bgp-overlay") and (name := _safe_device_name(b))
            }
            missing = top_device_set - overlay_device_names
            if not missing:
                break
            if attempt < _OVERLAY_BGP_MAX_RETRIES - 1:
                delay = _retry_delay(_OVERLAY_BGP_RETRY_DELAY, attempt)
                self.logger.info(
                    f"Overlay BGP not yet visible for top device(s) {sorted(missing)} — "
                    f"retrying in {delay:.2f}s "
                    f"(attempt {attempt + 1}/{_OVERLAY_BGP_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)

        underlay_type = routing_strategy.split("-")[0]

        if underlay_type == "ospf":
            underlay = await self.client.filters(
                kind=ManagedOSPF,
                capabilities__name__values=all_device_names,
            )
        else:
            underlay = [b for b in all_bgp if "underlay" in b.name.value]

        # Overlay is always iBGP/eBGP (ManagedBGP) — only the underlay can be OSPF.
        overlay = [b for b in all_bgp if "overlay" in b.name.value]

        self.logger.info(
            f"Collected: {len(interfaces)} P2P interface(s), "
            f"{len(loopback_interfaces)} loopback(s), "
            f"{len(underlay)} existing underlay, {len(overlay)} existing overlay"
        )

        # ================================================================
        # PHASE 2: BUILD ROUTING PLAN (pure helper, no DB access)
        # ================================================================

        planner = RoutingPlanner(deployment_id=self.deployment_id, logger=self.logger)

        evpn_af_id = await self._ensure_evpn_af_node()

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
        # Peerings additionally retry on save (_save_peering_with_retry): unlike
        # processes, a peering can reference a process id created by a DIFFERENT,
        # concurrently-running create_routing() call against the same shared
        # device (e.g. every pod's spines peering to the same super-spines) — a
        # write-contention race the HFID resolution above doesn't cover.
        plan.bgp_peerings = [await self.client.create(kind=ManagedBGPPeering, data=d) for d in plan.bgp_peerings]
        plan.ospf_interfaces = [
            await self.client.create(kind=RoutingOSPFInterface, data=d) for d in plan.ospf_interfaces
        ]
        for obj in plan.bgp_peerings:
            await _save_peering_with_retry(obj, self.logger)
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
            await _save_peering_with_retry(obj, self.logger)

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
                if existing_id not in self.client.group_context.related_node_ids:
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

    async def _ensure_evpn_af_node(self) -> str:
        """Upsert the L2VPN/EVPN RoutingBGPAddressFamily node and return its ID.

        This node must exist before any BGP process or peering can reference it
        via the address_families relationship. The DC1 static demo creates it via
        YAML load; generator-driven topologies need it created here.
        """
        try:
            existing = await self.client.filters(
                kind="RoutingBGPAddressFamily",
                afi__value="l2vpn",
                safi__value="evpn",
            )
            if existing:
                node_id = existing[0].id
                if node_id not in self.client.group_context.related_node_ids:
                    self.client.group_context.related_node_ids.append(node_id)
                return node_id
        except Exception as exc:
            self.logger.debug(f"Could not query RoutingBGPAddressFamily: {exc}")

        try:
            obj = await self.client.create(
                kind="RoutingBGPAddressFamily",
                data={
                    "afi": "l2vpn",
                    "safi": "evpn",
                    "advertise_all_vni": True,
                    "advertise_default_gw": True,
                    "description": "L2VPN EVPN overlay",
                },
            )
            await obj.save(allow_upsert=True)
            self.logger.info(f"Upserted RoutingBGPAddressFamily l2vpn/evpn: {obj.id}")
            return obj.id
        except Exception as exc:
            self.logger.warning(f"Could not upsert RoutingBGPAddressFamily l2vpn/evpn: {exc}")
            return ""

    async def _resolve_shared_passwords(self) -> tuple[str | None, str | None]:
        """Find the fabric's shared underlay/overlay RoutingPassword IDs, by deterministic name.

        These are created once by the DC generator (``_create_shared_routing_objects``);
        pod/rack layers only look them up here — mirrors ``_resolve_shared_objects``
        for ``overlay_as_id``/``ospf_area_id``. A missing password is non-fatal: it
        just means auth wasn't configured (e.g. DC generator hasn't run yet, or ran
        before this feature existed) — routing creation proceeds without it.
        """
        underlay_id: str | None = None
        overlay_id: str | None = None

        underlay_name = f"{self.fabric_name}-underlay-key"
        try:
            existing = await self.client.get(kind=RoutingPassword, name__value=underlay_name, raise_when_missing=False)
            if existing:
                underlay_id = existing.id
        except Exception as e:
            self.logger.debug(f"Error querying RoutingPassword {underlay_name}: {e}")

        overlay_name = f"{self.fabric_name}-overlay-key"
        try:
            existing = await self.client.get(kind=RoutingPassword, name__value=overlay_name, raise_when_missing=False)
            if existing:
                overlay_id = existing.id
        except Exception as e:
            self.logger.debug(f"Error querying RoutingPassword {overlay_name}: {e}")

        return underlay_id, overlay_id

    async def _resolve_shared_objects(self, routing_strategy: str) -> tuple[str | None, str | None]:
        """Find shared DC-level overlay AS and OSPF area. Returns (overlay_as_id, ospf_area_id)."""
        overlay_as_id: str | None = None
        ospf_area_id: str | None = None

        if routing_strategy in (RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value):
            overlay_desc = f"{self.fabric_name} overlay ASN for iBGP EVPN"
            try:
                existing = await self.client.filters(kind=RoutingAutonomousSystem, description__value=overlay_desc)
                if existing:
                    overlay_as_id = existing[0].id
                    self.logger.info(f"Found existing overlay AS: AS{existing[0].asn.value} ({overlay_as_id})")
            except Exception as e:
                self.logger.warning(f"Error querying overlay AS for {self.fabric_name}: {e}")

        if routing_strategy == RoutingStrategy.OSPF_IBGP.value:
            area_name = f"{self.fabric_name}-ospf-area-0"
            try:
                area = await self.client.get(kind=RoutingOSPFArea, name__value=area_name)
                if area:
                    ospf_area_id = area.id
                    self.logger.info(f"Found existing OSPF area: {area_name}")
            except Exception as e:
                self.logger.warning(f"Error querying OSPF area {area_name}: {e}")

        return overlay_as_id, ospf_area_id
