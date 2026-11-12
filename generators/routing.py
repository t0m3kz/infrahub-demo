"""Routing mixin for CommonGenerator."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging
    from .pools import PoolMixin

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
# Defaults for a missing address family; same values as the bootstrap data.
_ADDRESS_FAMILY_DATA: dict[tuple[str, str], dict[str, Any]] = {
    IPV4_UNICAST_AF: {"description": "IPv4 unicast"},
    IPV6_UNICAST_AF: {"description": "IPv6 unicast"},
    EVPN_AF: {"description": "L2VPN EVPN overlay", "advertise_all_vni": True, "advertise_default_gw": True},
}


def _dci_key_name(circuit_name: str) -> str:
    """The per-circuit key of a DCI, not a fabric-wide one."""
    return f"{circuit_name}-dci-key"


class RoutingMixin(SharedRoutingObjectsMixin):
    """Routing methods for CommonGenerator (needs its client, logger, deployment_id, fabric_name, resource_lock)."""

    # Type-checker declarations, provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    deployment_id: str
    fabric_name: str
    # Type checking only: aliases of the host's other bases, so nothing shadows them in the MRO.
    if TYPE_CHECKING:
        resource_lock = PoolMixin.resource_lock

    async def create_routing(
        self,
        bottom_devices: list[str],
        top_devices: list[str],
        options: RoutingOptions | None = None,
        p2p_interfaces: list[tuple[Any, Any]] | None = None,
        bottom_role: str = "",
        top_role: str = "",
    ) -> None:
        """Create routing for a layer pair from the ``(src, dst)`` cabling pairs (``[]`` for process-only calls).
        ``bottom_role``/``top_role`` replace querying ``DcimDevice.role``, which intermittently resolved to None from
        another worker.
        """
        if options is None:
            options = RoutingOptions()
        if p2p_interfaces is None:
            p2p_interfaces = []
        design = options.get("design") or {}
        routing_strategy = design.get("routing_strategy")
        # An office fabric (no_overlay) has no overlay BGP, AS or EVPN family, only the OSPF underlay.
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

        # Resolve shared state (overlay AS, OSPF area, auth keys, sibling pod BGP) in one retry loop with one backoff.
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
        # Auth keys are created once by the DC generator; a missing key is non-fatal.
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

        # Shared DC-level objects are referenced, never tracked: add_dc/dc_pod_cascade own them.

        underlay_type = routing_strategy.split("-")[0]

        if underlay_type == "ospf":
            underlay = await self.client.filters(
                kind=ManagedOSPF,
                capabilities__name__values=all_device_names,
            )
        else:
            underlay = [b for b in all_bgp if b.process_role.value == "underlay"]

        # Overlay is always BGP; only the underlay can be OSPF.
        overlay = [b for b in all_bgp if b.process_role.value == "overlay"]

        # Device name -> MLAG domain, so MLAG peers share one underlay ASN (.dev/bgp.txt).
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

        # Phase 2: build the routing plan (pure)

        # strict: refuse to skip a device (no router-id/loopback) instead of leaving it with no BGP/OSPF process.
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

        # Phase 3: create SDK objects, save in dependency order

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

        # Step 3: upsert BGP + OSPF processes by HFID (local_as upserts cleanly, no new/existing split).
        plan.bgp_processes = [await self.client.create(kind=ManagedBGP, data=d) for d in plan.bgp_processes]
        plan.ospf_processes = [await self.client.create(kind=ManagedOSPF, data=d) for d in plan.ospf_processes]
        for obj in plan.bgp_processes + plan.ospf_processes:
            await obj.save(allow_upsert=True)
            self.logger.info(f"  Saved: {getattr(getattr(obj, 'name', None), 'value', obj.id)}")

        # Step 4: resolve HFID refs to processes saved in this run into ids (search-index lag gives NODE_NOT_FOUND).
        process_id_by_name = {obj.name.value: obj.id for obj in plan.bgp_processes + plan.ospf_processes}

        def _resolve_hfid(ref: dict) -> dict:
            fresh_id = process_id_by_name.get(ref.get("hfid"))
            return {"id": fresh_id} if fresh_id else ref

        for peering in plan.bgp_peerings:
            peering["bgp_processes"] = [_resolve_hfid(ref) for ref in peering["bgp_processes"]]
        for ospf_peering in plan.ospf_peerings:
            ospf_peering["ospf_process"] = [_resolve_hfid(ref) for ref in ospf_peering["ospf_process"]]

        # Step 5: save OSPF interfaces before peerings (same id resolution); peerings retry on save because concurrent
        # runs may create the process they reference.
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

    # Batch save helpers

    async def _save_autonomous_systems(self, as_dicts: list[dict]) -> dict[str, str]:
        """Save AS objects and return the device_name -> AS ID mapping (existing ones are only registered)."""
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
        """Find or create a RoutingBGPAddressFamily under a lock; untracked (global, shared). Bootstrap data normally
        provides them.
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
    ) -> None:
        """Upsert one ManagedBGPPeering, by default directly connected (ttl 1, BFD, both community flags). Shared by
        session circuits, the metro FABRIC session and the office fabric; processes are passed by id; no ``password_id``
        means no password.
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
                "address_families": [{"id": af_id} for af_id in address_family_ids],
                "bgp_processes": [{"id": process_id} for process_id in process_ids],
                **({"password": {"id": password_id}} if password_id else {}),
                "capabilities": [{"id": interface_id} for interface_id in interface_ids],
            },
        )
        await save_with_node_not_found_retry(peering, self.logger)
