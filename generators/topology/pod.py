"""Infrastructure generator for pod topology creation."""

import asyncio
from typing import Any, Literal, cast

from utils.data_cleaning import clean_data

from ..common import CablingOptions, CommonGenerator, DeviceOptions, RoutingOptions
from ..helpers.routing import RoutingStrategy
from ..models import PodModel
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface, TopologyPod

_SIBLING_SPINE_MAX_RETRIES = 10
_SIBLING_SPINE_RETRY_DELAY = 3.0


class PodTopologyGenerator(CommonGenerator):
    """Generate pod topology with resource pools and spine infrastructure.

    Creates resource pools (technical and management) and creates spine devices
    within a pod topology.

    This is a pure bootstrap: it never fans out to add_rack itself. That fan-out
    lives in PodRackCascadeGenerator, a subclass that reuses this generate() via
    super() — see generators/topology/pod_rack_cascade.py's docstring for why.

    Waits for an in-flight add_dc/dc_pod_cascade run on its parent DC before
    reading DC-level data (see CommonGenerator.wait_for_parent_generator_and_refetch).
    """

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure."""

        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod Deployment data found in GraphQL response")
                return

            # This bootstrap reads DC-level data (super-spine devices, ASN pool,
            # management pool) written by add_dc/dc_pod_cascade. If either is
            # still running for our parent DC (e.g. a bulk DC+pod+rack load, or a
            # structural DC change firing add_dc + dc_pod_cascade in parallel),
            # wait for it and re-parse rather than risk running on partial data.
            dc_id = deployment_list[0].get("parent", {}).get("id")
            if dc_id:
                for parent_generator in ("add_dc", "dc_pod_cascade"):
                    refreshed = await self.wait_for_parent_generator_and_refetch(parent_generator, dc_id)
                    if refreshed is not None:
                        data = refreshed
                        deployment_list = clean_data(data).get("TopologyPod", [])
                        if not deployment_list:
                            self.logger.error("No Pod Deployment data found in GraphQL response")
                            return

            self.data = PodModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for pod {self.data.name}")

        pod_id = self.data.id
        dc = self.data.parent
        dc_design = dc.design
        self.deployment_id = dc.id  # Store for cable linking
        self.pod_name = self.data.name.lower()
        self.fabric_name = dc.name.lower()

        design = self.data.design

        deployment_type = self.data.deployment_type

        spine_entries = self.data.spine_templates
        if not spine_entries:
            self.logger.error(f"Pod {self.data.name}: no spine fabric_templates entries — cannot build fabric")
            return
        spine_count = sum(entry.quantity for entry in spine_entries)
        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            dc.naming_convention,
        )

        if design and spine_count > design.max_spines_per_pod:
            self.logger.error(
                f"Pod {self.data.name} requests {spine_count} spines "
                f"but pod design '{design.name}' allows at most {design.max_spines_per_pod}"
            )
            return

        indexes: list[int] = [dc.index, self.data.index]

        # Calculate pool sizes from design maximums (not actual deployed racks).
        # Pools must be sized for full design capacity so adding racks later won't exhaust them.
        pool_sizes = {}
        max_leafs = 0
        max_tors = 0
        is_ipv6 = dc_design.is_ipv6 if dc_design else False
        is_dual_stack = dc_design.is_dual_stack if dc_design else False
        if design:
            from generators.helpers import calculate_pod_pools

            p2p_addressing = dc_design.p2p_addressing if dc_design else "/31"

            max_spines = spine_count
            max_super_spines = sum(entry.quantity for entry in dc.super_spine_templates)
            rows = design.rows

            if deployment_type == "middle_rack":
                max_leafs = rows * design.network_racks_per_row * design.max_leafs_per_network_rack
                max_tors = rows * design.network_racks_per_row * design.max_tors_per_network_rack
            elif deployment_type == "tor":
                max_leafs = 0
                max_tors = rows * design.compute_racks_per_row * design.max_tors_per_compute_rack
            else:  # mixed
                max_leafs = rows * design.network_racks_per_row * design.max_leafs_per_network_rack
                max_tors = rows * design.compute_racks_per_row * design.max_tors_per_compute_rack

            calculated_pools = calculate_pod_pools(
                max_super_spines_per_fabric=max_super_spines,
                max_spines_per_pod=max_spines,
                max_leafs=max_leafs,
                max_tors=max_tors,
                deployment_type=deployment_type,
                p2p_addressing=p2p_addressing,
                ipv6=is_ipv6,
                dual_stack=is_dual_stack,
                compute_racks=rows * design.compute_racks_per_row,
                network_racks=rows * design.network_racks_per_row,
            )

            pool_sizes["technical"] = calculated_pools["technical"]
            pool_sizes["loopback"] = calculated_pools["loopback"]

            self.logger.info(
                f"Calculated pool sizes for pod {self.data.name}: "
                f"technical=/{calculated_pools['technical']}, loopback=/{calculated_pools['loopback']} "
                f"(spines={max_spines}, leafs={max_leafs}, tors={max_tors}, "
                f"p2p={p2p_addressing}, ipv6={is_ipv6}, dual_stack={is_dual_stack}, deployment={deployment_type})"
            )

        # Allocate/upsert pools (idempotent via identifier + allow_upsert)
        # Must always run so objects are tracked by the generator framework
        pod_pools = await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=pool_sizes,
        )

        # Reference DC-level ASN pool (one pool per DC, shared by all devices)
        # DC generator creates the pool; pod just references it for routing and propagates to pod.asn_pool
        dc_asn_pool_id: str | None = None
        dc_asn_pool_name: str | None = None
        if dc.fabric_asn_pool and dc.fabric_asn_pool.name:
            dc_asn_pool_id = dc.fabric_asn_pool.id
            dc_asn_pool_name = dc.fabric_asn_pool.name

            # Propagate DC pool reference to pod so rack generator can find it via pod.asn_pool
            pod_obj = await self.client.get(kind="TopologyPod", id=pod_id)
            if pod_obj:
                pod_obj.asn_pool = {"id": dc.fabric_asn_pool.id}
                await pod_obj.save(allow_upsert=True)
                self.logger.info(f"Pod {self.data.name}: linked to DC ASN pool '{dc_asn_pool_name}'")

        # Pass management pool ID from DC parent (create_devices resolves ID to SDK object)
        management_pool_id = dc.management_pool.id if dc.management_pool else None

        spines: list[str] = []
        for entry in spine_entries:
            entry_spines = await self.create_devices(
                deployment_id=self.data.id,
                device_role="spine",
                amount=entry.quantity,
                template=entry.template.model_dump(),
                naming_convention=naming_conv,
                options=DeviceOptions(
                    indexes=indexes,
                    allocate_loopback=True,
                    loopback_pool=pod_pools.get("loopback"),
                    loopback_prefix_length=128 if is_ipv6 else 32,
                    management_pool=management_pool_id,
                ),
            )
            spines.extend(entry_spines)

        # Interface names come from the first entry's template only — same accepted
        # limitation as dc.py's super-spine device creation: assumes every spine
        # template shares consistent downlink-interface naming.
        spine_template = spine_entries[0].template

        parent = self.data.parent
        super_spine_devices = [device.name for device in (parent.devices or [])]
        parent_super_spine_entries = parent.super_spine_templates
        super_spine_interfaces = (
            [iface.name for iface in parent_super_spine_entries[0].template.interfaces]
            if parent_super_spine_entries
            else []
        )

        # Pre-seed spine eBGP processes before cabling exists so parallel rack generators
        # find them and don't try to create duplicates.
        # OSPF_IBGP is excluded: spine overlay BGP needs overlay_as_id (resolved inside
        # create_routing via _resolve_shared_objects) and is created in the post-cable call below.
        if (
            dc_design
            and dc_asn_pool_id
            and dc_design.routing_strategy
            in (
                RoutingStrategy.EBGP_EBGP.value,
                RoutingStrategy.EBGP_IBGP.value,
            )
        ):
            await self.create_routing(
                bottom_devices=spines,
                top_devices=super_spine_devices,
                options=RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id),
                p2p_interfaces=[],
                bottom_role="spine",
                top_role="super-spine",
            )

        spine_interfaces = [iface.name for iface in spine_template.interfaces if iface.role == "uplink"]
        if not spine_interfaces:
            self.logger.error(
                f"Pod {self.data.name}: No uplink interfaces found in spine template. "
                "Cannot create spine-to-super-spine cabling."
            )
            return

        # Skip cabling if no super-spines (single-pod DC scenario)
        skip_cabling = False
        if not super_spine_devices or not super_spine_interfaces:
            self.logger.info(
                f"Pod {self.data.name}: Skipping spine-to-super-spine cabling (single-pod DC or no super-spines)"
            )
            skip_cabling = True

            # No super-spine tier: pre-seed this pod's own spine overlay BGP now
            # (skip_underlay=True — underlay comes from rack.py's leaf<->spine cabling)
            # so it exists before any rack generator's leaf-to-spine routing call runs.
            # Those calls treat spines as top_devices and rely on an existing overlay
            # BGP process for them.
            if dc_design:
                await self.create_routing(
                    bottom_devices=spines,
                    top_devices=[],
                    options=RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id, skip_underlay=True),
                    p2p_interfaces=[],
                    bottom_role="spine",
                )

            await self._cable_to_existing_sibling_pods(
                spines=spines,
                spine_interfaces=[iface.name for iface in spine_template.interfaces if iface.role == "uplink"],
                dc=dc,
                dc_design=dc_design,
                dc_asn_pool_id=dc_asn_pool_id,
                pod_pools=pod_pools,
                is_ipv6=is_ipv6,
            )

        if not skip_cabling:
            dc_max_spines = dc_design.max_spines_per_pod if dc_design else spine_count
            cabling_offset = (self.data.index - 1) * dc_max_spines
            p2p_prefix_length = 127 if is_ipv6 else 31
            routing_opts = (
                RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id)
                if dc_design and dc_asn_pool_id
                else RoutingOptions()
            )
            p2p_pairs = await self.create_cabling(
                bottom_devices=spines,
                bottom_interfaces=spine_interfaces,
                top_devices=super_spine_devices,
                top_interfaces=super_spine_interfaces,
                strategy="pod",
                options=CablingOptions(
                    cabling_offset=cabling_offset,
                    pool=pod_pools.get("technical"),
                    p2p_prefix_length=p2p_prefix_length,
                ),
                bottom_sorting=self.data.spine_interface_sorting_method,
                top_sorting=parent.fabric_interface_sorting_method,
            )
            if routing_opts.get("design"):
                await self.create_routing(
                    bottom_devices=spines,
                    top_devices=super_spine_devices,
                    options=routing_opts,
                    p2p_interfaces=p2p_pairs,
                    bottom_role="spine",
                    top_role="super-spine",
                )
        # When there are no super-spines to cable to (skip_cabling), inter-pod
        # back-to-back spine cabling is handled above by _cable_to_existing_sibling_pods.
        # Fan-out to add_rack is handled by the sibling pod_rack_cascade generator,
        # not here — see PodTopologyGenerator's class docstring.

        # Cable+route this pod's own border-leaf devices (created by dc.py's
        # _create_border_leaf_devices with deployment_id=this pod, but NOT cabled
        # there — pod.py already owns spine context, so it does the uplink cabling
        # during its own bootstrap instead of dc.py re-deriving it).
        await self._cable_border_leafs_to_spines(
            spines=spines,
            spine_downlink_interfaces=[iface.name for iface in spine_template.interfaces if iface.role == "downlink"],
            dc_design=dc_design,
            dc_asn_pool_id=dc_asn_pool_id,
            pod_pools=pod_pools,
            is_ipv6=is_ipv6,
        )

        # A pod added AFTER its DC already declared border-leaf/firewall/load-balancer
        # fabric_templates needs a retroactive share of those devices — same as any
        # other structural DC-level reconciliation, this requires an explicit
        # dc_pod_cascade run (see tasks/demo.py's own manual dc_pod_cascade calls
        # after bulk loads). Deliberately NOT auto-triggered from here: every
        # add_pod run (including each pod during a bulk multi-DC load) would fire
        # its own concurrent dc_pod_cascade re-run against the same DC-level pools/
        # ASN pool, racing the DC's own already-in-flight bootstrap.

    async def _cable_border_leafs_to_spines(
        self,
        *,
        spines: list[str],
        spine_downlink_interfaces: list[str],
        dc_design: Any,
        dc_asn_pool_id: str | None,
        pod_pools: dict[str, Any],
        is_ipv6: bool,
    ) -> None:
        """Cable+route this pod's own border-leaf devices (already created by
        dc.py, deployment_id=this pod) to this pod's spines. Offset is
        deterministic (design's leaf/tor CAPACITY, not live device count) so it
        never collides with rack.py's own leaf/tor offsets into the same spine
        downlink ports — rack.py's offsets start at 0 and fill up to capacity;
        border-leaf's offset starts exactly where that capacity ends. No-ops if
        this pod has no border-leaf devices (the common case — most pods don't
        host any)."""
        border_leafs = await self.client.filters(
            kind=DcimPhysicalDevice, deployment__ids=[self.data.id], role__value="border-leaf"
        )
        if not border_leafs:
            return
        border_leaf_names = sorted(bl.name.value for bl in border_leafs)

        bl_uplinks = await self.client.filters(
            kind=DcimPhysicalInterface, device__name__values=border_leaf_names, role__value="uplink"
        )
        bl_uplink_names = sorted({iface.name.value for iface in bl_uplinks})
        if not bl_uplink_names or not spine_downlink_interfaces:
            self.logger.error(
                f"Pod {self.data.name}: cannot cable border-leaf uplinks — "
                f"bl_uplinks={len(bl_uplink_names)}, spine_downlinks={len(spine_downlink_interfaces)}."
            )
            return

        design = self.data.design
        offset = 0
        if design:
            rows = design.rows
            if self.data.deployment_type == "tor":
                offset = rows * design.compute_racks_per_row * design.max_tors_per_compute_rack
            else:
                offset = rows * design.network_racks_per_row * design.max_leafs_per_network_rack

        p2p_prefix_length = 127 if is_ipv6 else 31
        p2p_pairs = await self.create_cabling(
            bottom_devices=border_leaf_names,
            bottom_interfaces=bl_uplink_names,
            top_devices=spines,
            top_interfaces=spine_downlink_interfaces,
            strategy="rack",
            options=CablingOptions(
                cabling_offset=offset,
                pool=pod_pools.get("technical"),
                p2p_prefix_length=p2p_prefix_length,
            ),
        )
        if dc_design and dc_asn_pool_id:
            await self.create_routing(
                bottom_devices=border_leaf_names,
                top_devices=spines,
                options=RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id),
                p2p_interfaces=p2p_pairs,
                bottom_role="border-leaf",
                top_role="spine",
            )

    async def _cable_to_existing_sibling_pods(
        self,
        spines: list[str],
        spine_interfaces: list[str],
        dc: Any,
        dc_design: Any,
        dc_asn_pool_id: str | None,
        pod_pools: dict[str, Any],
        is_ipv6: bool,
    ) -> None:
        """Cable this pod's spines to every EXISTING lower-index sibling pod (back-to-back mesh).

        Decentralized: each pod cables itself to its lower-index siblings, rather
        than a DC-level step waiting for every pod to finish before cabling the
        whole mesh in one pass. Only the higher-index pod in any given pair ever
        writes that pair's cabling/routing — pod 1 never initiates anything, pod 2
        only cables to pod 1, pod 3 to pods 1 and 2, etc. — so there is exactly one
        writer per pair regardless of how many pods run concurrently; the only
        remaining concern is data availability (has the sibling's spine/overlay
        BGP landed yet), which create_cabling's own interface-readiness retry and
        create_routing's overlay-BGP-readiness retry already absorb.

        This also makes incremental single-pod-add correct for free: a pod added
        later simply finds its existing lower-index siblings already fully formed
        and cables to them immediately — no dc.py-level orchestration required.
        """
        if not spine_interfaces:
            self.logger.warning(f"Pod {self.data.name}: no spine uplink interfaces — skipping inter-pod mesh cabling")
            return

        siblings = await self.client.filters(kind=TopologyPod, parent__ids=[dc.id])
        lower_siblings = [s for s in siblings if s.index.value < self.data.index]
        if not lower_siblings:
            self.logger.info(f"Pod {self.data.name}: no lower-index sibling pods yet — nothing to mesh-cable")
            return

        dc_max_spines = dc_design.max_spines_per_pod if dc_design else 0
        p2p_prefix_length = 127 if is_ipv6 else 31
        routing_opts = RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id) if dc_design else RoutingOptions()

        for sibling in sorted(lower_siblings, key=lambda s: s.index.value):
            # Retry: during INITIAL bulk DC creation, every pod's TopologyPod object
            # already exists (loaded together before any generator runs), but a lower-
            # index sibling's own add_pod run may not have reached spine creation yet.
            # For an incremental single-pod-add, the sibling is already fully formed
            # and this resolves on the first attempt. Mirrors create_cabling's own
            # interface-readiness retry.
            sibling_spines: list[str] = []
            sibling_uplink_names: list[str] = []
            for attempt in range(_SIBLING_SPINE_MAX_RETRIES):
                sibling_spines_devices = await self.client.filters(
                    kind=DcimPhysicalDevice,
                    deployment__ids=[sibling.id],
                    role__value="spine",
                )
                sibling_spines = [s.name.value for s in sibling_spines_devices]
                if sibling_spines:
                    sibling_uplinks = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__name__values=sibling_spines,
                        role__value="uplink",
                    )
                    sibling_uplink_names = sorted({iface.name.value for iface in sibling_uplinks})
                    if sibling_uplink_names:
                        break
                if attempt < _SIBLING_SPINE_MAX_RETRIES - 1:
                    delay = self._retry_delay(_SIBLING_SPINE_RETRY_DELAY, attempt)
                    self.logger.info(
                        f"Pod {self.data.name}: sibling pod idx={sibling.index.value} spines/uplinks not ready yet — "
                        f"retrying in {delay:.2f}s (attempt {attempt + 1}/{_SIBLING_SPINE_MAX_RETRIES})"
                    )
                    await asyncio.sleep(delay)

            if not sibling_spines or not sibling_uplink_names:
                self.logger.warning(
                    f"Pod {self.data.name}: sibling pod idx={sibling.index.value} still has no spine "
                    f"devices/uplinks after {_SIBLING_SPINE_MAX_RETRIES} attempts — skipping this pair"
                )
                continue

            cabling_offset = (sibling.index.value - 1) * dc_max_spines
            self.logger.info(
                f"Pod {self.data.name} (idx={self.data.index}): cabling to sibling pod idx={sibling.index.value} "
                f"[offset={cabling_offset}]"
            )
            p2p_pairs = await self.create_cabling(
                bottom_devices=spines,
                bottom_interfaces=spine_interfaces,
                top_devices=sibling_spines,
                top_interfaces=sibling_uplink_names,
                strategy="pod",
                options=CablingOptions(
                    cabling_offset=cabling_offset,
                    pool=pod_pools.get("technical"),
                    p2p_prefix_length=p2p_prefix_length,
                ),
            )
            if routing_opts.get("design"):
                await self.create_routing(
                    bottom_devices=spines,
                    top_devices=sibling_spines,
                    options=routing_opts,
                    p2p_interfaces=p2p_pairs,
                    bottom_role="spine",
                    top_role="spine",
                )
