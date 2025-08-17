"""Network Segment Generator for Multi-Customer Fabrics."""

import logging
from typing import Any, Dict, List

from infrahub_sdk.generator import InfrahubGenerator

from .common import TopologyCreator


class NetworkSegmentCreator(TopologyCreator):
    """Create network segments for multi-customer fabrics."""

    def __init__(
        self, client: Any, log: logging.Logger, branch: str, data: dict
    ) -> None:
        super().__init__(client, log, branch, data)
        self.segment_objects: List[Any] = []  # Track created segments

    async def create_network_segments(self, customers: List[Dict[str, Any]]) -> None:
        """Create network segments for each customer with VLAN from customer pool."""
        self.log.info("Creating multitenant network segments")

        segment_data_list: List[Dict[str, Any]] = []

        for customer in customers:
            customer_name = customer.get("name", "")
            segments = customer.get("segments", [])

            for segment_info in segments:
                segment_name = f"{customer_name}-{segment_info.get('name', 'default')}"

                # VLAN assigned from customer pool (VNI = VLAN + 10000, RD = VLAN)
                vlan_id = segment_info.get("vlan_id")
                if not vlan_id:
                    self.log.warning(f"No VLAN ID provided for segment {segment_name}")
                    continue

                vni = vlan_id + 10000
                rd = str(vlan_id)

                segment_data_list.append(
                    {
                        "payload": {
                            "name": segment_name,
                            "description": f"Network segment for customer {customer_name} (VLAN: {vlan_id}, VNI: {vni}, RD: {rd})",
                            "vlan_id": vlan_id,
                            "segment_type": segment_info.get("type", "l2_only"),
                            "tenant_isolation": "customer_dedicated",
                            "external_routing": segment_info.get(
                                "external_routing", False
                            ),
                            "status": "active",
                        },
                        "store_key": f"segment-{segment_name}",
                    }
                )

        await self._create_in_batch(
            kind="ServiceNetworkSegment", data_list=segment_data_list
        )

        # Store segment references for later use
        for segment_data in segment_data_list:
            segment_key = segment_data["store_key"]
            segment_obj = self.client.store.get(key=segment_key)
            if segment_obj:
                self.segment_objects.append(segment_obj)

    async def assign_interfaces_to_segments(
        self, customer_data: List[Dict[str, Any]]
    ) -> None:
        """Assign interfaces to network segments based on customer requirements."""
        self.log.info("Assigning interfaces to network segments")

        for customer in customer_data:
            segments = customer.get("segments", [])

            for segment_info in segments:
                segment_name = (
                    f"{customer.get('name', '')}-{segment_info.get('name', 'default')}"
                )

                # Find the segment object
                segment = None
                for seg_obj in self.segment_objects:
                    if seg_obj.name.value == segment_name:
                        segment = seg_obj
                        break

                if not segment:
                    self.log.warning(f"Segment {segment_name} not found")
                    continue

                # Assign interfaces based on interface mappings
                interface_mappings = segment_info.get("interface_mappings", [])
                for mapping in interface_mappings:
                    device_name = mapping.get("device")
                    interface_name = mapping.get("interface")

                    if device_name and interface_name:
                        self.log.info(
                            f"Would assign interface {interface_name} on {device_name} to segment {segment_name}"
                        )
                        # Note: Interface assignment would be implemented here
                        # The exact API depends on the schema and SDK capabilities


class NetworkSegmentGenerator(InfrahubGenerator):
    """Generate network segments for multi-customer fabrics.

    This generator creates network segments with automated VLAN/VNI/RD assignment
    based on customer requirements and fabric pools.
    """

    async def generate(self, data: dict) -> None:
        """Generate network segments for customers."""
        from .common import clean_data

        cleaned_data = clean_data(data)
        if isinstance(cleaned_data, dict):
            customer_data = cleaned_data.get("customers", [])
            fabric_data = cleaned_data.get("fabric", {})
        else:
            raise ValueError("clean_data() did not return a dictionary")

        self.logger.info(
            f"Generating network segments for {len(customer_data)} customers"
        )

        # Create network segment creator
        segment_creator = NetworkSegmentCreator(
            client=self.client, log=self.logger, branch=self.branch, data=fabric_data
        )

        # Create segments
        if customer_data:
            await segment_creator.create_network_segments(customer_data)
            await segment_creator.assign_interfaces_to_segments(customer_data)
        else:
            self.logger.warning("No customer data provided, skipping segment creation")
