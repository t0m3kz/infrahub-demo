from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal, Optional, TypeVar

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import (
    CoreIPAddressPool,
    CoreIPPrefixPool,
    CoreStandardGroup,
)
from pydantic import BaseModel

from .helpers import CablingPlanner, DeviceNamingConfig

if TYPE_CHECKING:
    pass

from .schema_protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    IpamIPAddress,
    TopologyPod,
)

T = TypeVar("T", bound=BaseModel)


class CommonGenerator(InfrahubGenerator):
    """
    An extended InfrahubGenerator with helper methods for creating objects.
    """

    def clean_data(self, data: dict) -> Any:
        """
        Recursively transforms the input data by extracting 'value', 'node', or 'edges' from dictionaries.

        This is a wrapper around the shared utils.data_cleaning.clean_data function
        to maintain compatibility with existing generator code.

        Args:
            data: The input data to clean.

        Returns:
            The cleaned data with extracted values.
        """
        from utils.data_cleaning import clean_data as _clean_data

        return _clean_data(data)

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

    async def get_devices_with_available_ports(
        self,
        device_names: list[str],
        interface_role: str = "leaf",
        top_n: int = 2,
    ) -> tuple[list[str], int]:
        """Get devices with most available ports using GraphQL.

        Args:
            device_names: List of device names to check
            interface_role: Role of interfaces to check (default: "leaf")
            top_n: Number of top devices to return (default: 2)

        Returns:
            Tuple of (selected_device_names, total_available_ports)
        """
        from typing import TypedDict

        class DeviceAvailability(TypedDict):
            device: str
            available_ports: int

        device_availability: list[DeviceAvailability] = []

        for device_name in device_names:
            # Use GraphQL to efficiently count available ports
            query = """
            query GetDeviceAvailablePorts($device_name: String!, $role: String!) {
                DcimGenericDevice(name__value: $device_name) {
                    edges {
                        node {
                            id
                            name { value }
                            interfaces(role__value: $role) {
                                count
                                edges {
                                    node {
                                        id
                                        name { value }
                                        ... on DcimPhysicalInterface {
                                            cable {
                                                node { id }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            """

            result = await self.client.execute_graphql(
                query=query,
                variables={"device_name": device_name, "role": interface_role},
                branch_name=self.branch,
            )

            device_data = result.get("DcimGenericDevice", {}).get("edges", [])
            if not device_data:
                self.logger.warning(
                    f"Device {device_name} not found in GraphQL response"
                )
                continue

            interfaces_data = device_data[0].get("node", {}).get("interfaces", {})
            total_ports = interfaces_data.get("count", 0)

            # Count interfaces with cables
            connected_count = 0
            for iface_edge in interfaces_data.get("edges", []):
                iface = iface_edge.get("node", {})
                if iface.get("cable", {}).get("node"):
                    connected_count += 1

            available_ports = total_ports - connected_count

            self.logger.debug(
                f"Device {device_name}: {total_ports} total ports, "
                f"{connected_count} connected, {available_ports} available"
            )

            device_availability.append(
                {
                    "device": device_name,
                    "available_ports": available_ports,
                }
            )

        # Sort by available ports (descending) and select top N
        device_availability.sort(key=lambda x: x["available_ports"], reverse=True)
        selected_devices: list[str] = [
            item["device"] for item in device_availability[:top_n]
        ]

        # Calculate total available ports in selected devices
        total_available = sum(
            item["available_ports"] for item in device_availability[:top_n]
        )

        return selected_devices, total_available

    async def allocate_resource_pools(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        fabric_name: str,
        pod_name: Optional[str] = None,
        ipv6: Optional[bool] = False,
    ) -> None:
        """Ensure required per-pod / fabric pools exist.

        Notes:
        - This function ensures minimal placeholder pools exist (side-effect).
        - It accepts a simple strategy name string for `pools` (e.g. "fabric") and
          normalizes it to a FabricPoolConfig internally for deterministic behavior.
        - It intentionally returns None: creation is a side-effect. Actual address/prefix
          allocation is performed later by generators which will fetch pools by name.
        """
        # Local import to avoid runtime cycles during type-checking
        from .helpers import FabricPoolConfig

        self.logger.info("Implementing resource pools")

        pool_prefix = f"{pod_name}" if pod_name else fabric_name

        # Create a new dictionary with only the keys that FabricPoolConfig expects
        valid_keys = [
            "maximum_super_spines",
            "maximum_pods",
            "maximum_spines",
            "maximum_leafs",
            "maximum_tors",
        ]
        filtered_pools = {key: pools[key] for key in valid_keys if key in pools}
        pod = await self.client.get(kind=TopologyPod, id=id) if pod_name else None
        pools_config = FabricPoolConfig(
            **filtered_pools, kind=strategy, ipv6=ipv6 or False
        )
        for pool_name, pool_size in pools_config.pools().items():
            if strategy == "fabric" and pool_name in [
                "management",
                "technical",
                "loopback",
            ]:
                parent_pool_name = (
                    f"{pool_name.capitalize()}-IPv4"
                    if not ipv6 or pool_name == "management"
                    else f"{pool_name.capitalize()}-IPv6"
                )
            elif strategy == "fabric" and not pod_name:
                parent_pool_name = f"{fabric_name}-{pool_name.split('-')[-1]}-pool"
            else:
                parent_pool_name = f"{fabric_name}-{pool_name}-pool"

            parent_pool = await self.client.get(
                kind=CoreIPPrefixPool,
                name__value=parent_pool_name,
            )
            self.logger.info(
                f"Allocating next IP prefix for pool '{pool_name}' (/{pool_size}) in parent '{parent_pool_name}'"
            )
            allocated_prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=parent_pool,
                identifier=id,
                prefix_length=pool_size,
                data={
                    "role": f"{pool_name if pool_name in ['management', 'technical', 'loopback'] else pool_name.split('-')[-1]}"
                },
            )

            pool_full_name = f"{pool_prefix}-{pool_name}-pool"

            # Determine if this is a prefix or address pool
            is_prefix_pool = (
                strategy == "fabric" and pool_name in ["technical", "loopback"]
            ) or (strategy == "pod" and pool_name == "technical")

            if is_prefix_pool:
                new_pool = await self.client.create(
                    kind=CoreIPPrefixPool,
                    data={
                        "name": pool_full_name,
                        "default_prefix_type": "IpamPrefix",
                        "default_prefix_length": pool_size,
                        "ip_namespace": {"hfid": ["default"]},
                        "identifier": id,
                        "resources": [allocated_prefix],
                    },
                )
            else:
                new_pool = await self.client.create(
                    kind=CoreIPAddressPool,
                    data={
                        "name": pool_full_name,
                        "default_address_type": "IpamIPAddress",
                        "default_prefix_length": pool_size,
                        "ip_namespace": {"hfid": ["default"]},
                        "identifier": id,
                        "resources": [allocated_prefix],
                    },
                )

            await new_pool.save(allow_upsert=True)

            # Map pool names to pod attributes
            pool_kind = "CoreIPPrefixPool" if is_prefix_pool else "CoreIPAddressPool"
            self.logger.info(f"- Created [{pool_kind}] {new_pool.hfid}")
            # Update Pods
            pool_attribute_map = {
                "loopback": "loopback_pool",
                "technical": "prefix_pool",
            }

            if pod and pool_name in pool_attribute_map:
                setattr(pod, pool_attribute_map[pool_name], new_pool.id)
                self.logger.info(
                    f"- Updated pod {pod.name.value} with {new_pool.name.value} pool"
                )
                await pod.save(allow_upsert=True)

    async def create_devices(
        self,
        device_role: str,
        amount: int,
        deployment_id: str,
        template: dict[str, Any],
        naming_convention: Literal["standard", "hierarchical", "flat"] = "flat",
        options: Optional[dict] = None,
    ) -> list[str]:
        """Create devices using a consolidated options dict and batch creation."""
        # Normalize options
        options = options or {}
        pod_name: str = options.get("pod_name", "")
        fabric_name: str = options.get("fabric_name", "")
        virtual: bool = bool(options.get("virtual", False))
        indexes: Optional[list[int]] = options.get("indexes", None)
        allocate_loopback: bool = bool(options.get("allocate_loopback", False))
        rack: str = options.get("rack", "")

        device_prefix: str = fabric_name if not pod_name else pod_name

        device_names: list[str] = sorted(
            [
                DeviceNamingConfig(strategy=naming_convention).format_device_name(
                    fabric_name,
                    device_role,
                    index=idx,
                    fabric_name=fabric_name,
                    indexes=indexes,
                )
                for idx in range(1, amount + 1)
            ]
        )
        management_pool_name = f"{fabric_name}-management-pool"

        if device_role == "super-spine":
            # Super-spine devices use fabric-level super-spine-loopback pool
            loopback_pool_name = f"{fabric_name}-{device_role}-loopback-pool"
        else:
            # Other devices (spine, leaf, etc.) use pod-level loopback pool
            # device_prefix already includes fabric-pod combination when pod_name is present
            loopback_pool_name = f"{device_prefix}-loopback-pool"

        device_kind = DcimVirtualDevice if virtual else DcimPhysicalDevice

        # # Fetch pools once
        management_pool = await self.client.get(
            kind=CoreIPAddressPool,
            name__value=management_pool_name,
        )

        loopback_pool = None
        if allocate_loopback:
            loopback_pool = await self.client.get(
                kind=CoreIPAddressPool,
                name__value=loopback_pool_name,
            )

        batch_devices = await self.client.create_batch()
        batch_loopbacks = await self.client.create_batch()

        device_group = await self.client.get(
            kind=CoreStandardGroup, name__value=f"{device_role}s"
        )
        try:
            # Add device objects and related loopback interfaces (if any) to the batch
            for name in device_names:
                obj = await self.client.create(
                    kind=device_kind,
                    data={
                        "name": name,
                        "object_template": {
                            "id": template.get("id") if template else None
                        },
                        "status": "active",
                        "deployment": {"id": deployment_id} if deployment_id else None,
                        "device_type": template.get("device_type"),
                        "platform": template.get("platform"),
                        "primary_address": await self.client.allocate_next_ip_address(
                            resource_pool=management_pool,
                            identifier=name,
                            prefix_length=32,
                            data={"description": f"Management IP for {name}"},
                        ),
                        "rack": {"id": rack} if rack else None,
                        "member_of_groups": [device_group.id],
                    },
                    branch=self.branch,
                )
                batch_devices.add(task=obj.save, allow_upsert=True, node=obj)

                if loopback_pool:
                    obj = await self.client.create(
                        kind=DcimVirtualInterface,
                        data={
                            "name": "Loopback0",
                            "description": "Loopback interface",
                            # refer to device by unique name; server should resolve references on apply
                            "device": {"hfid": name},
                            "status": "active",
                            "role": "loopback",
                            "ip_addresses": [
                                await self.client.allocate_next_ip_address(
                                    resource_pool=loopback_pool,
                                    identifier=name,
                                    prefix_length=32,
                                    data={"description": f"Loopback IP for {name}"},
                                )
                            ],
                        },
                        branch=self.branch,
                    )
                    batch_loopbacks.add(task=obj.save, allow_upsert=True, node=obj)

            # Execute batch and collect created nodes
            async for node, _ in batch_devices.execute():
                self.logger.info(f"- Created [{node.get_kind()}] {node.hfid}")
            async for node, _ in batch_loopbacks.execute():
                self.logger.info(
                    f"- Created [{node.get_kind()}] {node.device.hfid} {node.name.value}"
                )
        except ValidationError as exc:
            self.logger.error("Batch creation failed with validation error: %s", exc)
            raise
        except Exception as exc:
            self.logger.error("Unexpected error during batch creation: %s", exc)
            raise
        return device_names

    async def create_cabling(
        self,
        bottom_devices: list[str],
        bottom_interfaces: list[str],
        top_devices: list[str],
        top_interfaces: list[str],
        strategy: Literal[
            "pod", "rack", "hierarchical_rack", "intra_rack", "custom"
        ] = "rack",
        options: Optional[dict[str, Any]] = None,
    ) -> None:
        """Create cabling connections between device layers with enhanced hierarchical support."""

        options = options or {}
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up"
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up"
        cabling_offset: int = int(options.get("cabling_offset", 0))
        pool: Any = options.get("pool")

        self.logger.info(
            f"Creating cabling between {len(bottom_devices)} bottom and {len(top_devices)} top devices "
            f"(strategy: {strategy}, bottom: {bottom_sorting}, top: {top_sorting})"
        )

        # Fetch interfaces in batch, ensuring the related device data is included
        src_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=bottom_devices,
            name__values=bottom_interfaces,
            # cable__isnull=True,
        )
        dst_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=top_devices,
            name__values=top_interfaces,
            # cable__isnull=True,
        )

        if not src_interfaces or not dst_interfaces:
            self.logger.warning("No available interfaces found; skipping cabling")
            return

        planner = CablingPlanner(
            bottom_interfaces=src_interfaces,
            top_interfaces=dst_interfaces,
            bottom_sorting=bottom_sorting,
            top_sorting=top_sorting,
        )

        # Build cabling plan based on strategy
        cabling_plan = planner.build_cabling_plan(
            scenario=strategy,
            cabling_offset=cabling_offset,
        )

        if not cabling_plan:
            self.logger.warning(
                "No cabling connections planned; skipping cable creation"
            )
            return

        for src_interface, dst_interface in cabling_plan:
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
                self.logger.info(
                    f"- Allocated prefix {p2p_prefix.display_label} for {name}"
                )

                # Create a temporary address pool from the p2p prefix
                host_addresses = p2p_prefix.prefix.value.hosts()  # type: ignore

                src_ip = await self.client.create(
                    kind=IpamIPAddress,
                    address=str(next(host_addresses)) + "/31",
                )
                await src_ip.save(allow_upsert=True)
                updated_src_interface.ip_address = src_ip.id  # type: ignore

                dst_ip = await self.client.create(
                    kind=IpamIPAddress,
                    address=str(next(host_addresses)) + "/31",
                )
                await dst_ip.save(allow_upsert=True)
                updated_dst_interface.ip_address = dst_ip.id  # type: ignore

            updated_src_interface.description.value = name
            updated_dst_interface.description.value = name
            updated_src_interface.status.value = "active"
            updated_dst_interface.status.value = "active"
            await updated_src_interface.save()
            await updated_dst_interface.save()
            self.logger.info(f"- Created connection {name}")
