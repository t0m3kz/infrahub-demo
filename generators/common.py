from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Type, Union

from infrahub_sdk.exceptions import GraphQLError, ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNode

from .schema_protocols import (
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
)

SIZE_MAPPING: Dict[str, Dict[str, Dict[str, int]]] = {
    "S": {
        "spine": {"technical": 26, "management": 26, "management_prefix": 26},
        "super-spine": {"technical": 24, "management": 24, "management_prefix": 31},
    },
    "M": {
        "spine": {"technical": 25, "management": 25, "management_prefix": 25},
        "super-spine": {"technical": 21, "management": 23, "management_prefix": 30},
    },
    "L": {
        "spine": {"technical": 24, "management": 24, "management_prefix": 24},
        "super-spine": {"technical": 19, "management": 21, "management_prefix": 28},
    },
    "XL": {
        "spine": {"technical": 22, "management": 23, "management_prefix": 23},
        "super-spine": {"technical": 16, "management": 20, "management_prefix": 28},
    },
}


class CommonGenerator(InfrahubGenerator):
    """
    An extended InfrahubGenerator with helper methods for creating objects.
    """

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

    def clean_data(self, data: Dict[str, Any]) -> Any:
        """Recursively clean GraphQL response data.

        Extracts 'value', 'node', or 'edges' from nested dictionaries.
        Returns single-key dict values and first list elements automatically.

        Args:
            data: GraphQL-like response data to clean.

        Returns:
            Cleaned data (dict, list, or single value).

        Raises:
            ValidationError: If result is empty after cleaning.
        """

        def _clean(d: Union[Dict[str, Any], List[Any], Any]) -> Any:
            if isinstance(d, dict):
                if "value" in d:
                    return d["value"]
                if "edges" in d:
                    return _clean(d["edges"])
                if "node" in d:
                    return _clean(d["node"])

                # This handles the top-level 'data' key and other nested objects
                return {key: _clean(value) for key, value in d.items()}

            if isinstance(d, list):
                return [_clean(item) for item in d]

            return d

        # Start cleaning from the top, usually unwrapping the 'data' key
        cleaned = _clean(data)
        if isinstance(cleaned, dict) and "data" in cleaned:
            cleaned = cleaned["data"]

        # If the result is a dictionary with only one key, return its value.
        if isinstance(cleaned, dict) and len(cleaned) == 1:
            cleaned = next(iter(cleaned.values()))

        # If the result is a list, return the first element.
        if isinstance(cleaned, list) and cleaned:
            return cleaned[0]

        is_empty = (
            cleaned is None
            or cleaned == ""
            or (isinstance(cleaned, dict) and not cleaned)
            or (isinstance(cleaned, list) and not cleaned)
        )

        if is_empty:
            raise ValidationError(
                "clean_data returned empty result after processing input data"
            )

        return cleaned

    async def create_in_batch(
        self,
        kind: Type[CoreNode],
        data_list: list,
    ) -> None:
        """Create multiple objects of a specific kind in batch.

        Creates objects in batch mode for efficiency and stores them in the local store.

        Args:
            kind: The kind of object to create (e.g., DcimPhysicalDevice).
            data_list: List of data dictionaries containing 'payload' and optional 'store_key'.
        """
        batch = await self.client.create_batch()
        for data in data_list:
            try:
                obj: CoreNode = await self.client.create(
                    kind=kind, data=data.get("payload"), branch=self.branch
                )
                batch.add(task=obj.save, allow_upsert=True, node=obj)
                if data.get("store_key"):
                    self.client.store.set(
                        key=data.get("store_key"), node=obj, branch=self.branch
                    )
            except GraphQLError as exc:
                self.logger.debug(f"- Creation failed due to {exc}")
        try:
            async for node, _ in batch.execute():
                object_reference = (
                    " ".join(node.hfid) if node.hfid else node.display_label
                )
                self.logger.info(
                    f"- Created [{node.get_kind()}] {object_reference}"
                    if object_reference
                    else f"- Created [{node.get_kind()}]"
                )
        except ValidationError as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def create(self, kind: Type[CoreNode], data: dict) -> None:
        """Create a single object of a specific kind.

        Creates an individual object and stores it in the local store with optional key.

        Args:
            kind: The kind of object to create.
            data: Dictionary containing 'payload' and optional 'store_key'.

        Raises:
            GraphQLError: If object creation fails in GraphQL.
            ValidationError: If object validation fails.
        """
        try:
            obj = await self.client.create(
                kind=kind, data=data.get("payload"), branch=self.branch
            )
            await obj.save(allow_upsert=True)
            object_reference = " ".join(obj.hfid) if obj.hfid else obj.display_label
            self.logger.info(
                f"- Created [{kind.__name__}] {object_reference}"
                if object_reference
                else f"- Created [{kind.__name__}]"
            )
            if data.get("store_key"):
                self.client.store.set(
                    key=data.get("store_key"), node=obj, branch=self.branch
                )
        except (GraphQLError, ValidationError) as exc:
            self.logger.error(f"- Creation failed due to {exc}")

    async def allocate_resource_pools(
        self,
        type: str,
        id: str,
        size: str,
        name_prefix: str,
        fabric_name: Optional[str] = None,
    ) -> None:
        """Allocate resource pools for pod infrastructure.

        Creates separate technical, management, loopback prefix and address pools
        for each pod using fabric_name-pod_name naming convention.

        Args:
            type: Node type (spine, super-spine) determining pool hierarchy.
            id: Unique identifier for the allocation tracking.
            size: Size identifier (S, M, L, XL) for prefix length configuration.
            name_prefix: Pod name prefix for naming allocated pools.
            fabric_name: Name of the fabric for parent pool reference. Optional.

        Raises:
            GraphQLError: If pool retrieval or allocation fails.
        """

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
        await self.create(
            kind=CoreIPPrefixPool,
            data={
                "payload": {
                    "name": f"{pool_prefix}-technical-pool",
                    "default_prefix_type": "IpamPrefix",
                    "default_prefix_length": 24,
                    "ip_namespace": {"hfid": ["default"]},
                    "resources": [
                        await self.client.allocate_next_ip_prefix(
                            resource_pool=fabric_technical_pool,
                            identifier=id,
                            prefix_length=SIZE_MAPPING[size][type]["technical"],
                            data={"role": "technical"},
                        )
                    ],
                },
                "store_key": f"{pool_prefix}-technical-pool",
            },
        )

        # Create loopback address pool
        await self.create(
            kind=CoreIPAddressPool,
            data={
                "payload": {
                    "name": f"{pool_prefix}-loopback-pool",
                    "default_address_type": "IpamIPAddress",
                    "default_prefix_length": 32,
                    "ip_namespace": {"hfid": ["default"]},
                    "resources": [
                        await self.client.allocate_next_ip_prefix(
                            resource_pool=self.client.store.get(
                                kind=CoreIPPrefixPool,
                                key=f"{pool_prefix}-technical-pool",
                            ),
                            identifier=id,
                            data={"role": "loopback"},
                            prefix_length=28,
                        )
                    ],
                },
                "store_key": f"{pool_prefix}-loopback-pool",
            },
        )

        # Create management prefix pool
        await self.create(
            kind=CoreIPPrefixPool,
            data={
                "payload": {
                    "name": f"{pool_prefix}-management-prefix-pool",
                    "default_prefix_type": "IpamPrefix",
                    "default_prefix_length": 24,
                    "ip_namespace": {"hfid": ["default"]},
                    "resources": [
                        await self.client.allocate_next_ip_prefix(
                            resource_pool=fabric_management_pool,
                            identifier=id,
                            prefix_length=SIZE_MAPPING[size][type]["management"],
                            data={"role": "management"},
                        )
                    ],
                },
                "store_key": f"{pool_prefix}-management-prefix-pool",
            },
        )

        # Create management address pool
        await self.create(
            kind=CoreIPAddressPool,
            data={
                "payload": {
                    "name": f"{pool_prefix}-management-pool",
                    "default_address_type": "IpamIPAddress",
                    "default_prefix_length": SIZE_MAPPING[size][type]["management"],
                    "ip_namespace": {"hfid": ["default"]},
                    "resources": [
                        await self.client.allocate_next_ip_prefix(
                            resource_pool=self.client.store.get(
                                kind=CoreIPPrefixPool,
                                key=f"{pool_prefix}-management-prefix-pool",
                            ),
                            identifier=id,
                            data={"role": "management"},
                            prefix_length=28,
                        )
                    ],
                },
                "store_key": f"{pool_prefix}-management-pool",
            },
        )

    async def create_devices(
        self,
        type: str,
        amount: int,
        template: dict,
        prefix_name: str,
        deployment_id: str,
        fabric_name: str = "",
        virtual: bool = False,
    ) -> None:
        """Create devices with loopback and management interfaces.

        Creates physical or virtual devices based on a template, allocates management
        and loopback IP addresses, and configures management interfaces.

        Args:
            type: Device type (super-spine, spine, leaf, etc.).
            amount: Number of devices to create.
            template: Template dictionary with device configuration (id, platform, device_type).
            prefix_name: Prefix for device naming.
            deployment_id: Deployment ID to associate devices with.
            virtual: If True, creates virtual devices; otherwise creates physical devices. Defaults to False.

        Raises:
            GraphQLError: If device or interface creation fails.
        """
        self.logger.info(f"Creating super spine switches for fabric {prefix_name}")
        # Create devices
        if virtual:
            device_kind = DcimVirtualDevice
            management_kind = DcimVirtualInterface
        else:
            device_kind = DcimPhysicalDevice
            management_kind = DcimPhysicalInterface

        # Use appropriate pool naming based on fabric_name presence
        # For DC (no fabric_name): use prefix_name only
        # For pods (fabric_name provided): use fabric_name-prefix_name
        if fabric_name:
            management_pool_key = f"{fabric_name}-{prefix_name}-management-pool"
            loopback_pool_key = f"{fabric_name}-{prefix_name}-loopback-pool"
        else:
            management_pool_key = f"{prefix_name}-management-pool"
            loopback_pool_key = f"{prefix_name}-loopback-pool"

        await self.create_in_batch(
            kind=device_kind,
            data_list=[
                {
                    "payload": {
                        "name": f"{prefix_name}-{type}-{idx:02d}",
                        "object_template": {"id": template.get("id", None)},
                        "status": "active",
                        "deployment": {"id": deployment_id},
                        "platform": {
                            "id": template.get("platform", {}).get("id", None)
                        },
                        "device_type": {
                            "id": template.get("device_type", {}).get("id", None)
                        },
                        "primary_address": await self.client.allocate_next_ip_address(
                            resource_pool=self.client.store.get(
                                kind=CoreIPAddressPool,
                                key=management_pool_key,
                            ),
                            identifier=f"{prefix_name}-{type}-{idx:02d}",
                            prefix_length=32,
                            data={
                                "description": f"Management IP for {prefix_name}-{type}-{idx:02d}"
                            },
                        ),
                    },
                    "store_key": f"{prefix_name}-{type}-{idx:02d}",
                }
                for idx in range(1, amount + 1)
            ],
        )

        # Create loopback interfaces
        await self.create_in_batch(
            kind=DcimVirtualInterface,
            data_list=[
                {
                    "payload": {
                        "name": "Loopback0",
                        "description": "Loopback interface",
                        "device": self.client.store.get(
                            kind=device_kind,
                            key=f"{prefix_name}-{type}-{idx:02d}",
                        ),
                        "status": "active",
                        "role": "loopback",
                        "ip_addresses": [
                            await self.client.allocate_next_ip_address(
                                resource_pool=self.client.store.get(
                                    kind=CoreIPAddressPool,
                                    key=loopback_pool_key,
                                ),
                                identifier=f"{prefix_name}-{type}-{idx:02d}",
                                prefix_length=32,
                                data={
                                    "description": f"Loopback IP for {prefix_name}-{type}-{idx:02d}"
                                },
                            )
                        ],
                    },
                }
                for idx in range(1, amount + 1)
                if type in ["super-spine", "spine", "leaf"]
            ],
        )

        # Configure management interfaces
        interfaces = await self.client.filters(
            kind=management_kind,
            device__name__values=[
                f"{prefix_name}-{type}-{idx:02d}" for idx in range(1, amount + 1)
            ],
            role__value="management",
        )
        self.logger.info(f"Configuring {len(interfaces)} management interfaces")
        for interface in interfaces:
            device = self.client.store.get(
                kind=device_kind,
                key=f"{interface.device.id}",
            )
            interface.status.value = "active"
            interface.description.value = (
                f"Management interface for {interface.device.display_label}"
            )
            interface.ip_addresses.add(f"{device.primary_address.id}")
            await interface.save(allow_upsert=True)
            self.logger.info(
                (
                    f" - Configured {interface.device.display_label} {interface.display_label}"
                    f" with IP address {device.primary_address.display_label}"
                )
            )
