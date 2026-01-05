from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal, Optional, TypeVar, cast

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreStandardGroup
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

    Instance variables set during generate() lifecycle:
        deployment_id: Root deployment (DC/POP) ID for linking cables (required)
        fabric_name: Fabric/DC name (lowercase) for pool and device naming (required)
        pod_name: Pod name (lowercase) for pool naming (optional, only in pod/rack generators)
    """

    # Instance variables - must be set in generate() before calling helper methods
    deployment_id: str = ""  # Required: set to DC/POP ID
    fabric_name: str = ""  # Required: set to fabric/DC name
    pod_name: Optional[str] = None  # Optional: only for pod/rack generators

    async def _get_default_branch(self) -> str:
        """Best-effort discovery of the default branch name.

        Falls back to "main" if the SDK does not expose a helper or the call fails.
        """

        branch_manager = getattr(self.client, "branch", None)
        if branch_manager:
            get_default = getattr(branch_manager, "get_default", None)
            if callable(get_default):
                try:
                    return await get_default()
                except Exception:
                    pass

            default_attr = getattr(branch_manager, "default", None)
            if isinstance(default_attr, str) and default_attr:
                return default_attr

        return "main"

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
        """Ensure required per-pod / fabric pools exist on the default branch."""

        from .helpers import FabricPoolConfig

        self.logger.info("Implementing resource pools")

        fabric_name = self.fabric_name
        pod_name = self.pod_name
        pool_prefix = pod_name if pod_name else fabric_name
        default_branch = await self._get_default_branch()

        valid_keys = ["maximum_super_spines", "maximum_pods", "maximum_spines", "maximum_switches"]
        filtered_pools = {key: pools[key] for key in valid_keys if key in pools}
        pod = await self.client.get(kind=TopologyPod, id=id) if pod_name else None
        pools_config = FabricPoolConfig(**filtered_pools, kind=strategy, ipv6=ipv6 or False)

        for pool_name, pool_size in pools_config.pools().items():
            if strategy == "fabric" and pool_name in ["management", "technical", "loopback"]:
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
                branch=default_branch,
            )

            self.logger.info(
                f"Allocating next IP prefix for pool '{pool_name}' (/{pool_size}) in parent '{parent_pool_name}'"
            )
            pool_full_name = f"{pool_prefix}-{pool_name}-pool"
            pool_identifier = pool_full_name

            allocated_prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=parent_pool,
                identifier=pool_identifier,
                prefix_length=pool_size,
                branch=default_branch,
                data={
                    "role": f"{pool_name if pool_name in ['management', 'technical', 'loopback'] else pool_name.split('-')[-1]}"
                },
            )

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
                        "identifier": pool_identifier,
                        "resources": [{"id": allocated_prefix.id}],
                    },
                    branch=default_branch,
                )
            else:
                new_pool = await self.client.create(
                    kind=CoreIPAddressPool,
                    data={
                        "name": pool_full_name,
                        "default_address_type": "IpamIPAddress",
                        "default_prefix_length": pool_size,
                        "ip_namespace": {"hfid": ["default"]},
                        "identifier": pool_identifier,
                        "resources": [{"id": allocated_prefix.id}],
                    },
                    branch=default_branch,
                )

            await new_pool.save(allow_upsert=True)

            pool_attribute_map = {"loopback": "loopback_pool", "technical": "prefix_pool"}
            if pod and pool_name in pool_attribute_map:
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
        options = options or {}
        fabric_name = self.fabric_name
        pod_name = self.pod_name or ""
        default_branch = await self._get_default_branch()
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
            loopback_pool_name = f"{fabric_name}-{device_role}-loopback-pool"
        else:
            loopback_pool_name = f"{device_prefix}-loopback-pool"

        device_kind = DcimVirtualDevice if virtual else DcimPhysicalDevice

        management_pool = await self.client.get(
            kind=CoreIPAddressPool,
            name__value=management_pool_name,
            branch=default_branch,
        )

        loopback_pool = None
        if allocate_loopback:
            loopback_pool = await self.client.get(
                kind=CoreIPAddressPool,
                name__value=loopback_pool_name,
                branch=default_branch,
            )

        batch_devices = await self.client.create_batch()
        batch_loopbacks = await self.client.create_batch()

        device_group = await self.client.get(kind=CoreStandardGroup, name__value=f"{device_role}s")
        try:
            existing_devices_list = await self.client.filters(
                kind=device_kind,
                name__values=device_names,
                include=["member_of_groups"],
            )
            existing_devices_map = {device.name.value: device for device in existing_devices_list}

            for name in device_names:
                existing_device = existing_devices_map.get(name)
                groups = [peer.id for peer in existing_device.member_of_groups.peers] if existing_device else []
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
                            branch=default_branch,
                            data={"description": f"Management IP for {name}"},
                        ),
                        "rack": {"id": rack} if rack else None,
                        "member_of_groups": [{"id": group_id} for group_id in groups],
                    },
                )
                batch_devices.add(task=obj.save, allow_upsert=True, node=obj)

                if loopback_pool:
                    obj = await self.client.create(
                        kind=DcimVirtualInterface,
                        data={
                            "name": "Loopback0",
                            "description": "Loopback interface",
                            "device": {"hfid": name},
                            "status": "active",
                            "role": "loopback",
                            "ip_addresses": [
                                await self.client.allocate_next_ip_address(
                                    resource_pool=loopback_pool,
                                    identifier=name,
                                    prefix_length=32,
                                    branch=default_branch,
                                    data={"description": f"Loopback IP for {name}"},
                                )
                            ],
                        },
                    )
                    batch_loopbacks.add(task=obj.save, allow_upsert=True, node=obj)

            async for node, _ in batch_devices.execute():
                self.logger.info(f"- Created [{node.get_kind()}] {node.hfid}")
            async for node, _ in batch_loopbacks.execute():
                self.logger.info(f"- Created [{node.get_kind()}] {node.device.hfid} {node.name.value}")
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
            "hierarchical_rack",
            "intra_rack",
            "intra_rack_middle",
            "intra_rack_mixed",
            "custom",
        ] = "rack",
        options: Optional[dict[str, Any]] = None,
    ) -> None:
        """Create cabling connections between device layers (simple per-link flow)."""

        options = options or {}
        cabling_offset: int = int(options.get("cabling_offset", 0))
        pool: Any = options.get("pool") or (f"{self.pod_name}-technical-pool" if self.pod_name else None)
        default_branch = await self._get_default_branch()

        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up"
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up"

        self.logger.info(
            f"Creating cabling between {len(bottom_devices)} bottom and {len(top_devices)} top devices "
            f"(strategy: {strategy}, bottom: {bottom_sorting}, top: {top_sorting})"
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
            self.logger.warning(
                f"No available interfaces found; skipping cabling "
                f"(src: {len(src_interfaces) if src_interfaces else 0}, "
                f"dst: {len(dst_interfaces) if dst_interfaces else 0})"
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
                data={"name": name, "type": "mmf", "endpoints": [src_interface.id, dst_interface.id]},
                branch=default_branch,
            )
            await network_link.save(allow_upsert=True)

            updated_src = await self.client.get(DcimPhysicalInterface, id=src_interface.id, branch=default_branch)
            updated_dst = await self.client.get(DcimPhysicalInterface, id=dst_interface.id, branch=default_branch)

            if pool:
                technical_pool = await self.client.get(kind=CoreIPPrefixPool, name__value=pool, branch=default_branch)
                p2p_prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=technical_pool,
                    identifier=link_identifier,
                    prefix_length=31,
                    member_type="address",
                    branch=default_branch,
                    data={"role": "technical", "is_pool": True},
                )
                self.logger.info(f"- Allocated prefix {p2p_prefix.display_label} for {name}")

                host_addresses = p2p_prefix.prefix.value.hosts()  # type: ignore

                src_ip = await self.client.create(
                    kind=IpamIPAddress,
                    address=str(next(host_addresses)) + "/31",
                    branch=default_branch,
                )
                await src_ip.save(allow_upsert=True)
                updated_src.ip_address = {"id": src_ip.id}  # type: ignore

                dst_ip = await self.client.create(
                    kind=IpamIPAddress,
                    address=str(next(host_addresses)) + "/31",
                    branch=default_branch,
                )
                await dst_ip.save(allow_upsert=True)
                updated_dst.ip_address = {"id": dst_ip.id}  # type: ignore

            updated_src.description.value = name
            updated_dst.description.value = name
            updated_src.status.value = "active"
            updated_dst.status.value = "active"
            await updated_src.save(allow_upsert=True)
            await updated_dst.save(allow_upsert=True)
            self.logger.info(f"- Created connection {name}")
