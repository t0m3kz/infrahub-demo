from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional, TypeVar

from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool
from pydantic import BaseModel

from .helpers import CablingStrategy, DeviceNamingConfig, build_cabling_plan
from .schema_protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    TopologyPod,
)

T = TypeVar("T", bound=BaseModel)


SIZE_MAPPING: Dict[str, Dict[str, Dict[str, int]]] = {
    "S": {
        "spine": {
            "technical": 26,
            "management": 26,
            "management_prefix": 26,
            "loopback": 28,
        },
        "super-spine": {
            "technical": 24,
            "management": 24,
            "management_prefix": 31,
            "loopback": 28,
        },
    },
    "M": {
        "spine": {
            "technical": 25,
            "management": 25,
            "management_prefix": 25,
            "loopback": 27,
        },
        "super-spine": {
            "technical": 21,
            "management": 23,
            "management_prefix": 30,
            "loopback": 27,
        },
    },
    "L": {
        "spine": {
            "technical": 23,
            "management": 23,
            "management_prefix": 24,
            "loopback": 26,
        },
        "super-spine": {
            "technical": 19,
            "management": 21,
            "management_prefix": 28,
            "loopback": 26,
        },
    },
    "XL": {
        "spine": {
            "technical": 21,
            "management": 22,
            "management_prefix": 23,
            "loopback": 25,
        },
        "super-spine": {
            "technical": 16,
            "management": 20,
            "management_prefix": 28,
            "loopback": 25,
        },
    },
}


