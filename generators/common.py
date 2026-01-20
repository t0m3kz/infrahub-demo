from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal, Optional, TypeVar

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreStandardGroup
from pydantic import BaseModel

from .helpers import CablingPlanner, DeviceNamingConfig

if TYPE_CHECKING:
    pass

from .protocols import (
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

    Instance variables set during generate() lifecycle:
        deployment_id: Root deployment (DC/POP) ID for linking cables (required)
        fabric_name: Fabric/DC name (lowercase) for pool and device naming (required)
        pod_name: Pod name (lowercase) for pool naming (optional, only in pod/rack generators)
    """

    # Instance variables - must be set in generate() before calling helper methods
    deployment_id: str = ""  # Required: set to DC/POP ID
    fabric_name: str = ""  # Required: set to fabric/DC name
    pod_name: Optional[str] = None  # Optional: only for pod/rack generators

    def calculate_checksum(self) -> str:
        """Calculate a SHA256 checksum based on configuration data.

        Creates a deterministic checksum from the configuration that will be
        used to generate infrastructure. This ensures the same configuration
        always produces the same checksum, regardless of what was created.

        Args:
            data: Configuration data dictionary (e.g., design pattern, capacities)

        Returns:
            SHA256 hexdigest of the configuration data.
        """
        # import json

        related_ids = self.client.group_context.related_group_ids + self.client.group_context.related_node_ids
        sorted_ids = sorted(related_ids)
        joined = ",".join(sorted_ids)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    async def allocate_resource_pools(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        ipv6: Optional[bool] = False,
    ) -> None:
        """Ensure required per-pod / fabric pools exist.

        Notes:
        - This function ensures minimal placeholder pools exist (side-effect).
        - It accepts a simple strategy name string for `pools` (e.g. "fabric") and
          normalizes it to a FabricPoolConfig internally for deterministic behavior.
        - It intentionally returns None: creation is a side-effect. Actual address/prefix
          allocation is performed later by generators which will fetch pools by name.
        - Uses self.fabric_name and self.pod_name (if set) from instance variables.
        """
        # Local import to avoid runtime cycles during type-checking
        from .helpers import FabricPoolConfig

        self.logger.info("Implementing resource pools")

        fabric_name = self.fabric_name
        pod_name = self.pod_name
        pool_prefix = pod_name if pod_name else fabric_name

        # Create a new dictionary with only the keys that FabricPoolConfig expects
        valid_keys = [
            "maximum_super_spines",
            "maximum_pods",
            "maximum_spines",
            "maximum_switches",
        ]
        filtered_pools = {key: pools[key] for key in valid_keys if key in pools}
        pod = await self.client.get(kind=TopologyPod, id=id) if pod_name else None
        pools_config = FabricPoolConfig(**filtered_pools, kind=strategy, ipv6=ipv6 or False)
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
            pool_full_name = f"{pool_prefix}-{pool_name}-pool"

            allocated_prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=parent_pool,
                identifier=pool_full_name,
                prefix_length=pool_size,
                data={
                    "role": f"{pool_name if pool_name in ['management', 'technical', 'loopback'] else pool_name.split('-')[-1]}",
                    "identifier": pool_full_name,
                },
            )

            # Determine if this is a prefix or address pool
            is_prefix_pool = (strategy == "fabric" and pool_name in ["technical", "loopback"]) or (
                strategy == "pod" and pool_name == "technical"
            )

            if is_prefix_pool:
                new_pool = await self.client.create(
                    kind=CoreIPPrefixPool,
                    data={
                        "name": pool_full_name,
                        "default_prefix_type": "IpamPrefix",
                        "default_prefix_length": pool_size,
                        "ip_namespace": {"hfid": ["default"]},
                        "identifier": pool_full_name,
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
                        "identifier": pool_full_name,
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
                # Attach using explicit node reference to avoid merge-time drops on CoreIPAddressPool links
                pool_ref = {"id": new_pool.id, "hfid": [new_pool.hfid]}
                setattr(pod, pool_attribute_map[pool_name], pool_ref)
                self.logger.info(f"- Updated pod {pod.name.value} with pool {pool_full_name} (id: {new_pool.id})")
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
        """Create devices using a consolidated options dict and batch creation.

        Uses self.fabric_name and self.pod_name (if set) from instance variables.
        """
        # Normalize options
        options = options or {}
        fabric_name = self.fabric_name
        pod_name = self.pod_name or ""
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

        device_group = await self.client.get(kind=CoreStandardGroup, name__value=f"{device_role}s")
        try:
            # Fetch all existing devices in a single batch to optimize performance
            existing_devices_list = await self.client.filters(
                kind=device_kind,
                name__values=device_names,
                include=["member_of_groups"],
            )
            existing_devices_map = {device.name.value: device for device in existing_devices_list}

            # Add device objects and related loopback interfaces (if any) to the batch
            for name in device_names:
                existing_device = existing_devices_map.get(name)
                if existing_device:
                    groups = [peer.id for peer in existing_device.member_of_groups.peers]
                else:
                    groups = []

                # Ensure the new group is not duplicated
                if device_group.id not in groups:
                    groups.append(device_group.id)

                obj = await self.client.create(
                    kind=device_kind,
                    data={
                        "name": name,
                        "object_template": {"id": template.get("id") if template else None},
                        "status": "active",
                        "role": device_role,
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
                        "member_of_groups": [{"id": group_id} for group_id in groups],
                    },
                )
                batch_devices.add(task=obj.save, allow_upsert=True, node=obj)

                if loopback_pool:
                    loopback_obj = await self.client.create(
                        kind=DcimVirtualInterface,
                        data={
                            "name": "Loopback0",
                            "description": "Loopback interface",
                            # Reference device object directly
                            "device": obj,
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
                    )
                    batch_loopbacks.add(task=loopback_obj.save, allow_upsert=True, node=loopback_obj)

            # Execute batch and collect created nodes
            created_devices = []
            created_loopbacks = []
            
            async for node, _ in batch_devices.execute():
                created_devices.append(node)
                self.logger.info(f"  - Created [{node.get_kind()}] {node.hfid}")
            
            async for node, _ in batch_loopbacks.execute():
                created_loopbacks.append(node)
                self.logger.info(f"  - Created [{node.get_kind()}] {node.device.hfid} {node.name.value}")
            
            # Summary logging
            self.logger.info(
                f"Device creation completed: {len(created_devices)} {device_role}(s) created"
                + (f" with {len(created_loopbacks)} loopback interface(s)" if created_loopbacks else "")
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
            "pod",
            "rack",
            "intra_rack",
            "intra_rack_middle",
            "intra_rack_mixed",
        ] = "rack",
        options: Optional[dict[str, Any]] = None,
    ) -> None:
        """Create cabling connections between device layers (simple per-link flow)."""

        options = options or {}
        cabling_offset: int = int(options.get("cabling_offset", 0))
        pool: Any = options.get("pool") or (f"{self.pod_name}-technical-pool" if self.pod_name else None)

        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up"
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up"

        self.logger.info(
            f"Initiating cabling: {len(bottom_devices)} bottom device(s) → {len(top_devices)} top device(s) "
            f"[strategy={strategy}, offset={cabling_offset}, pool={pool or 'none'}]"
        )

        import asyncio

        await asyncio.sleep(2.0)

        src_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=bottom_devices,
            name__values=bottom_interfaces,
        )

        dst_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=top_devices,
            name__values=top_interfaces,
        )

        if not src_interfaces or not dst_interfaces:
            self.logger.error(
                f"CABLING ABORTED - Insufficient interfaces available: "
                f"Bottom interfaces: {len(src_interfaces) if src_interfaces else 0}/{len(bottom_devices) * len(bottom_interfaces)}, "
                f"Top interfaces: {len(dst_interfaces) if dst_interfaces else 0}/{len(top_devices) * len(top_interfaces)}. "
                f"Check device templates and interface roles."
            )
            return

        planner = CablingPlanner(
            bottom_interfaces=src_interfaces,
            top_interfaces=dst_interfaces,
            bottom_sorting=bottom_sorting,
            top_sorting=top_sorting,
        )

        cabling_plan = planner.build_cabling_plan(
            scenario=strategy,
            cabling_offset=cabling_offset,
        )
        
        planned_connections = len(cabling_plan)
        if planned_connections == 0:
            self.logger.warning(
                "Cabling plan produced 0 connections - no cables will be created. "
                "Check interface availability and compatibility."
            )
            return
        
        self.logger.info(f"Cabling plan generated: {planned_connections} connection(s) planned")

        if not cabling_plan:
            self.logger.warning("No cabling connections planned; skipping cable creation")
            return

        for src_interface, dst_interface in cabling_plan:
            endpoint_names = sorted(
                [
                    f"{src_interface.device.display_label}-{src_interface.name.value}",
                    f"{dst_interface.device.display_label}-{dst_interface.name.value}",
                ]
            )
            name = "__".join(endpoint_names)
            link_identifier = "__".join(sorted([src_interface.id, dst_interface.id]))

            network_link = await self.client.create(
                kind=DcimCable,
                data={
                    "name": name,
                    "type": "mmf",
                    "endpoints": [src_interface.id, dst_interface.id],
                    # Attach cables to the deployment so inventory is scoped correctly
                    "deployment": {"id": self.deployment_id} if self.deployment_id else None,
                },
            )
            await network_link.save(allow_upsert=True)

            updated_src = await self.client.get(DcimPhysicalInterface, id=src_interface.id)
            updated_dst = await self.client.get(DcimPhysicalInterface, id=dst_interface.id)

            if pool:
                technical_pool = await self.client.get(kind=CoreIPPrefixPool, name__value=pool)
                p2p_prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=technical_pool,
                    identifier=link_identifier,
                    prefix_length=31,
                    member_type="address",
                    data={"role": "technical", "is_pool": True},
                )
                self.logger.info(f"- Allocated prefix {p2p_prefix.display_label} for {name}")

                host_addresses = p2p_prefix.prefix.value.hosts()  # type: ignore

                src_ip = await self.client.create(kind=IpamIPAddress, address=str(next(host_addresses)) + "/31")
                await src_ip.save(allow_upsert=True)
                updated_src.ip_address = src_ip.id  # type: ignore

                dst_ip = await self.client.create(kind=IpamIPAddress, address=str(next(host_addresses)) + "/31")
                await dst_ip.save(allow_upsert=True)
                updated_dst.ip_address = dst_ip.id  # type: ignore

            updated_src.description.value = name
            updated_dst.description.value = name
            updated_src.status.value = "active"
            updated_dst.status.value = "active"
            await updated_src.save()
            await updated_dst.save()
            self.logger.info(f"  - Created connection {name}")
        
        # Summary logging
        self.logger.info(
            f"Cabling completed: {len(cabling_plan)} connection(s) established successfully "
            f"[{len(bottom_devices)} bottom ↔ {len(top_devices)} top devices]"
        )
