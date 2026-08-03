"""Device creation mixin for CommonGenerator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.exceptions import NodeNotFoundError, ValidationError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreStandardGroup

if TYPE_CHECKING:
    import logging

from .helpers import DeviceNameContext, DeviceNamingConfig, get_loopback_name
from .protocols import DcimPhysicalDevice, DcimVirtualDevice, DcimVirtualInterface
from .types import DeviceOptions


class DeviceMixin:
    """Mixin providing device creation methods for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``, ``fabric_name``,
    ``pod_name``, and ``_resolve_pool`` (all present on ``CommonGenerator``).
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    fabric_name: str
    pod_name: str | None
    # CommonGenerator._resolve_pool (PoolMixin) — annotation only, no method body.
    _resolve_pool: Any

    async def create_devices(
        self,
        device_role: str,
        quantity: int,
        deployment_id: str,
        template: dict[str, Any],
        naming_convention: Literal["standard", "hierarchical", "flat", "computed"] = "flat",
        options: DeviceOptions | None = None,
        *,
        owner: Any | None = None,
        hosting_device: Any | None = None,
    ) -> list[str]:
        """Create devices using batch creation.

        Uses self.fabric_name and self.pod_name (if set) from instance variables.
        See ``DeviceOptions`` for available option keys. ``owner`` and
        ``hosting_device`` let callers supply shared placement context for both
        physical and virtual device creation.
        """
        # Normalize options
        if options is None:
            options = DeviceOptions()
        fabric_name = self.fabric_name
        pod_name = self.pod_name or ""
        virtual: bool = bool(options.get("virtual", False))
        indexes: list[int] | None = options.get("indexes", None)
        allocate_loopback: bool = bool(options.get("allocate_loopback", False))
        rack: str = options.get("rack", "")

        # Accept pool references from options: SDK objects, ID strings, or None
        provided_loopback_pool = options.get("loopback_pool")
        provided_management_pool = options.get("management_pool")

        device_prefix: str = fabric_name if not pod_name else pod_name

        naming = DeviceNamingConfig(strategy=naming_convention)
        device_names: list[str] = sorted(
            [
                naming.format_device_name(
                    DeviceNameContext.from_indexes(
                        fabric_name=fabric_name,
                        device_role=device_role,
                        role_index=idx,
                        indexes=indexes or [],
                    )
                )
                for idx in range(1, quantity + 1)
            ]
        )
        management_pool_name = f"{fabric_name}-management-pool"

        if device_role in ("super-spine", "border-leaf"):
            # Both are DC-level fabric tiers sharing one fabric-scoped loopback pool
            # (see dc.py's "dc-fabric-loopback" allocation) — neither is owned by
            # any one pod's own loopback pool.
            loopback_pool_name = f"{fabric_name}-dc-fabric-loopback-pool"
        else:
            # Other devices (spine, leaf, etc.) use pod-level loopback pool
            # device_prefix already includes fabric-pod combination when pod_name is present
            loopback_pool_name = f"{device_prefix}-loopback-pool"

        device_kind = DcimVirtualDevice if virtual else DcimPhysicalDevice

        def _object_id(value: Any | None) -> Any | None:
            if value is None:
                return None
            if isinstance(value, dict):
                return value.get("id")
            return getattr(value, "id", value)

        owner_id = _object_id(owner)
        hosting_device_id = _object_id(hosting_device)
        template_owner = template.get("owner") if isinstance(template, dict) else None
        template_owner_id = _object_id(template_owner)
        if owner_id is None:
            owner_id = template_owner_id

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

        group_name = options.get("group_name") or f"{device_role}s"
        try:
            device_group = await self.client.get(kind=CoreStandardGroup, name__value=group_name)
        except NodeNotFoundError:
            # Keep generators robust on branches where bootstrap groups were not
            # loaded yet (e.g. ad-hoc test branches).
            device_group = await self.client.create(
                kind=CoreStandardGroup,
                data={
                    "name": group_name,
                    "description": f"Auto-created by generator for role {device_role}",
                },
            )
            await device_group.save(allow_upsert=True)
            self.logger.info(f"Created missing device group '{group_name}' for role '{device_role}'")
        try:
            # Fetch all existing devices in a single batch to optimize performance
            existing_devices_list = await self.client.filters(
                kind=device_kind,
                name__values=device_names,
                include=["member_of_groups", "primary_address"],
            )
            existing_devices_map = {device.name.value: device for device in existing_devices_list}

            existing_loopbacks_by_device: dict[str, Any] = {}
            if loopback_pool:
                existing_loopbacks = await self.client.filters(
                    kind=DcimVirtualInterface,
                    device__name__values=device_names,
                    role__value="loopback",
                    include=["device", "ip_address"],
                )
                for loopback in existing_loopbacks:
                    # device is a mandatory Parent relationship (schemas/base/dcim.yml),
                    # always resolvable given include=["device"] above.
                    existing_loopbacks_by_device[loopback.device.peer.name.value] = loopback

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

                primary_address_rel = getattr(existing_device, "primary_address", None) if existing_device else None
                primary_address_peer = getattr(primary_address_rel, "peer", None)
                primary_address_obj = primary_address_peer or primary_address_rel
                primary_address_id = getattr(primary_address_obj, "id", None)
                if primary_address_id:
                    primary_address_data: Any = {"id": primary_address_id}
                else:
                    primary_address_data = await self.client.allocate_next_ip_address(
                        resource_pool=management_pool,
                        identifier=name,
                        prefix_length=32,
                        data={"description": f"Management IP for {name}"},
                    )

                obj = await self.client.create(
                    kind=device_kind,
                    data={
                        # Pass existing id so upsert matches by ID, not hfid lookup
                        **({"id": existing_device.id} if existing_device else {}),
                        "name": name,
                        # Only send object_template on first creation — re-sending it on an existing
                        # device triggers a server-side re-instantiation that fails with
                        # "device is mandatory for DcimPhysicalInterface".
                        **(
                            {"object_template": {"id": template.get("id") if template else None}}
                            if not existing_device
                            else {}
                        ),
                        "status": "active",
                        "role": device_role,
                        "deployment": {"id": deployment_id} if deployment_id else None,
                        "device_type": template.get("device_type"),
                        "platform": template.get("platform"),
                        **({"owner": {"id": owner_id}} if owner_id else {}),
                        **({"hosting_device": {"id": hosting_device_id}} if virtual and hosting_device_id else {}),
                        "primary_address": primary_address_data,
                        "rack": {"id": rack} if rack else None,
                        "member_of_groups": [{"id": group_id} for group_id in groups],
                    },
                )
                batch_devices.add(task=obj.save, allow_upsert=True, node=obj)

                loopback_obj = None
                if loopback_pool:
                    existing_loopback = existing_loopbacks_by_device.get(name)
                    loopback_ip_rel = getattr(existing_loopback, "ip_address", None) if existing_loopback else None
                    loopback_ip_peer = getattr(loopback_ip_rel, "peer", None)
                    loopback_ip_obj = loopback_ip_peer or loopback_ip_rel
                    loopback_ip_id = getattr(loopback_ip_obj, "id", None)
                    if loopback_ip_id:
                        loopback_ip_data: Any = {"id": loopback_ip_id}
                    else:
                        loopback_ip_data = await self.client.allocate_next_ip_address(
                            resource_pool=loopback_pool,
                            identifier=name,
                            prefix_length=options.get("loopback_prefix_length", 32),
                            data={"description": f"Loopback IP for {name}"},
                        )

                    loopback_obj = await self.client.create(
                        kind=DcimVirtualInterface,
                        data={
                            **({"id": existing_loopback.id} if existing_loopback else {}),
                            "name": get_loopback_name((template.get("platform") or {}).get("name") or "", 0),
                            "description": "Loopback interface",
                            # Reference device object directly
                            "device": obj,
                            "status": "active",
                            "role": "loopback",
                            "ip_address": loopback_ip_data,
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
