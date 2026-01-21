"""Infrastructure generator for pod topology creation."""

from typing import Any, Literal, cast

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..models import PodModel
from ..protocols import LocationRack


class PodTopologyGenerator(CommonGenerator):
    """Generate pod topology with resource pools and spine infrastructure.

    Creates resource pools (technical and management) and creates spine devices
    within a pod topology.
    """

    def _validate_layout_design_compatibility(self, layout: Any, design: Any) -> None:
        """Validate site layout is compatible with pod design.

        Args:
            layout: SiteLayout Pydantic model
            design: PodDesign Pydantic model

        Raises:
            RuntimeError: If layout and design are incompatible
        """
        layout_name = layout.name
        design_name = design.name

        # Check explicit compatibility list (if field exists)
        compatible_layouts = getattr(design, "compatible_layouts", None)
        if compatible_layouts:
            layout_names = [comp_layout.name for comp_layout in compatible_layouts]
            if layout_name not in layout_names:
                raise RuntimeError(
                    f"Pod design '{design_name}' not compatible with "
                    f"site layout '{layout_name}'. "
                    f"Compatible layouts: {', '.join(layout_names)}"
                )

        # Check ToR capacity
        max_tors = design.max_tors_per_row
        compute_racks = getattr(layout, "compute_racks_per_row", None)
        if max_tors and compute_racks and max_tors > compute_racks:
            raise RuntimeError(
                f"Design '{design_name}' requires {max_tors} ToRs/row "
                f"but layout '{layout_name}' only has {compute_racks} compute racks/row"
            )

        # Check Leaf capacity (assuming 4 leafs per network rack)
        max_leafs = design.max_leafs_per_row
        network_racks = getattr(layout, "network_racks_per_row", None)
        if max_leafs and network_racks:
            leafs_capacity = network_racks * 4
            if max_leafs > leafs_capacity:
                raise RuntimeError(
                    f"Design '{design_name}' requires {max_leafs} Leafs/row "
                    f"but layout '{layout_name}' only supports {leafs_capacity} leafs/row "
                    f"({network_racks} network racks × 4 leafs)"
                )

        self.logger.info(f"✓ Layout '{layout_name}' compatible with design '{design_name}'")

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

        pod_checksum = self.calculate_checksum()

        # Get deployment type from design if available, otherwise use legacy field
        deployment_type = self.data.design.deployment_type if self.data.design else self.data.deployment_type

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
        self.deployment_id = dc.id  # Store for cable linking
        self.pod_name = self.data.name.lower()
        self.fabric_name = dc.name.lower()

        # Use new design/layout architecture
        if self.data.design:
            design = self.data.design
            # Get naming convention from site_layout first, fallback to design, then default
            if self.data.site_layout and self.data.site_layout.naming_convention:
                naming_conv = cast(
                    Literal["standard", "hierarchical", "flat"],
                    (self.data.site_layout.naming_convention or "standard").lower(),
                )
            else:
                naming_conv = "standard"

            # Design is now properly typed as PodDesign model
            spine_count = design.spine_count
            spine_template = design.spine_template

            # Validate layout compatibility if both are present
            if self.data.site_layout:
                self._validate_layout_design_compatibility(self.data.site_layout, design)
        else:
            # Backward compatibility: Use pod attributes
            design = None
            naming_conv = "standard"
            spine_count = self.data.amount_of_spines
            spine_template = self.data.spine_template

        indexes: list[int] = [dc.index or 1, self.data.index]

        # Use pool sizes directly from design (no calculation needed)
        # Design specifies technical_pool_size and loopback_pool_size as integers (CIDR prefix length)
        # Pass as integers to allocate_resource_pools (GraphQL expects Int type, not string)
        pool_sizes = {}
        if design:
            if design.technical_pool_size:
                pool_sizes["technical"] = design.technical_pool_size
            if design.loopback_pool_size:
                pool_sizes["loopback"] = design.loopback_pool_size

        # Allocate pools with explicit sizes from design and capture the created pool objects
        pod_pools = await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=pool_sizes,
        )

        # Type guards for spine configuration
        if not spine_count:
            raise RuntimeError(f"Spine count not defined for pod {self.data.name}")
        if not spine_template:
            raise RuntimeError(f"Spine template not defined for pod {self.data.name}")
        if not hasattr(spine_template, "model_dump"):
            raise RuntimeError(f"Spine template missing model_dump method for pod {self.data.name}")
        if not hasattr(spine_template, "interfaces"):
            raise RuntimeError(f"Spine template missing interfaces for pod {self.data.name}")

        spines = await self.create_devices(
            deployment_id=self.data.id,
            device_role="spine",
            amount=spine_count,
            template=spine_template.model_dump(),
            naming_convention=naming_conv,
            options={
                "indexes": indexes,
                "allocate_loopback": True,
                "loopback_pool": pod_pools.get("loopback"),  # Pass pool object directly
                "prefix_pool": pod_pools.get("technical"),  # Pass pool object directly
            },
        )

        spine_interfaces_data = spine_template.interfaces
        spine_interfaces = [iface.name for iface in spine_interfaces_data]
        if not spine_interfaces:
            self.logger.error(
                f"Pod {self.data.name}: No uplink interfaces found in spine template. "
                "Cannot create spine-to-super-spine cabling."
            )
            raise RuntimeError(f"Pod {self.data.name}: Cannot cable spines - no uplink interfaces in template")

        parent = self.data.parent
        super_spine_devices = [device.name for device in (parent.devices or [])]
        super_spine_template = parent.super_spine_template
        super_spine_interfaces = [
            iface.name for iface in (super_spine_template.interfaces if super_spine_template else [])
        ]

        # Only fail if super-spines exist but template/interfaces are missing
        # Single-pod DCs with no super-spines are valid and should skip cabling
        if super_spine_devices and not super_spine_interfaces:
            self.logger.error(
                f"Pod {self.data.name}: Super-spine devices exist but no downlink interfaces found in template. "
                "Cannot create super-spine-to-spine cabling."
            )
            raise RuntimeError(
                f"Pod {self.data.name}: Cannot cable to super-spines - no downlink interfaces in template"
            )

        # Skip cabling if no super-spines (single-pod DC scenario)
        if not super_spine_devices or not super_spine_interfaces:
            self.logger.info(
                f"Pod {self.data.name}: Skipping spine-to-super-spine cabling (single-pod DC or no super-spines)"
            )
            return

        # Extract maximum_spines from design, default to spine_count if None
        maximum_spines = (
            design.maximum_spines
            if design and design.maximum_spines is not None
            else (design.spine_count if design else 2)
        )

        await self.create_cabling(
            bottom_devices=spines,
            bottom_interfaces=spine_interfaces,
            top_devices=super_spine_devices,
            top_interfaces=super_spine_interfaces,
            strategy="pod",
            options={
                "cabling_offset": ((self.data.index - 1) * (maximum_spines or 0)),
                "top_sorting": self.data.spine_interface_sorting_method,
                "bottom_sorting": self.data.spine_interface_sorting_method,
                "pool": f"{self.pod_name}-technical-pool",
            },
        )

        # Update checksums for middle racks first (rack_type="network" with leafs)
        # This triggers middle rack generation before ToR racks in mixed deployments
        self.logger.info("Updating checksums for middle racks (network type) to trigger their generation first")
        await self.update_checksum()
