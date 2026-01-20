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

        for rack in racks:
            # Always add to group context to prevent deletion
            self.client.group_context.related_node_ids.append(rack.id)

            # Determine if this rack's checksum should be updated based on deployment type
            should_update = self.data.deployment_type in ["tor", "middle_rack"] or (
                self.data.deployment_type == "mixed" and rack.rack_type.value == "network"
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
        design = dc.design_pattern
        indexes: list[int] = [dc.index or 1, self.data.index]

        await self.allocate_resource_pools(
            id=pod_id,
            strategy="pod",
            pools=design.model_dump() if design else {},
        )

        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            ((design.naming_convention if design else None) or "standard").lower(),
        )

        spines = await self.create_devices(
            deployment_id=self.data.id,
            device_role="spine",
            amount=self.data.amount_of_spines,
            template=self.data.spine_template.model_dump(),
            naming_convention=naming_conv,
            options={
                "indexes": indexes,
                "allocate_loopback": True,
            },
        )

        spine_switch_template = self.data.spine_template
        spine_interfaces_data = spine_switch_template.interfaces
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

        await self.create_cabling(
            bottom_devices=spines,
            bottom_interfaces=spine_interfaces,
            top_devices=super_spine_devices,
            top_interfaces=super_spine_interfaces,
            strategy="pod",
            options={
                "cabling_offset": ((self.data.index - 1) * ((design.maximum_spines if design else None) or 2)),
                "top_sorting": self.data.spine_interface_sorting_method,
                "bottom_sorting": self.data.spine_interface_sorting_method,
                "pool": f"{self.pod_name}-technical-pool",
            },
        )

        # Update checksums for middle racks first (rack_type="network" with leafs)
        # This triggers middle rack generation before ToR racks in mixed deployments
        self.logger.info("Updating checksums for middle racks (network type) to trigger their generation first")
        await self.update_checksum()
