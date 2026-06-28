from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from typing import Any, Literal, Optional

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreStandardGroup

from .helpers import CableTypeDetector, CablingPlanner, DeviceNamingConfig
from .protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    IpamIPAddress,
    TopologyPod,
)
from .routing import RoutingMixin

# Re-export TypedDicts so existing imports (from .common import DeviceOptions, ...) keep working
from .types import CablingOptions, DeviceOptions, RoutingOptions  # noqa: F401


class CommonGenerator(RoutingMixin, InfrahubGenerator):
    """
    An extended InfrahubGenerator with helper methods for creating objects.

    Instance variables set during generate() lifecycle:
        deployment_id: Root deployment (DC/POP) ID for linking cables (required)
        fabric_name: Fabric/DC name (lowercase) for pool and device naming (required)
        pod_name: Pod name (lowercase) for pool naming (optional, only in pod/rack generators)

    Error handling conventions:
        - ``generate()`` entry points: ``self.logger.error()`` + ``return``.
          These are called by the SDK framework; raising would crash the workflow.
        - Internal helper methods (``_get_spine_devices``, etc.): ``raise RuntimeError``
          for missing prerequisites so the caller can decide how to handle it.
        - Pure helpers (naming, routing, cabling): ``raise ValueError`` for invalid inputs.
        - Loop iterations: ``self.logger.warning()`` + ``continue`` to skip one bad item
          without halting the entire batch operation.
    """

    # Instance variables - must be set in generate() before calling helper methods
    deployment_id: str = ""  # Required: set to DC/POP ID
    fabric_name: str = ""  # Required: set to fabric/DC name
    pod_name: Optional[str] = None  # Optional: only for pod/rack generators

    async def _resolve_pool(
        self,
        provided: Any,
        kind: type,
        fallback_name: str | None = None,
    ) -> Any:
        """Resolve a pool reference to an SDK node object.

        Accepts:
        - SDK node object (CoreIPAddressPool/CoreIPPrefixPool) → returned as-is
        - Pool ID string → resolved via client.get(id=...)
        - None + fallback_name → resolved via client.get(name__value=fallback_name)
        - None + no fallback_name → returns None (pool disabled)

        This avoids redundant client.get() calls when pool IDs are already
        available from GraphQL query data.
        """
        if provided is None:
            if fallback_name is None:
                return None
            return await self.client.get(kind=kind, name__value=fallback_name)
        if isinstance(provided, str):
            return await self.client.get(kind=kind, id=provided)
        # Already an SDK object
        return provided

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
        related_ids = self.client.group_context.related_group_ids + self.client.group_context.related_node_ids
        sorted_ids = sorted(related_ids)
        joined = ",".join(sorted_ids)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    async def upsert_number_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
        node: str,
        node_attribute: str,
        parent_kind: str | None = None,
        parent_id: str | None = None,
        parent_attr: str | None = None,
    ) -> Any:
        """Create or update a CoreNumberPool and optionally link it to a parent.

        Args:
            pool_name: Name for the pool
            description: Pool description
            start_range: First value in range
            end_range: Last value in range
            node: Infrahub node kind for allocation (e.g. "RoutingAutonomousSystem")
            node_attribute: Attribute on the node (e.g. "asn", "vlan_id", "vni")
            parent_kind: Optional parent kind to link the pool to
            parent_id: Optional parent ID
            parent_attr: Optional attribute name on parent (e.g. "vlan_pool")

        Returns:
            The created/upserted CoreNumberPool SDK object
        """
        pool = await self.client.create(
            kind="CoreNumberPool",
            data={
                "name": pool_name,
                "description": description,
                "node": node,
                "node_attribute": node_attribute,
                "start_range": start_range,
                "end_range": end_range,
            },
        )
        await pool.save(allow_upsert=True)
        self.logger.info(
            "Upserted number pool %s (range: %s-%s, id: %s)",
            pool_name,
            start_range,
            end_range,
            pool.id,
        )

        if parent_kind and parent_id and parent_attr:
            parent = await self.client.get(kind=parent_kind, id=parent_id)
            if parent:
                setattr(parent, parent_attr, {"id": pool.id, "hfid": [pool.hfid]})
                await parent.save(allow_upsert=True)
                self.logger.info("- Updated %s with %s (id: %s)", parent_kind, parent_attr, pool.id)

        return pool

    async def upsert_asn_pool(
        self,
        pool_name: str,
        description: str,
        start_range: int,
        end_range: int,
        parent_kind: str,
        parent_id: str,
        parent_attr: str,
    ) -> Any:
        """Create or update an ASN CoreNumberPool. Convenience wrapper around upsert_number_pool."""
        return await self.upsert_number_pool(
            pool_name=pool_name,
            description=description,
            start_range=start_range,
            end_range=end_range,
            node="RoutingAutonomousSystem",
            node_attribute="asn",
            parent_kind=parent_kind,
            parent_id=parent_id,
            parent_attr=parent_attr,
        )

    async def allocate_resource_pools(
        self,
        strategy: Literal["fabric", "pod"],
        pools: dict[str, Any],
        id: str,
        ipv6: Optional[bool] = False,
        dual_stack: Optional[bool] = False,
    ) -> dict[str, Any]:
        """Ensure required per-pod / fabric pools exist.

        Args:
            strategy: "fabric" for DC-level pools, "pod" for pod-level pools
            pools: Dictionary of explicit pool sizes {pool_name: prefix_length}
            id: DC or Pod ID
            ipv6: Use IPv6 for data pools
            dual_stack: IPv6 for technical/P2P pools, IPv4 for loopback/management

        Returns:
            Dictionary mapping pool names to pool objects: {"loopback": pool_obj, "technical": pool_obj}

        Notes:
        - Requires explicit pool sizes like {"technical": 24, "loopback": 28}
        - Fabric strategy also requires "management" and "super-spine-loopback" pools
        """
        self.logger.info("Implementing resource pools")

        fabric_name = self.fabric_name
        pod_name = self.pod_name
        pool_prefix = pod_name if pod_name else fabric_name

        # Get pod object if working with pod strategy (needed for updating pool references)
        pod = await self.client.get(kind=TopologyPod, id=id) if pod_name else None

        # Store created pools to return
        created_pools = {}

        # Use explicit pool sizes (all callers now provide explicit sizes)
        for pool_name, pool_size in pools.items():
            if strategy == "fabric" and pool_name in [
                "management",
                "technical",
                "loopback",
            ]:
                # Dual-stack: technical uses IPv6, loopback/management use IPv4
                # Full IPv6: technical and loopback use IPv6, management uses IPv4
                if dual_stack:
                    use_ipv6 = pool_name == "technical"
                elif ipv6:
                    use_ipv6 = pool_name != "management"
                else:
                    use_ipv6 = False
                parent_pool_name = f"{pool_name.capitalize()}-IPv6" if use_ipv6 else f"{pool_name.capitalize()}-IPv4"
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

            # Determine if this is a prefix or address pool
            is_prefix_pool = (strategy == "fabric" and pool_name in ["technical", "loopback"]) or (
                strategy == "pod" and pool_name == "technical"
            )

            # Allocate prefix from parent pool (idempotent via identifier)
            allocated_prefix = await self.client.allocate_next_ip_prefix(
                resource_pool=parent_pool,
                identifier=pool_full_name,
                prefix_length=pool_size,
                data={
                    "role": f"{pool_name if pool_name in ['management', 'technical', 'loopback'] else pool_name.split('-')[-1]}",
                    "identifier": pool_full_name,
                },
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

            pool_kind = "CoreIPPrefixPool" if is_prefix_pool else "CoreIPAddressPool"
            self.logger.info(f"- Created [{pool_kind}] {new_pool.hfid}")

            created_pools[pool_name] = new_pool

        # Update pod with all pool references in a single save
        pool_attribute_map = {
            "loopback": "loopback_pool",
            "technical": "prefix_pool",
        }

        if pod:
            pod_updated = False
            for pool_name, pool_obj in created_pools.items():
                if pool_name in pool_attribute_map:
                    pool_ref = {"id": pool_obj.id, "hfid": [pool_obj.hfid]}
                    setattr(pod, pool_attribute_map[pool_name], pool_ref)
                    self.logger.info(f"- Attaching pool {pool_obj.hfid} to pod (id: {pool_obj.id})")
                    pod_updated = True
            if pod_updated:
                await pod.save(allow_upsert=True)
                self.logger.info(f"- Saved pod {pod.name.value} with all pool references")

        return created_pools

    async def create_devices(
        self,
        device_role: str,
        amount: int,
        deployment_id: str,
        template: dict[str, Any],
        naming_convention: Literal["standard", "hierarchical", "flat"] = "flat",
        options: DeviceOptions | None = None,
    ) -> list[str]:
        """Create devices using batch creation.

        Uses self.fabric_name and self.pod_name (if set) from instance variables.
        See ``DeviceOptions`` for available option keys.
        """
        # Normalize options
        if options is None:
            options = DeviceOptions()
        fabric_name = self.fabric_name
        pod_name = self.pod_name or ""
        virtual: bool = bool(options.get("virtual", False))
        indexes: Optional[list[int]] = options.get("indexes", None)
        allocate_loopback: bool = bool(options.get("allocate_loopback", False))
        rack: str = options.get("rack", "")

        # Accept pool references from options: SDK objects, ID strings, or None
        provided_loopback_pool = options.get("loopback_pool")
        provided_management_pool = options.get("management_pool")

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

        # Resolve pools: accept SDK objects, ID strings, or fall back to name-based lookup
        management_pool = await self._resolve_pool(
            provided=provided_management_pool,
            kind=CoreIPAddressPool,
            fallback_name=management_pool_name,
        )

        loopback_pool = None
        if allocate_loopback:
            loopback_pool = await self._resolve_pool(
                provided=provided_loopback_pool,
                kind=CoreIPAddressPool,
                fallback_name=loopback_pool_name,
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
                        # Pass existing id so upsert matches by ID, not hfid lookup
                        **({"id": existing_device.id} if existing_device else {}),
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

                loopback_obj = None
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
                            "ip_address": await self.client.allocate_next_ip_address(
                                resource_pool=loopback_pool,
                                identifier=name,
                                prefix_length=options.get("loopback_prefix_length", 32),
                                data={"description": f"Loopback IP for {name}"},
                            ),
                        },
                    )
                    batch_loopbacks.add(task=loopback_obj.save, allow_upsert=True, node=loopback_obj)

            # Execute batch and collect created nodes
            created_devices = []
            created_loopbacks = []

            async for node, error in batch_devices.execute():
                if error:
                    self.logger.error(f"  - Failed to save [{node.get_kind()}] {node.hfid}: {error}")
                    raise ValidationError(str(error))
                created_devices.append(node)
                self.logger.info(f"  - Created [{node.get_kind()}] {node.hfid}")

            async for node, error in batch_loopbacks.execute():
                if error:
                    self.logger.error(f"  - Failed to save loopback for {node.device.hfid}: {error}")
                    raise ValidationError(str(error))
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
        options: CablingOptions | None = None,
        bottom_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
        top_sorting: Literal["top_down", "bottom_up"] = "bottom_up",
    ) -> None:
        """Create cabling connections between device layers.

        Simple approach: query interfaces → build plan → for each connection:
        create cable, fetch interfaces, allocate IPs, save interfaces.
        All saves use allow_upsert=True for idempotency and generator tracking.
        """
        if options is None:
            options = CablingOptions()
        cabling_offset: int = int(options.get("cabling_offset", 0))

        self.logger.info(
            f"Creating cabling: {len(bottom_devices)} bottom → {len(top_devices)} top "
            f"[strategy={strategy}, offset={cabling_offset}]"
        )

        # Retry querying interfaces until template instantiation completes.
        # Templates are applied asynchronously; a fixed sleep is fragile under load.
        _MAX_RETRIES = 10
        _RETRY_DELAY = 3.0
        src_interfaces: list = []
        dst_interfaces: list = []
        for _attempt in range(_MAX_RETRIES):
            src_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=bottom_devices,
                name__values=bottom_interfaces,
                include=["cable"],
            )
            dst_interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__name__values=top_devices,
                name__values=top_interfaces,
                include=["cable"],
            )
            if src_interfaces and dst_interfaces:
                break
            self.logger.info(
                f"Interfaces not ready yet (src={len(src_interfaces)}, dst={len(dst_interfaces)}) — "
                f"retrying in {_RETRY_DELAY}s (attempt {_attempt + 1}/{_MAX_RETRIES})"
            )
            await asyncio.sleep(_RETRY_DELAY)

        if not src_interfaces or not dst_interfaces:
            self.logger.error(
                f"Interfaces still not found after {_MAX_RETRIES} attempts "
                f"(src={len(src_interfaces)}, dst={len(dst_interfaces)}) — skipping cabling"
            )
            return

        # Build lookup map for O(1) access after cabling plan is built
        iface_map: dict[str, Any] = {iface.id: iface for iface in src_interfaces + dst_interfaces}

        # Build cabling plan
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
            self.logger.warning("No cabling connections planned")
            return

        # Resolve technical pool for P2P address allocation
        technical_pool = await self._resolve_pool(
            provided=options.get("pool"),
            kind=CoreIPPrefixPool,
            fallback_name=None,
        )

        # Execute plan: create cable → allocate IPs → save interfaces
        for src_interface, dst_interface in cabling_plan:
            endpoint_names = sorted(
                [
                    f"{src_interface.device.display_label}-{src_interface.name.value}",
                    f"{dst_interface.device.display_label}-{dst_interface.name.value}",
                ]
            )
            cable_name = "__".join(endpoint_names)
            link_identifier = "__".join(sorted([src_interface.id, dst_interface.id]))

            src_intf_type = getattr(getattr(src_interface, "interface_type", None), "value", None)
            dst_intf_type = getattr(getattr(dst_interface, "interface_type", None), "value", None)
            cable_type = CableTypeDetector.detect_cable_type(src_intf_type, dst_intf_type)

            cable = await self.client.create(
                kind=DcimCable,
                data={
                    "name": cable_name,
                    "type": cable_type,
                    "endpoints": [src_interface.id, dst_interface.id],
                    "deployment": {"id": self.deployment_id} if self.deployment_id else None,
                },
            )
            await cable.save(allow_upsert=True)

            # Use already-fetched interface objects; set cable to prevent upsert sending null
            updated_src = iface_map[src_interface.id]
            updated_dst = iface_map[dst_interface.id]
            updated_src.cable = cable
            updated_dst.cable = cable

            # Allocate P2P addresses if pool provided
            # prefix_length: 127 for IPv6 (RFC 6164, default), 31 for IPv4 (RFC 3021, exception)
            p2p_prefix_length: int = options.get("p2p_prefix_length", 31)
            if technical_pool:
                p2p_prefix = await self.client.allocate_next_ip_prefix(
                    resource_pool=technical_pool,
                    identifier=link_identifier,
                    prefix_length=p2p_prefix_length,
                    member_type="address",
                    data={"role": "technical", "is_pool": True},
                )
                self.logger.info(f"- Allocated prefix {p2p_prefix.display_label} for {cable_name}")

                # Iterate the network directly — works for both /31 (RFC 3021) and
                # /127 (RFC 6164) where .hosts() returns only one address in Python.
                network = ipaddress.ip_network(p2p_prefix.prefix.value, strict=False)
                addrs = list(network)
                ip_namespace = p2p_prefix.ip_namespace

                src_ip = await self.client.create(
                    kind=IpamIPAddress,
                    data={
                        "address": f"{addrs[0]}/{p2p_prefix_length}",
                        "ip_namespace": ip_namespace,
                    },
                )
                await src_ip.save(allow_upsert=True)
                updated_src.ip_address = src_ip.id

                dst_ip = await self.client.create(
                    kind=IpamIPAddress,
                    data={
                        "address": f"{addrs[1]}/{p2p_prefix_length}",
                        "ip_namespace": ip_namespace,
                    },
                )
                await dst_ip.save(allow_upsert=True)
                updated_dst.ip_address = dst_ip.id

            # Save interfaces with fabric-p2p tag
            updated_src.description.value = cable_name
            updated_src.status.value = "active"
            updated_src.tags.add({"hfid": "fabric-p2p"})
            await updated_src.save(allow_upsert=True)

            updated_dst.description.value = cable_name
            updated_dst.status.value = "active"
            updated_dst.tags.add({"hfid": "fabric-p2p"})
            await updated_dst.save(allow_upsert=True)

            self.logger.info(f"  - Created connection {cable_name}")

    # Routing methods (create_routing, _find_existing_overlay_as, _find_existing_ospf_area)
    # are inherited from RoutingMixin — see generators/routing.py
