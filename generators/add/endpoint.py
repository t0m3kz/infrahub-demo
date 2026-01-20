"""Infrastructure generator for endpoint device connectivity.

This generator connects endpoint devices (servers) to network infrastructure
based on deployment type (middle_rack, tor, mixed). It follows deployment-aware
routing logic with proper interface type matching and dual-homing support.
"""

from __future__ import annotations

from typing import Any, Literal

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..models import EndpointModel
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface


class EndpointConnectivityGenerator(CommonGenerator):
    """Generate connectivity for endpoint devices based on deployment patterns.

    Deployment strategies:
    - middle_rack: Connect to ToRs in same rack, fallback to ToRs in same row
    - tor: Connect to ToR switches in same rack, fallback to same row
    - mixed: Connect to ToR devices in same rack, fallback to middle rack leafs in same row

    Features:
    - Interface type and role matching (customer ↔ access)
    - Dual-homing across consecutive device pairs
    - Idempotency via existing cable detection
    - Uses CablingPlanner and CommonGenerator.create_cabling()
    """

    async def generate(self, data: dict[str, Any]) -> None:
        """Generate endpoint device connectivity based on deployment type."""
        try:
            deployment_list = clean_data(data).get("DcimDevice", [])
            if not deployment_list:
                self.logger.error("No Endpoint Device data found in GraphQL response")
                return

            model_data = EndpointModel(endpoint=deployment_list[0])
            self.data = model_data.endpoint
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error(f"Generation failed due to {exc}")
            return

        if not self.data.rack:
            self.logger.error(f"Endpoint {self.data.name} has no rack assigned - cannot determine connectivity")
            return

        deployment_type = self.data.rack.pod.deployment_type
        self.pod_name = self.data.rack.pod.name.lower()
        pod_id = self.data.rack.pod.id
        dc = self.data.rack.pod.parent
        self.deployment_id = dc.id
        self.fabric_name = dc.name.lower()

        self.logger.info(f"Generating connectivity for endpoint {self.data.name} in {deployment_type} deployment")

        # Update endpoint device to set deployment to pod
        endpoint_device = await self.client.get(kind=DcimPhysicalDevice, id=self.data.id)
        current_deployment = endpoint_device.deployment.id
        if current_deployment != pod_id:
            endpoint_device.deployment = pod_id  # type: ignore
            await endpoint_device.save()
            self.logger.info(f"Updated {self.data.name} deployment to pod {self.pod_name}")

        if deployment_type == "middle_rack":
            await self._connect_middle_rack_deployment()
        elif deployment_type == "tor":
            await self._connect_tor_deployment()
        elif deployment_type == "mixed":
            await self._connect_mixed_deployment()
        else:
            self.logger.error(f"Unknown deployment type '{deployment_type}' for endpoint {self.data.name}")

    async def _connect_middle_rack_deployment(self) -> None:
        """Connect endpoint in middle_rack deployment.

        Strategy: Server in compute rack connects to ToRs in the middle rack (network rack) in same row.
        Middle_rack topology has one network rack per row containing ToR switches that serve compute racks.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        self.logger.info(
            f"Endpoint {self.data.name} is in {self.data.rack.rack_type} rack "
            f"(row {self.data.rack.row_index}), searching for ToRs in middle rack (network rack) in same row"
        )

        # In middle_rack deployment, ToRs are in the network rack, not the compute rack
        # We need to find ToRs in the middle rack (rack_type=network) in the same row
        target_devices = await self._get_devices_in_middle_rack(
            pod_id=self.data.rack.pod.id,
            row_index=self.data.rack.row_index,
            role="tor",
        )

        if not target_devices:
            self.logger.warning(
                f"No ToR devices found in middle rack for endpoint {self.data.name} "
                f"(row {self.data.rack.row_index}, middle_rack deployment)"
            )
            return

        await self._create_endpoint_connections(target_devices, "tor")

    async def _connect_tor_deployment(self) -> None:
        """Connect endpoint in tor deployment.

        Strategy: Connect to ToR switches in same rack, fallback to same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        target_devices = self._get_devices_in_rack(role="tor")

        if not target_devices:
            self.logger.info(
                f"No ToRs in same rack for {self.data.name}, searching same row {self.data.rack.row_index}"
            )
            target_devices = await self._get_devices_in_row(
                pod_id=self.data.rack.pod.id,
                row_index=self.data.rack.row_index,
                role="tor",
            )

        if not target_devices:
            self.logger.warning(f"No ToR devices found for endpoint {self.data.name} (tor deployment)")
            return

        await self._create_endpoint_connections(target_devices, "tor")

    async def _connect_mixed_deployment(self) -> None:
        """Connect endpoint in mixed deployment.

        Strategy: Connect to ToR devices in same rack, fallback to middle rack leafs in same row.
        """
        # Safe to assert - validated in generate() before calling this method
        assert self.data.rack is not None, "Rack must be assigned"

        target_devices = self._get_devices_in_rack(role="tor")
        target_role: Literal["tor", "leaf"] = "tor"

        if not target_devices:
            self.logger.info(
                f"No ToRs in same rack for {self.data.name}, searching middle rack leafs in row {self.data.rack.row_index}"
            )
            target_devices = await self._get_devices_in_row(
                pod_id=self.data.rack.pod.id,
                row_index=self.data.rack.row_index,
                role="leaf",
            )
            target_role = "leaf"

        if not target_devices:
            self.logger.warning(
                f"No ToR or middle rack leaf devices found for endpoint {self.data.name} (mixed deployment)"
            )
            return

        await self._create_endpoint_connections(target_devices, target_role)

    def _get_devices_in_rack(self, role: Literal["tor", "leaf"]) -> list[dict[str, Any]]:
        """Extract devices of specific role from rack data.

        Args:
            role: Device role to filter by (tor or leaf)

        Returns:
            List of matching devices with id, name, and role
        """
        # Safe to assert - validated in generate() before calling deployment methods
        assert self.data.rack is not None, "Rack must be assigned"

        matching_devices = []

        for device in self.data.rack.devices:
            if device.role == role:
                matching_devices.append(
                    {
                        "id": device.id,
                        "name": {"value": device.name},
                        "role": {"value": device.role},
                    }
                )

        return matching_devices

    async def _get_devices_in_middle_rack(
        self, pod_id: str, row_index: int, role: Literal["tor", "leaf"]
    ) -> list[dict[str, Any]]:
        """Query devices in the middle rack (network rack) of specific row.

        In middle_rack deployments, the network rack contains ToRs and Leafs that
        serve compute racks in the same row.
        """
        from ..protocols import LocationRack

        # Find the network rack in this row
        racks = await self.client.filters(
            kind=LocationRack,
            pod__ids=[pod_id],
            row_index__value=row_index,
            rack_type__value="network",
        )

        if not racks:
            self.logger.warning(f"No network rack found in row {row_index} for middle_rack deployment")
            return []

        rack_ids = [rack.id for rack in racks]

        devices = await self.client.filters(kind=DcimPhysicalDevice, role__value=role, rack__ids=rack_ids)

        return [
            {
                "id": device.id,
                "name": {"value": device.name.value},
                "role": {"value": device.role.value},
            }
            for device in devices
        ]

    async def _get_devices_in_row(
        self, pod_id: str, row_index: int, role: Literal["tor", "leaf"]
    ) -> list[dict[str, Any]]:
        """Query devices of specific role in same row across all racks."""
        from ..protocols import LocationRack

        racks = await self.client.filters(kind=LocationRack, pod__ids=[pod_id], row_index__value=row_index)

        if not racks:
            return []

        rack_ids = [rack.id for rack in racks]

        devices = await self.client.filters(kind=DcimPhysicalDevice, role__value=role, rack__ids=rack_ids)

        return [
            {
                "id": device.id,
                "name": {"value": device.name.value},
                "role": {"value": device.role.value},
            }
            for device in devices
        ]

    async def _create_endpoint_connections(
        self,
        target_devices: list[dict[str, Any]],
        target_role: Literal["tor", "leaf"],
    ) -> None:
        """Create dual-homed connections from endpoint to target devices.

        Args:
            target_devices: List of target device dictionaries
            target_role: Role of target devices (tor or leaf)
        """
        if len(target_devices) < 2:
            self.logger.warning(f"Need at least 2 {target_role} devices for dual-homing, found {len(target_devices)}")
            return

        target_pair = self._select_consecutive_device_pair(target_devices, target_role)
        if len(target_pair) < 2:
            self.logger.warning(f"Could not find consecutive {target_role} pair with compatible interfaces")
            return

        target_device_names = [dev.get("name", {}).get("value") for dev in target_pair]

        self.logger.info(f"Selected {target_role} pair for {self.data.name}: {target_device_names}")

        # Get endpoint interfaces without existing cables
        available_endpoint_interfaces = [intf for intf in self.data.interfaces if not intf.cable]

        if not available_endpoint_interfaces:
            self.logger.info(f"All interfaces on {self.data.name} already have cables")
            return

        target_interfaces = await self._query_compatible_interfaces(
            device_names=target_device_names,
            endpoint_interfaces=available_endpoint_interfaces,
        )

        if not target_interfaces:
            self.logger.warning(f"No compatible access interfaces found on {target_role} devices {target_device_names}")
            return

        endpoint_intf_names = [intf.name for intf in available_endpoint_interfaces[:4]]
        target_intf_names = sorted(set(intf.name.value for intf in target_interfaces))

        await self.create_cabling(
            bottom_devices=[self.data.name],
            bottom_interfaces=endpoint_intf_names,
            top_devices=target_device_names,
            top_interfaces=target_intf_names,
            strategy="intra_rack",
            options={
                "cabling_offset": 0,
                "bottom_sorting": "bottom_up",
                "top_sorting": "bottom_up",
            },
        )

        self.logger.info(
            f"Successfully created dual-homed connections for {self.data.name} to {target_role}s {target_device_names}"
        )

    def _select_consecutive_device_pair(self, devices: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
        """Select pair of consecutive devices for dual-homing."""
        import re

        def extract_id(name: str) -> int | None:
            match = re.search(r"(\d+)$", name)
            return int(match.group(1)) if match else None

        device_map = {}
        for device in devices:
            name = device.get("name", {}).get("value", "")
            dev_id = extract_id(name)
            if dev_id is not None:
                device_map[dev_id] = device

        sorted_ids = sorted(device_map.keys())
        for i in range(len(sorted_ids) - 1):
            id1, id2 = sorted_ids[i], sorted_ids[i + 1]
            if id2 - id1 == 1 and id1 % 2 == 1:
                return [device_map[id1], device_map[id2]]

        return devices[:2]

    async def _query_compatible_interfaces(
        self, device_names: list[str], endpoint_interfaces: list[Any]
    ) -> list[DcimPhysicalInterface]:
        """Query compatible interfaces on target devices for endpoint connectivity.

        Args:
            device_names: Names of target devices (ToRs or Leafs)
            endpoint_interfaces: Endpoint interface models

        Returns:
            List of compatible interfaces on target devices

        Note:
            For ToRs/Leafs connecting to endpoints, we look for:
            - Interfaces with roles: downlink, customer, access, or peer
            - Matching interface types if endpoint has specific types
            - Available interfaces (not already connected)
        """
        endpoint_types = set(intf.interface_type for intf in endpoint_interfaces if intf.interface_type)

        # ToR/Leaf interfaces connecting to endpoints typically have these roles
        acceptable_roles = ["downlink", "customer"]

        if not endpoint_types:
            # No specific interface type requirement - query by role only
            all_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=device_names,
                role__values=acceptable_roles,
            )
        else:
            # Match both interface type and role
            all_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=device_names,
                interface_type__values=list(endpoint_types),
                role__values=acceptable_roles,
            )

        self.logger.info(
            f"Query returned {len(all_interfaces)} interfaces matching roles={acceptable_roles}, types={endpoint_types}"
        )

        # Filter out interfaces that already have cables
        # Note: cable is a RelatedNode with id=None for uncabled interfaces
        interfaces = [intf for intf in all_interfaces if not (intf.cable and intf.cable.id)]

        self.logger.info(
            f"Found {len(interfaces)} available (uncabled) compatible interfaces on devices {device_names} "
            f"(interface_types={endpoint_types}, roles={acceptable_roles}, total_compatible={len(all_interfaces)})"
        )
        return interfaces