class CommonGenerator(InfrahubGenerator):
    """
    An extended InfrahubGenerator with helper methods for creating objects.
    """

    def clean_data(self, data: dict) -> Any:
        """Recursively transforms the input data by extracting 'value', 'node', or 'edges' from dictionaries.

        Handles GraphQL response structures including:
        - Single-value dicts: {'value': X} → X
        - Node wrappers: {'node': {...}} → {...}
        - Edge lists: {'edges': [{...}]} → [...]
        - Parent wrappers: {'parent': {...}} → {...}
        - Nested structures with recursive cleaning
        - Multiple top-level query keys

        Args:
            data: The input data to clean.

        Returns:
            The cleaned data with extracted values.
        """
        # GraphQL wrapper keys to unwrap (order matters - tried first to last)
        WRAPPER_KEYS = ["value", "node", "parent", "edges"]

        if isinstance(data, dict):
            dict_result = {}
            for key, value in data.items():
                if isinstance(value, dict):
                    # Check for pure single-key wrappers
                    unwrapped = False
                    for wrapper_key in WRAPPER_KEYS:
                        if value.get(wrapper_key) and len(value) == 1:
                            # Pure wrapper found -> unwrap and clean
                            dict_result[key] = self.clean_data(value[wrapper_key])
                            unwrapped = True
                            break

                    if not unwrapped:
                        # No pure wrapper found -> recurse fully
                        if "__" in key:
                            # GraphQL double-underscore keys -> normalize
                            dict_result[key.replace("__", "")] = self.clean_data(value)
                        else:
                            dict_result[key] = self.clean_data(value)
                elif "__" in key:
                    # GraphQL double-underscore keys -> normalize
                    dict_result[key.replace("__", "")] = value
                else:
                    # Recurse on other values (lists, scalars, etc.)
                    dict_result[key] = self.clean_data(value)
            return dict_result

        if isinstance(data, list):
            list_result = []
            for item in data:
                if isinstance(item, dict) and item.get("node") is not None:
                    # Item has 'node' wrapper -> extract and clean the node
                    cleaned_item = self.clean_data(item["node"])
                else:
                    # Regular item -> clean as-is
                    cleaned_item = self.clean_data(item)
                list_result.append(cleaned_item)
            return list_result

        # Return the data as-is if it's a scalar
        return data

    def calculate_checksum(self) -> str:
        """Calculate a SHA256 checksum of related IDs from the current session.

        Combines related group IDs and node IDs into a sorted comma-separated string,
        then computes the SHA256 hash for validation or change detection.

        Returns:
            SHA256 hexdigest of sorted related IDs.
        """
        related_ids = (
            self.client.group_context.related_group_ids
            + self.client.group_context.related_node_ids
        )
        sorted_ids = sorted(related_ids)
        joined = ",".join(sorted_ids)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    async def allocate_resource_pools(
        self,
        type: str,
        id: str,
        size: str,
        name_prefix: str,
        fabric_name: Optional[str] = None,
    ) -> None:
        """Allocate resource pools for pod infrastructure."""

        parent_technical_pool = (
            "Technical-IPv4"
            if type == "super-spine"
            else f"{fabric_name}-technical-pool"
        )
        parent_management_pool = (
            "Management-IPv4"
            if type == "super-spine"
            else f"{fabric_name}-management-prefix-pool"
        )

        self.logger.info(f"Allocating {type} resource pools for {name_prefix}")
        fabric_technical_pool = await self.client.get(
            kind=CoreIPPrefixPool, name__value=parent_technical_pool
        )
        fabric_management_pool = await self.client.get(
            kind=CoreIPPrefixPool, name__value=parent_management_pool
        )

        # Use appropriate naming convention based on type
        # For super-spine (DC): use name_prefix only
        # For spine/leaf (pods): use fabric_name-name_prefix
        if type == "super-spine":
            pool_prefix = name_prefix
        else:
            pool_prefix = f"{fabric_name}-{name_prefix}"

        # Create technical prefix pool
        technical_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=fabric_technical_pool,
            identifier=id,
            prefix_length=SIZE_MAPPING[size][type]["technical"],
            data={"role": "technical"},
        )

        technical_pool = await self.client.create(
            kind=CoreIPPrefixPool,
            data={
                "name": f"{pool_prefix}-technical-pool",
                "default_prefix_type": "IpamPrefix",
                "default_prefix_length": 24,
                "ip_namespace": {"hfid": ["default"]},
                "resources": [technical_prefix],
            },
        )
        await technical_pool.save(allow_upsert=True)
        self.logger.debug(
            f"- Created [CoreIPPrefixPool] {technical_pool.display_label}"
        )

        # Create loopback address pool
        loopback_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=technical_pool,
            identifier=id,
            data={"role": "loopback"},
            prefix_length=SIZE_MAPPING[size][type]["loopback"],
        )

        loopback_pool = await self.client.create(
            kind=CoreIPAddressPool,
            data={
                "name": f"{pool_prefix}-loopback-pool",
                "default_address_type": "IpamIPAddress",
                "default_prefix_length": 32,
                "ip_namespace": {"hfid": ["default"]},
                "resources": [loopback_prefix],
            },
        )
        await loopback_pool.save(allow_upsert=True)
        self.logger.debug(
            f"- Created [CoreIPAddressPool] {loopback_pool.display_label}"
        )

        # Create management prefix pool
        management_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=fabric_management_pool,
            identifier=id,
            prefix_length=SIZE_MAPPING[size][type]["management"],
            data={"role": "management"},
        )

        management_prefix_pool = await self.client.create(
            kind=CoreIPPrefixPool,
            data={
                "name": f"{pool_prefix}-management-prefix-pool",
                "default_prefix_type": "IpamPrefix",
                "default_prefix_length": 24,
                "ip_namespace": {"hfid": ["default"]},
                "resources": [management_prefix],
            },
        )
        await management_prefix_pool.save(allow_upsert=True)
        self.logger.debug(
            f"- Created [CoreIPPrefixPool] {management_prefix_pool.display_label}"
        )

        # Create management address pool
        management_addresses = await self.client.allocate_next_ip_prefix(
            resource_pool=management_prefix_pool,
            identifier=id,
            data={"role": "management"},
            prefix_length=28,
        )

        management_pool = await self.client.create(
            kind=CoreIPAddressPool,
            data={
                "name": f"{pool_prefix}-management-pool",
                "default_address_type": "IpamIPAddress",
                "default_prefix_length": SIZE_MAPPING[size][type]["management"],
                "ip_namespace": {"hfid": ["default"]},
                "resources": [management_addresses],
            },
        )
        await management_pool.save(allow_upsert=True)
        self.logger.debug(
            f"- Created [CoreIPAddressPool] {management_pool.display_label}"
        )

        if fabric_name:
            pod = await self.client.get(kind=TopologyPod, id=id)
            # Assign relationship references (one-to-one relationships)
            pod.loopback_pool = loopback_pool  # type: ignore
            pod.management_pool = management_pool  # type: ignore
            pod.prefix_pool = technical_pool  # type: ignore
            await pod.save(allow_upsert=True)

    async def create_devices(
        self,
        type: str,
        amount: int,
        template: dict,
        name_prefix: str,
        deployment_id: str,
        fabric_name: str = "",
        virtual: bool = False,
        naming_config: Optional[DeviceNamingConfig] = None,
    ) -> list[str]:
        """Create devices with loopback and management interfaces.

        Creates physical or virtual devices based on a template, allocates management
        and loopback IP addresses, and configures management interfaces.

        Args:
            type: Device type (super-spine, spine, leaf, etc.).
            amount: Number of devices to create.
            template: Template dictionary with device configuration (id, platform, device_type).
            name_prefix: Prefix for device naming.
            deployment_id: Deployment ID to associate devices with.
            fabric_name: Optional fabric name for pod naming. Defaults to "".
            virtual: If True, creates virtual devices; otherwise creates physical devices. Defaults to False.
            naming_config: Optional DeviceNamingConfig for custom naming strategy. Defaults to standard naming.

        Returns:
            A list of the names of the created devices.
        """
        self.logger.info(f"Creating {amount} {type} devices for {name_prefix}")
        created_device_names = []

        # Use default naming config if not provided
        if naming_config is None:
            naming_config = DeviceNamingConfig()

        # Select device and interface kinds
        if virtual:
            device_kind = DcimVirtualDevice
        else:
            device_kind = DcimPhysicalDevice

        # Determine pool and device naming based on fabric context
        if fabric_name:
            management_pool_name = f"{fabric_name}-{name_prefix}-management-pool"
            loopback_pool_name = f"{fabric_name}-{name_prefix}-loopback-pool"
            device_prefix = f"{fabric_name}-{name_prefix}"
        else:
            management_pool_name = f"{name_prefix}-management-pool"
            loopback_pool_name = f"{name_prefix}-loopback-pool"
            device_prefix = name_prefix

        # Fetch pools once
        management_pool = await self.client.get(
            kind=CoreIPAddressPool,
            name__value=management_pool_name,
        )

        loopback_pool = None
        if type in ["super-spine", "spine", "leaf"]:
            loopback_pool = await self.client.get(
                kind=CoreIPAddressPool,
                name__value=loopback_pool_name,
            )

        # Create devices using native SDK
        for idx in range(1, amount + 1):
            # Use naming config to format device name
            device_name = naming_config.format_device_name(device_prefix, type, idx)
            created_device_names.append(device_name)

            # Allocate management IP
            mgmt_ip = await self.client.allocate_next_ip_address(
                resource_pool=management_pool,
                identifier=device_name,
                prefix_length=32,
                data={"description": f"Management IP for {device_name}"},
            )

            # Create device
            device_data = {
                "name": device_name,
                "object_template": {"id": template["id"]},
                "status": "active",
                "deployment": {"id": deployment_id},
                "primary_address": mgmt_ip,
            }

            # Add platform if available
            if template.get("platform", {}).get("id"):
                device_data["platform"] = template["platform"]

            # Add device_type if available
            if template.get("device_type", {}).get("id"):
                device_data["device_type"] = template["device_type"]

            device = await self.client.create(
                kind=device_kind,
                data=device_data,
            )
            await device.save(allow_upsert=True)
            self.logger.debug(
                f"- Created [{device_kind.__name__}] {device.display_label}"
            )

            # Create loopback interface if applicable
            if type in ["super-spine", "spine", "leaf"] and loopback_pool:
                # Allocate loopback IP
                loop_ip = await self.client.allocate_next_ip_address(
                    resource_pool=loopback_pool,
                    identifier=device_name,
                    prefix_length=32,
                    data={"description": f"Loopback IP for {device_name}"},
                )

                # Create loopback interface
                loopback = await self.client.create(
                    kind=DcimVirtualInterface,
                    data={
                        "name": "Loopback0",
                        "description": "Loopback interface",
                        "device": device,
                        "status": "active",
                        "role": "loopback",
                        "ip_addresses": [loop_ip],
                    },
                )
                await loopback.save(allow_upsert=True)
                self.logger.debug(
                    f"- Created [DcimVirtualInterface] {loopback.display_label}"
                )

        # Configure management interfaces
        # Note: Management interface configuration is skipped as assignment requires
        # accessing the device's primary_address which should be set during device creation
        self.logger.info(
            f"Created {amount} {type} devices with management IPs assigned"
        )
        return created_device_names

    async def create_cabling(
        self,
        bottom_devices: list[str],
        bottom_interfaces: list[str],
        top_devices: list[str],
        top_interfaces: list[str],
        strategy: CablingStrategy = CablingStrategy.POD,
        cabling_offset: Optional[int] = None,
        bottom_sorting: str = "top_down",
        top_sorting: str = "top_down",
        pool: Optional[str] = None,
    ) -> None:
        """Create cabling connections between device layers using the specified strategy.

        Args:
            bottom_devices: List of source device names (in order).
            bottom_interfaces: List of source interface names.
            top_devices: List of target device names (in order).
            top_interfaces: List of target interface names.
            strategy: The cabling strategy to use. Defaults to POD.
            cabling_offset: Optional offset for interface allocation. Required for RACK strategy.
            bottom_sorting: Sorting direction for bottom interfaces: "top_down" or "bottom_up". Defaults to "top_down".
            top_sorting: Sorting direction for top interfaces: "top_down" or "bottom_up". Defaults to "top_down".

        Raises:
            ValueError: If RACK strategy is used without cabling_offset.
        """
        from .helpers import SortingDirection

        self.logger.info(
            f"Creating connections between {len(bottom_devices)} bottom and {len(top_devices)} top devices "
            f"using {strategy.value} strategy (bottom: {bottom_sorting}, top: {top_sorting})"
        )

        # Fetch interfaces in batch
        src_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=bottom_devices,
            name__values=bottom_interfaces,
            cable__isnull=True,
        )
        dst_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=top_devices,
            name__values=top_interfaces,
            cable__isnull=True,
        )

        self.logger.debug(
            f"Fetched {len(src_interfaces)} source interfaces from devices {bottom_devices} "
            f"with names {bottom_interfaces}"
        )
        self.logger.debug(
            f"Fetched {len(dst_interfaces)} destination interfaces from devices {top_devices} "
            f"with names {top_interfaces}"
        )

        # Log details of fetched interfaces
        for src in src_interfaces:
            self.logger.debug(
                f"  Source interface: {src.device.display_label}.{src.name.value} "
                f"(role: {src.role.value if src.role else 'N/A'})"
            )
        for dst in dst_interfaces:
            self.logger.debug(
                f"  Dest interface: {dst.device.display_label}.{dst.name.value} "
                f"(role: {dst.role.value if dst.role else 'N/A'})"
            )

        # Validate strategy requirements
        if cabling_offset is None and strategy == CablingStrategy.RACK:
            raise ValueError("RACK strategy requires cabling_offset parameter")

        # Convert sorting string parameters to enum
        sort_enum_map = {
            "top_down": SortingDirection.TOP_DOWN,
            "bottom_up": SortingDirection.BOTTOM_UP,
        }
        src_sort = sort_enum_map.get(bottom_sorting, SortingDirection.TOP_DOWN)
        dst_sort = sort_enum_map.get(top_sorting, SortingDirection.TOP_DOWN)

        # Log all source and destination devices
        src_devices_in_interfaces = set()
        dst_devices_in_interfaces = set()
        for iface in src_interfaces:
            src_devices_in_interfaces.add(iface.device.display_label)
        for iface in dst_interfaces:
            dst_devices_in_interfaces.add(iface.device.display_label)

        # Build cabling plan with flat interface lists
        cabling_plan = build_cabling_plan(
            index=cabling_offset or 1,
            src_interfaces=src_interfaces,
            dst_interfaces=dst_interfaces,
            strategy=strategy,
            src_sorting=src_sort,
            dst_sorting=dst_sort,
        )

        # Create cables
        self.logger.info(
            f"Cabling plan has {len(cabling_plan)} cable connections to create"
        )
        for cable_pair in cabling_plan:
            self.logger.debug(
                f"  Cable: {cable_pair[0].device.display_label}.{cable_pair[0].name.value} "
                f"↔ {cable_pair[1].device.display_label}.{cable_pair[1].name.value}"
            )

        cable_count = 0
        for src_interface, dst_interface in cabling_plan:
            cable_count += 1
            name = f"{src_interface.device.display_label}-{src_interface.name.value}__{dst_interface.device.display_label}-{dst_interface.name.value}"

            # Create a stable identifier for the p2p link based on interface IDs
            link_identifier = "__".join(sorted([src_interface.id, dst_interface.id]))

            # Create the cable and link it to the interfaces
            network_link = await self.client.create(
                kind=DcimCable,
                data={
                    "name": name,
                    "type": "mmf",
                    "endpoints": [src_interface.id, dst_interface.id],
                },
            )
            await network_link.save(allow_upsert=True)

            # Fetch interfaces to get writable instances
            updated_src_interface = await self.client.get(
                DcimPhysicalInterface, id=src_interface.id
            )
            updated_dst_interface = await self.client.get(
                DcimPhysicalInterface, id=dst_interface.id
            )
            if pool:
                technical_pool = await self.client.get(
                    kind=CoreIPPrefixPool, name__value=pool
                )
                p2p_prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=technical_pool,
                    identifier=link_identifier,  # Use stable ID-based identifier
                    prefix_length=31,
                    member_type="address",
                    data={
                        "role": "technical",
                        "is_pool": True,
                    },
                )
                await p2p_prefix.save(allow_upsert=True)

                # Create a temporary address pool from the p2p prefix
                p2p_address_pool = await self.client.create(
                    kind=CoreIPAddressPool,
                    data={
                        "name": f"p2p-pool-{p2p_prefix.id}",
                        "resources": [p2p_prefix],
                        "ip_namespace": {"hfid": ["default"]},
                        "default_address_type": "IpamIPAddress",
                        "default_prefix_length": 31,
                        "is_pool": True,
                        "member_type": "address",
                    },
                )
                await p2p_address_pool.save(allow_upsert=True)

                updated_src_interface.ip_address = (
                    await self.client.allocate_next_ip_address(
                        p2p_address_pool,
                        identifier=f"{link_identifier}-A",
                        prefix_length=32,
                    )
                )  # type: ignore
                updated_dst_interface.ip_address = (
                    await self.client.allocate_next_ip_address(
                        p2p_address_pool,
                        identifier=f"{link_identifier}-B",
                        prefix_length=32,
                    )
                )  # type: ignore

            updated_src_interface.status.value = "active"
            updated_dst_interface.status.value = name
            updated_src_interface.description.value = name
            updated_dst_interface.description.value = name
            updated_src_interface.status.value = "active"
            updated_dst_interface.status.value = "active"
            await updated_src_interface.save()
            await updated_dst_interface.save()
