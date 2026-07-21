"""Infrastructure generator for pod topology creation."""

from typing import Any, Literal, cast

from utils.data_cleaning import clean_data

from ..common import CablingOptions, CommonGenerator, DeviceOptions, RoutingOptions
from ..helpers.routing import RoutingStrategy
from ..models import PodModel
from ..protocols import DcimPhysicalDevice, LocationRack, TopologyPod


class PodTopologyGenerator(CommonGenerator):
    """Generate pod topology with resource pools and spine infrastructure.

    Creates resource pools (technical and management) and creates spine devices
    within a pod topology.
    """

    async def update_checksum(self) -> None:
        """Update checksum for racks in the pod and add them to group context for protection.

        Combined operation to avoid querying racks twice:
        1. Protects all existing racks from deletion
        2. Updates checksum for network/tor racks to trigger their generation
        """

        # Query all racks in this pod once
        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[self.data.id],
            rack_type__values=["network", "tor"],
        )

        # Use the pre-routing snapshot if available (captured before protection IDs
        # are added to related_node_ids). Falls back to live calculation for
        # backward compatibility (e.g., if called without running full generate()).
        pod_checksum = self.calculate_checksum()

        # Get deployment type from pod (design doesn't have deployment_type)
        deployment_type = self.data.deployment_type

        for rack in racks:
            # Always add to group context to prevent deletion
            self.client.group_context.related_node_ids.append(rack.id)

            # Determine if this rack's checksum should be updated based on deployment type
            # For mixed: only update network racks (ToR racks inherit from middle racks after leafs are created)
            should_update = deployment_type in ["tor", "middle_rack"] or (
                deployment_type == "mixed" and rack.rack_type.value == "network"
            )

            if should_update and rack.checksum.value != pod_checksum:
                rack.checksum.value = pod_checksum
                await rack.save(allow_upsert=True)
                self.logger.info(f"Checksum updated: {rack.name.value} → {pod_checksum} (triggers rack re-generation)")

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate pod topology infrastructure."""

        try:
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

        spine_count = self.data.amount_of_spines
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

        spine_template = self.data.spine_template

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
            max_super_spines = dc.amount_of_super_spines
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
        if dc.super_spine_asn_pool and dc.super_spine_asn_pool.name:
            dc_asn_pool_id = dc.super_spine_asn_pool.id
            dc_asn_pool_name = dc.super_spine_asn_pool.name

            # Propagate DC pool reference to pod so rack generator can find it via pod.asn_pool
            pod_obj = await self.client.get(kind="TopologyPod", id=pod_id)
            if pod_obj:
                pod_obj.asn_pool = {"id": dc.super_spine_asn_pool.id}
                await pod_obj.save(allow_upsert=True)
                self.logger.info(f"Pod {self.data.name}: linked to DC ASN pool '{dc_asn_pool_name}'")

        # Pass management pool ID from DC parent (create_devices resolves ID to SDK object)
        management_pool_id = dc.management_pool.id if dc.management_pool else None

        spines = await self.create_devices(
            deployment_id=self.data.id,
            device_role="spine",
            amount=spine_count,
            template=spine_template.model_dump(),
            naming_convention=naming_conv,
            options=DeviceOptions(
                indexes=indexes,
                allocate_loopback=True,
                loopback_pool=pod_pools.get("loopback"),
                loopback_prefix_length=128 if is_ipv6 else 32,
                management_pool=management_pool_id,
            ),
        )

        parent = self.data.parent
        super_spine_devices = [device.name for device in (parent.devices or [])]
        super_spine_interfaces = (
            [iface.name for iface in parent.super_spine_template.interfaces] if parent.super_spine_template else []
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

        spine_interfaces_data = spine_template.interfaces
        spine_interfaces = [iface.name for iface in spine_interfaces_data]
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

        is_back_to_back = dc_design is not None and getattr(dc_design, "max_super_spines_per_fabric", 0) == 0

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
        elif is_back_to_back:
            # Back-to-back: cable this pod's spines to spines in lower-index sibling pods.
            # Only connecting to lower-index pods avoids creating duplicate cables when
            # pod2 generator runs (pod2 → pod1), but pod1 generator has no pod to connect to.
            dc_max_spines = dc_design.max_spines_per_pod if dc_design else spine_count
            p2p_prefix_length = 127 if is_ipv6 else 31
            technical_pool = pod_pools.get("technical")

            sibling_pods = await self._get_sibling_pods_with_lower_index(dc.id)
            if not sibling_pods:
                self.logger.info(
                    f"Pod {self.data.name}: back-to-back design but no lower-index sibling pods found — "
                    "no inter-pod cabling to create (this is pod index 1 or the only pod)"
                )
            for sibling_pod_id, sibling_spine_names, sibling_index in sibling_pods:
                if not sibling_spine_names:
                    self.logger.warning(
                        f"Pod {self.data.name}: sibling pod index {sibling_index} has no spine devices yet — "
                        "skipping inter-pod cabling for that sibling"
                    )
                    continue
                # Offset: use sibling pod index so interfaces don't collide across siblings
                cabling_offset = (sibling_index - 1) * dc_max_spines
                self.logger.info(
                    f"Pod {self.data.name} (idx={self.data.index}): cabling to sibling pod idx={sibling_index} "
                    f"[{len(spines)} spines → {len(sibling_spine_names)} sibling spines, offset={cabling_offset}]"
                )
                routing_opts = (
                    RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id)
                    if dc_design and dc_asn_pool_id
                    else RoutingOptions()
                )
                p2p_pairs = await self.create_cabling(
                    bottom_devices=spines,
                    bottom_interfaces=spine_interfaces,
                    top_devices=sibling_spine_names,
                    top_interfaces=spine_interfaces,  # Same template → same interface names
                    strategy="pod",
                    options=CablingOptions(
                        cabling_offset=cabling_offset,
                        pool=technical_pool,
                        p2p_prefix_length=p2p_prefix_length,
                    ),
                    bottom_sorting=self.data.spine_interface_sorting_method,
                    top_sorting=self.data.spine_interface_sorting_method,
                )
                if routing_opts.get("design"):
                    await self.create_routing(
                        bottom_devices=spines,
                        top_devices=sibling_spine_names,
                        options=routing_opts,
                        p2p_interfaces=p2p_pairs,
                        bottom_role="spine",
                        top_role="spine",
                    )
        elif dc_design and dc_design.routing_strategy == RoutingStrategy.OSPF_IBGP.value:
            # No super-spines and not back-to-back — OSPF_IBGP design with super-spine
            # support configured but zero super-spines actually deployed. Nothing above
            # seeds spine overlay BGP for this case: the pre-seed block explicitly
            # excludes OSPF_IBGP (needs overlay_as_id resolved inside create_routing),
            # and the post-cable block never runs since cabling is skipped. eBGP
            # strategies don't need this fallback — their pre-seed call above already
            # runs with top_devices=[] when there are no super-spines. Seed overlay BGP
            # only (skip_underlay=True): spines already have their OSPF underlay from
            # rack.py's leaf<->spine cabling.
            if dc_asn_pool_id:
                await self.create_routing(
                    bottom_devices=spines,
                    top_devices=[],
                    options=RoutingOptions(design=dc_design, asn_pool=dc_asn_pool_id, skip_underlay=True),
                    p2p_interfaces=[],
                    bottom_role="spine",
                )

        await self.update_checksum()

    async def _get_sibling_pods_with_lower_index(self, dc_id: str) -> list[tuple[str, list[str], int]]:
        """Return spine device names for all sibling pods with index < self.data.index.

        Returns a list of (pod_id, [spine_names], pod_index) tuples, sorted by pod index.
        Only pods with a lower index than the current pod are returned to ensure each
        spine-to-spine cable is created exactly once across all pod generator runs.
        """
        all_pods = await self.client.filters(kind=TopologyPod, parent__ids=[dc_id])
        result: list[tuple[str, list[str], int]] = []

        for pod in all_pods:
            pod_index = pod.index.value
            if pod_index >= self.data.index:
                continue  # Only connect to lower-index pods

            # Fetch spine devices in this sibling pod
            sibling_spines = await self.client.filters(
                kind=DcimPhysicalDevice,
                deployment__ids=[pod.id],
                role__value="spine",
            )
            spine_names = [s.name.value for s in sibling_spines]
            result.append((pod.id, spine_names, pod_index))

        result.sort(key=lambda x: x[2])
        return result
