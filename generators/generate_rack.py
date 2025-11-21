from __future__ import annotations

from typing import Literal, cast

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .models import RackModel


class RackGenerator(CommonGenerator):
    """Generator for creating rack infrastructure based on fabric templates."""

    async def generate(self, data: dict) -> None:
        """Generate rack topology with special handling for OOB and console devices."""
        try:
            deployment_list = clean_data(data).get("LocationRack", [])
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
                check_interface_role = "leaf"

                if created_leaf_devices:
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
                        leaf_role_interfaces = [f"Ethernet1/{i}" for i in range(25, 31)]

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
                    # No leaf switches in rack - find external leafs from the pod
                    # Query all leaf devices in the pod excluding this rack
                    external_leafs_query = await self._get_external_leafs(
                        pod_id=pod.id, exclude_rack_id=self.data.id
                    )

                    if external_leafs_query:
                        # Find the 2 leafs with most available ports
                        (
                            external_leaf_devices,
                            _,
                        ) = await self.get_devices_with_available_ports(
                            device_names=external_leafs_query,
                            interface_role="leaf",
                            top_n=2,
                        )

                        self.logger.info(
                            f"ToR connectivity: using {len(external_leaf_devices)} external leaf devices "
                            f"from pod {pod_name} for rack {self.data.name}"
                        )

                        # Get leaf role interfaces
                        leaf_role_interfaces = [f"Ethernet1/{i}" for i in range(25, 31)]

                        await self.create_cabling(
                            bottom_devices=tor_devices,
                            bottom_interfaces=tor_interfaces,
                            top_devices=external_leaf_devices,
                            top_interfaces=leaf_role_interfaces,
                            strategy="intra_rack",
                            options={
                                "pool": f"{pod_name}-technical-pool",
                            },
                        )
                    else:
                        self.logger.warning(
                            f"No external leaf switches available in pod {pod_name} for ToR connectivity"
                        )
            elif deployment_type == "mixed":
                # Mixed deployment: ToRs in this rack connect to local Leafs,
                # and external ToRs connect to this rack's Leafs (if available)
                check_interface_role = "leaf"

                if created_leaf_devices:
                    # Get leaf interfaces for ToR connectivity
                    leaf_role_interfaces = [
                        interface.name
                        for interface in (
                            self.data.leafs[0].template.interfaces
                            if self.data.leafs
                            else []
                        )
                        if interface.role == check_interface_role
                    ]

                    if not leaf_role_interfaces:
                        leaf_role_interfaces = [f"Ethernet1/{i}" for i in range(25, 31)]

                    # Connect local ToRs to local Leafs in this rack
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

                    # Now handle external ToRs from other racks in the same pod
                    # Query all ToR devices in the pod excluding this rack
                    external_tors_query = await self._get_external_tors(
                        pod_id=pod.id, exclude_rack_id=self.data.id
                    )

                    if external_tors_query:
                        self.logger.info(
                            f"Mixed deployment: connecting {len(external_tors_query)} external ToRs "
                            f"to {len(created_leaf_devices)} leafs in rack {self.data.name}"
                        )

                        # Get ToR uplink interfaces
                        tor_uplink_interfaces = [
                            interface.name
                            for interface in tor_role.template.interfaces
                            if interface.role == "uplink"
                        ]

                        # Connect external ToRs to this rack's Leafs using least-utilized strategy
                        await self.create_cabling(
                            bottom_devices=external_tors_query,
                            bottom_interfaces=tor_uplink_interfaces,
                            top_devices=created_leaf_devices,
                            top_interfaces=leaf_role_interfaces,
                            strategy="intra_rack",
                            options={
                                "pool": f"{pod_name}-technical-pool",
                            },
                        )
                else:
                    self.logger.warning(
                        f"Mixed deployment without local leafs in rack {self.data.name} - "
                        "ToRs will need external connectivity"
                    )

    async def _get_external_leafs(self, pod_id: str, exclude_rack_id: str) -> list[str]:
        """Get all leaf device names from the pod excluding specified rack.

        Args:
            pod_id: Pod ID to query
            exclude_rack_id: Rack ID to exclude

        Returns:
            List of leaf device names
        """
        query = """
        query GetExternalLeafs($pod_id: String!, $rack_id: String!) {
            DcimGenericDevice(
                role__name__value: "leaf"
                location__id: $pod_id
            ) {
                edges {
                    node {
                        id
                        name { value }
                        rack {
                            node { id }
                        }
                    }
                }
            }
        }
        """

        result = await self.client.execute_graphql(
            query=query,
            variables={"pod_id": pod_id, "rack_id": exclude_rack_id},
            branch_name=self.branch,
        )

        devices = result.get("DcimGenericDevice", {}).get("edges", [])
        external_leafs = [
            device["node"]["name"]["value"]
            for device in devices
            if device["node"].get("rack", {}).get("node", {}).get("id")
            != exclude_rack_id
        ]

        return external_leafs

    async def _get_external_tors(self, pod_id: str, exclude_rack_id: str) -> list[str]:
        """Get all ToR device names from the pod excluding specified rack.

        Args:
            pod_id: Pod ID to query
            exclude_rack_id: Rack ID to exclude

        Returns:
            List of ToR device names
        """
        query = """
        query GetExternalToRs($pod_id: String!, $rack_id: String!) {
            DcimGenericDevice(
                role__name__value: "tor"
                location__id: $pod_id
            ) {
                edges {
                    node {
                        id
                        name { value }
                        rack {
                            node { id }
                        }
                    }
                }
            }
        }
        """

        result = await self.client.execute_graphql(
            query=query,
            variables={"pod_id": pod_id, "rack_id": exclude_rack_id},
            branch_name=self.branch,
        )

        devices = result.get("DcimGenericDevice", {}).get("edges", [])
        external_tors = [
            device["node"]["name"]["value"]
            for device in devices
            if device["node"].get("rack", {}).get("node", {}).get("id")
            != exclude_rack_id
        ]

        return external_tors
