from __future__ import annotations

from typing import Literal, cast

from .common import CommonGenerator
from .models import RackModel


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        try:
            deployment_list = self.clean_data(data).get("LocationRack", [])
            if not deployment_list:
                self.logger.error("No Rack Deployment data found in GraphQL response")
                return

            self.data = RackModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        self.logger.info(f"Generating topology for rack {self.data.name}")
        dc = self.data.pod.parent
        design = dc.design_pattern
        pod = self.data.pod
        pod_name = pod.name.lower()
        fabric_name = dc.name.lower()
        indexes: list[int] = [
            dc.index,
            pod.index,
            self.data.index,
        ]

        naming_conv = cast(
            Literal["standard", "hierarchical", "flat"],
            design.naming_convention.lower(),
        )

        deployment_type = pod.deployment_type
        created_leaf_devices = []

        # Process leaf devices first
        for leaf_role in self.data.leafs or []:
            # Create leaf devices and collect them for cabling
            leaf_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role=leaf_role.role,
                amount=leaf_role.quantity,
                template=leaf_role.template.model_dump(),
                naming_convention=naming_conv,
                options={
                    "pod_name": pod_name,
                    "fabric_name": fabric_name,
                    "indexes": indexes,
                    "allocate_loopback": True,
                    "rack": self.data.id,
                },
            )

            created_leaf_devices.extend(leaf_devices)

            leaf_interfaces = [
                interface.name for interface in leaf_role.template.interfaces
            ]
            spine_devices = [device.name for device in pod.devices]
            spine_interfaces = [iface.name for iface in pod.spine_template.interfaces]

            await self.create_cabling(
                bottom_devices=leaf_devices,
                bottom_interfaces=leaf_interfaces,
                top_devices=spine_devices,
                top_interfaces=spine_interfaces,
                strategy="rack",
                options={
                    "cabling_offset": (
                        (self.data.index - 1) * (design.maximum_rack_leafs or 2)
                    ),
                    "top_sorting": pod.spine_interface_sorting_method,
                    "bottom_sorting": pod.leaf_interface_sorting_method,
                    "pool": f"{pod_name}-technical-pool",
                },
            )

        # Process ToR devices with specific connectivity logic
        for tor_role in self.data.tors or []:
            # Create ToR devices
            tor_devices = await self.create_devices(
                deployment_id=pod.id,
                device_role=tor_role.role,
                amount=tor_role.quantity,
                template=tor_role.template.model_dump(),
                naming_convention=naming_conv,
                options={
                    "pod_name": pod_name,
                    "fabric_name": fabric_name,
                    "indexes": indexes,
                    "allocate_loopback": False,
                    "rack": self.data.id,
                },
            )

            # Get ToR uplink interfaces for connectivity
            tor_interfaces = [
                interface.name
                for interface in tor_role.template.interfaces
                if interface.role == "uplink"
            ]

            # Determine ToR connectivity based on deployment_type
            if deployment_type == "tor":
                # Connect ToRs to spine switches
                spine_devices = [device.name for device in pod.devices]
                spine_interfaces = [
                    iface.name for iface in pod.spine_template.interfaces
                ]

                await self.create_cabling(
                    bottom_devices=tor_devices,
                    bottom_interfaces=tor_interfaces,
                    top_devices=spine_devices,
                    top_interfaces=spine_interfaces,
                    strategy="rack",
                    options={
                        "cabling_offset": (
                            (self.data.index - 1) * (design.maximum_tors or 2)
                        ),
                        "top_sorting": pod.spine_interface_sorting_method,
                        "bottom_sorting": pod.leaf_interface_sorting_method,
                        "pool": f"{pod_name}-technical-pool",
                    },
                )
            elif deployment_type == "middle_rack":
                # Connect ToRs to leaf switches in the same rack if available
                # Use "leaf" role interfaces on leaf switches for ToR connectivity
                check_interface_role = "leaf"

                # Get the leaf devices that were created in this rack
                # (we know their names from the create_devices call earlier)
                if created_leaf_devices:
                    self.logger.info(
                        f"ToR connectivity: using {len(created_leaf_devices)} leaf devices "
                        f"created in rack {self.data.name}"
                    )

                    # Get leaf interfaces for ToR connectivity from template
                    leaf_role_interfaces = [
                        interface.name
                        for interface in (
                            self.data.leafs[0].template.interfaces
                            if self.data.leafs
                            else []
                        )
                        if interface.role == check_interface_role
                    ]
                    
                    # If template doesn't have interface details, use known interface names
                    # for N9K_C9336C_FX2_LEAF_MR template (Ethernet1/25-30 are leaf role)
                    if not leaf_role_interfaces:
                        leaf_role_interfaces = [
                            f"Ethernet1/{i}" for i in range(25, 31)
                        ]

                    # Use load-balanced cabling strategy that distributes ToRs evenly across all leafs
                    await self.create_cabling(
                        bottom_devices=tor_devices,
                        bottom_interfaces=tor_interfaces,
                        top_devices=created_leaf_devices,
                        top_interfaces=leaf_role_interfaces,
                        strategy="intra_rack",
                        options={
                            "pool": f"{pod_name}-technical-pool",
                        },
                    )
                else:
                    # No leaf switches in rack - connect to 2 leaf switches with lowest port usage
                    # TODO: Implement logic to find leaf switches with lowest port usage
                    self.logger.warning(
                        f"No leaf switches in rack {self.data.name}, "
                        "ToR connectivity to external leafs not yet implemented"
                    )
