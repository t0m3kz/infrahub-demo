"""Routing mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

from .helpers import RoutingPlanInput, RoutingPlanner, RoutingStrategy
from .protocols import (
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedBGP,
    ManagedBGPPeering,
    ManagedOSPF,
    RoutingAutonomousSystem,
    RoutingOSPFArea,
    RoutingOSPFInterface,
)
from .types import RoutingOptions


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
    ) -> None:
        """Create routing configuration between device layers.

        Follows create_cabling pattern: collect data, plan, create objects sequentially.

        Underlay always uses P2P interfaces, overlay always uses loopback interfaces.
        Overlay is either iBGP (shared ASN) or eBGP (per-device ASN from underlay).

        Existing shared objects (iBGP ASN, OSPF area) are queried here and passed
        to the routing helper so it stays pure (no DB access).
        See ``RoutingOptions`` for available option keys.
        """
        if options is None:
            options = RoutingOptions()
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

        # Protect shared DC-level objects from generator group cleanup
        for shared_id in [options.get("overlay_as_id"), options.get("ospf_area_id")]:
            if shared_id and shared_id not in self.client.group_context.related_node_ids:
                self.client.group_context.related_node_ids.append(shared_id)

        # ================================================================
        # PHASE 1: DATA COLLECTION (4 parallel queries)
        # ================================================================

        all_bgp, interfaces, loopback_interfaces = await asyncio.gather(
            self.client.filters(
                kind=ManagedBGP,
                device_capabilities__name__values=all_device_names,
                include=["local_as", "device_capabilities"],
                prefetch_relationships=True,
            ),
            self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=all_device_names,
                tags__name__value="fabric-p2p",
                include=["device", "cable"],
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
        underlay = [b for b in all_bgp if "underlay" in b.name.value]

        if routing_strategy == RoutingStrategy.OSPF_IBGP:
            overlay = await self.client.filters(
                kind=ManagedOSPF,
                device_capabilities__name__values=all_device_names,
            )
        else:
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

        plan = planner.build_routing_plan(
            RoutingPlanInput(
                bottom_devices=bottom_devices,
                top_devices=top_devices,
                underlay=underlay,
                overlay=overlay,
                interfaces=interfaces,
                loopback_interfaces=loopback_interfaces,
                options={**options},
                routing_strategy=routing_strategy,
                deployment_name=self.fabric_name,
            )
        )

        # ================================================================
        # PHASE 3: CREATE SDK OBJECTS, SAVE IN DEPENDENCY ORDER
        # ================================================================

        def _clean(d: dict, strip_local_as: bool = False) -> dict:
            """Remove internal keys (prefixed with _) before passing to SDK."""
            result = {k: v for k, v in d.items() if not k.startswith("_")}
            if strip_local_as:
                result.pop("local_as", None)
            return result

        # Step 1: Create + save AS objects, build device -> AS ID mapping
        device_to_as_id = await self._save_autonomous_systems(plan.autonomous_systems)

        # Step 2: Resolve local_as placeholders in BGP processes
        for bgp in plan.bgp_processes:
            local_as = bgp.get("local_as", {})
            if isinstance(local_as, dict) and "_for_device" in local_as:
                as_id = device_to_as_id.get(local_as["_for_device"])
                if as_id:
                    bgp["local_as"] = {"id": as_id}
                else:
                    self.logger.error(f"Cannot resolve AS for {bgp.get('name')}")

        # Step 3: Create BGP + OSPF SDK objects, save sequentially.
        # Every process is upserted by its deterministic HFID (name). Re-saving an
        # existing ManagedBGP is safe: local_as is cardinality-one and upserts cleanly
        # (replaces, never duplicates — verified on Infrahub 1.9.6), so the upsert
        # also (re)registers it in the group context with no new/existing split.
        plan.bgp_processes = [await self.client.create(kind=ManagedBGP, data=_clean(d)) for d in plan.bgp_processes]
        plan.ospf_processes = [await self.client.create(kind=ManagedOSPF, data=_clean(d)) for d in plan.ospf_processes]
        for obj in plan.bgp_processes + plan.ospf_processes:
            await obj.save(allow_upsert=True)
            self.logger.info(f"  Saved: {getattr(getattr(obj, 'name', None), 'value', obj.id)}")

        # Step 4: Create peering + OSPF interface SDK objects, save sequentially
        plan.bgp_peerings = [
            await self.client.create(kind=ManagedBGPPeering, data=_clean(d)) for d in plan.bgp_peerings
        ]
        plan.ospf_interfaces = [
            await self.client.create(kind=RoutingOSPFInterface, data=_clean(d)) for d in plan.ospf_interfaces
        ]
        for obj in plan.bgp_peerings + plan.ospf_interfaces:
            await obj.save(allow_upsert=True)
            self.logger.info(f"  Saved: {getattr(getattr(obj, 'name', None), 'value', obj.id)}")

        total = (
            len(plan.autonomous_systems)
            + len(plan.bgp_processes)
            + len(plan.ospf_processes)
            + len(plan.ospf_interfaces)
            + len(plan.bgp_peerings)
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
