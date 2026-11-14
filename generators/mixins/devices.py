"""Device creation mixin for CommonGenerator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.protocols import CoreIPAddressPool

if TYPE_CHECKING:
    import logging

from generators.helpers import DeviceNameContext, DeviceNamingConfig, get_loopback_name
from generators.mixins.domain_pairing import DomainPairingMixin
from generators.mixins.types import DeviceOptions, NamingConvention
from generators.mixins.virtual_nics import VirtualNicMixin
from generators.protocols import (
    CloudNetworkAppliance,
    DcimPhysicalDevice,
    DcimVirtualDevice,
    DcimVirtualInterface,
    ManagedController,
)
from utils.limits import ROLE_GROUP_NAMES

# device_role values that pair into an HA domain (DeviceOptions.ha_kind) rather than MLAG
_HA_PAIRED_ROLES = frozenset({"internal-firewall", "external-firewall", "load-balancer"})

# Fabric-tier roles route to a controller by controller_type alone (no platform match needed)
_FABRIC_CONTROLLER_TYPES = frozenset({"aci_apic", "dcnm", "nsx_manager", "ucs_manager", "dna_center"})
_FABRIC_ROLES = frozenset(
    {
        "super-spine",
        "hyper-spine",
        "border-leaf",
        "border-spine",
        "spine",
        "leaf",
        "tor",
        "l2-leaf",
        "access-leaf",
        "access-switch",
        "distribution-switch",
    }
)

# A generator query's role-bucketed controller aliases (queries/topology/add/dc.gql), merged by set_controllers_from
_CONTROLLER_ALIASES = ("fabric_controllers", "security_manager_controllers", "lb_manager_controllers")

# firewall/load-balancer route to a controller by controller_type AND platform, so a checkpoint_gaia firewall never gets
# a Panorama controller
_ROLE_CONTROLLER_TYPE: dict[str, str] = {
    "internal-firewall": "security_manager",
    "external-firewall": "security_manager",
    "load-balancer": "lb_manager",
}

# device_role -> HA node kind (pairing itself happens in create_devices())
HA_KIND_BY_ROLE: dict[str, str] = {
    "internal-firewall": "ManagedFirewallHA",
    "external-firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}


def _object_id(value: Any | None) -> Any | None:
    """id of a relationship reference given as None, a ``{"id": ...}`` dict or an SDK node."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", value)


def _initialized_peer_id(rel: Any) -> str | None:
    """Peer id of a RelatedNode, or None when missing or unset.
    A RelatedNode is always truthy and .peer raises when unset, so check .initialized first."""
    return rel.peer.id if rel is not None and rel.initialized else None


class DeviceMixin(DomainPairingMixin, VirtualNicMixin):
    """Device creation methods for CommonGenerator.
    The host provides ``client``, ``logger``, ``fabric_name``, ``pod_name`` and ``_resolve_pool``."""

    # Attribute declarations for the type checker, provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    fabric_name: str
    pod_name: str | None
    # CommonGenerator._resolve_pool, annotation only
    _resolve_pool: Any
    # Set by set_controllers_from(); read via getattr with default [] (no controllers is not an error)
    _all_controllers: list[dict[str, Any]]

    async def create_devices(
        self,
        device_role: str,
        quantity: int,
        deployment_id: str,
        template: dict[str, Any],
        naming_convention: NamingConvention = "flat",
        options: DeviceOptions | None = None,
        *,
        hosting_device: Any | None = None,
    ) -> list[str]:
        """Create devices in a batch using self.fabric_name and self.pod_name (if set); see ``DeviceOptions`` for
        options. The owner comes from ``template["owner"]``; ``hosting_device`` places a virtual device on its host.
        """
        if options is None:
            options = DeviceOptions()
        fabric_name = self.fabric_name
        pod_name = self.pod_name or ""
        appliance: bool = bool(options.get("appliance", False))
        account_id: str | None = options.get("account_id")
        virtual: bool = appliance or bool(options.get("virtual", False))
        indexes: list[int] | None = options.get("indexes", None)
        allocate_loopback: bool = bool(options.get("allocate_loopback", False))
        rack: str = options.get("rack", "")

        # Pool references: SDK objects, ID strings, or None
        provided_loopback_pool = options.get("loopback_pool")
        provided_management_pool = options.get("management_pool")

        device_prefix: str = fabric_name if not pod_name else pod_name

        name_override = options.get("name_override")
        if name_override:
            if quantity != 1:
                raise ValueError(f"name_override is only valid with quantity=1, got quantity={quantity}")
            device_names: list[str] = [name_override]
        else:
            naming = DeviceNamingConfig(strategy=naming_convention)
            first_role_index = options.get("role_index_start", 1)
            device_names = sorted(
                [
                    naming.format_device_name(
                        DeviceNameContext.from_indexes(
                            fabric_name=fabric_name,
                            device_role=device_role,
                            role_index=idx,
                            indexes=indexes or [],
                        )
                    )
                    for idx in range(first_role_index, first_role_index + quantity)
                ]
            )
        management_pool_name = f"{fabric_name}-management-pool"

        if device_role in ("super-spine", "border-leaf"):
            # Both are DC-level fabric tiers sharing one fabric-scoped loopback pool (dc.py "dc-fabric-loopback")
            loopback_pool_name = f"{fabric_name}-dc-fabric-loopback-pool"
        else:
            # Other devices use the pod-level loopback pool
            loopback_pool_name = f"{device_prefix}-loopback-pool"

        device_kind = CloudNetworkAppliance if appliance else DcimVirtualDevice if virtual else DcimPhysicalDevice

        owner_id = _object_id(template.get("owner"))
        hosting_device_id = _object_id(hosting_device)

        # Resolve pools: SDK objects, ID strings, or name-based lookup
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

        controller_info = self._resolve_role_controller(device_role=device_role, template=template)
        controller = None
        if controller_info is not None:
            # include= loads the managed_devices peer ids, so the attach below needs no fetch()
            controller = await self.client.get(
                kind=ManagedController, id=controller_info["id"], include=["managed_devices"]
            )

        device_group = None
        if controller is None:
            group_name = ROLE_GROUP_NAMES.get(device_role) or f"{device_role}s"
            # A missing bootstrap group (e.g. ad-hoc test branches) is created
            device_group = await self._standard_group(
                group_name, create_description=f"Auto-created by generator for role {device_role}"
            )
        try:
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
                    # device is a mandatory Parent (schemas/base/dcim.yml), resolvable via include=["device"]
                    existing_loopbacks_by_device[loopback.device.peer.name.value] = loopback

            for name in device_names:
                existing_device = existing_devices_map.get(name)
                if controller is not None:
                    # Controller-managed: tracked via managed_devices below, not group membership
                    groups = []
                elif existing_device:
                    groups = [peer.id for peer in existing_device.member_of_groups.peers]
                else:
                    groups = []

                if device_group is not None and device_group.id not in groups:
                    groups.append(device_group.id)

                primary_address_rel = getattr(existing_device, "primary_address", None) if existing_device else None
                primary_address_id = _initialized_peer_id(primary_address_rel)
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
                        # Pass the existing id so the upsert matches by ID
                        **({"id": existing_device.id} if existing_device else {}),
                        "name": name,
                        # object_template only on first creation: re-sending it on an existing device fails server-side
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
                        **({"account": {"id": account_id} if account_id else None} if appliance else {}),
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
                    loopback_ip_id = _initialized_peer_id(loopback_ip_rel)
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
                            "device": obj,
                            "status": "active",
                            "role": "loopback",
                            "ip_address": loopback_ip_data,
                        },
                    )
                    batch_loopbacks.add(task=loopback_obj.save, allow_upsert=True, node=loopback_obj)

            created_devices = []
            created_loopbacks = []

            async for node, error in batch_devices.execute():
                if error:
                    self.logger.error(f"  - Failed to save [{node.get_kind()}] {node.hfid}: {error}")
                    raise ValidationError(str(error))
                created_devices.append(node)
                verb = "Updated" if node.name.value in existing_devices_map else "Created"
                self.logger.info(f"  - {verb} [{node.get_kind()}] {node.hfid}")

            async for node, error in batch_loopbacks.execute():
                if error:
                    self.logger.error(f"  - Failed to save loopback for {node.device.hfid}: {error}")
                    raise ValidationError(str(error))
                created_loopbacks.append(node)
                verb = "Updated" if node.device.hfid[0] in existing_loopbacks_by_device else "Created"
                self.logger.info(f"  - {verb} [{node.get_kind()}] {node.device.hfid} {node.name.value}")

            self.logger.info(
                f"Device creation completed: {len(created_devices)} {device_role}(s) created"
                + (f" with {len(created_loopbacks)} loopback interface(s)" if created_loopbacks else "")
            )

            if controller is not None and created_devices:
                existing_managed_ids = {peer.id for peer in controller.managed_devices.peers}
                missing_ids = [node.id for node in created_devices if node.id not in existing_managed_ids]
                # A RelationshipAdd of just these devices, not a list save, since concurrent racks and pods attach to
                # one controller; untracked because the controller is shared data
                if missing_ids:
                    await controller.add_relationships(relation_to_update="managed_devices", related_nodes=missing_ids)
                self.logger.info(
                    f"Added {len(created_devices)} {device_role}(s) to controller {controller.hfid}'s managed_devices"
                )
        except ValidationError as exc:
            self.logger.error("Batch creation failed with validation error: %s", exc)
            raise

        if device_kind is DcimVirtualDevice and hosting_device_id:
            await self._pin_virtual_nics(created_devices, hosting_device_id)

        ha_kind = options.get("ha_kind")
        if ha_kind and device_role in _HA_PAIRED_ROLES:
            await self._ensure_ha_pairs(device_names, ha_kind=ha_kind, role_label=device_role, device_kind=device_kind)

        mlag_create = options.get("mlag_create", "no")
        if mlag_create != "no":
            await self._ensure_mlag_pairs(
                device_names,
                devices_by_name={node.name.value: node for node in created_devices},
                role_label=device_role,
                template=template,
                mlag_create=mlag_create,
                supports_virtual=options.get("mlag_supports_virtual", True),
            )

        return device_names

    async def create_ha_role_devices(
        self,
        *,
        role: Literal["internal-firewall", "external-firewall", "load-balancer"],
        entries: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: NamingConvention,
        indexes: list[int],
        appliance_template_kind: str | None = None,
        appliance_options: DeviceOptions | None = None,
    ) -> list[tuple[dict[str, Any], list[str]]]:
        """Create HA-paired firewall/load-balancer devices for one fabric_templates role; returns (entry, created_names)
        per entry. No loopbacks. An entry matching ``appliance_template_kind`` gets DeviceOptions.appliance=True plus
        ``appliance_options``.
        """
        options = DeviceOptions(indexes=indexes, ha_kind=HA_KIND_BY_ROLE[role])

        results: list[tuple[dict[str, Any], list[str]]] = []
        for entry in entries:
            entry_options = dict(options)
            if (
                appliance_template_kind is not None
                and (entry.get("template") or {}).get("template_kind") == appliance_template_kind
            ):
                entry_options.update(appliance_options or {})
                entry_options["appliance"] = True
            names = await self.create_devices(
                deployment_id=deployment_id,
                device_role=role,
                quantity=entry["quantity"],
                template=entry["template"],
                naming_convention=naming_convention,
                options=cast("DeviceOptions", entry_options),
            )
            results.append((entry, names))
        return results

    def set_controllers_from(self, source: Mapping[str, Any]) -> None:
        """Set ``self._all_controllers`` from a query's fabric/security_manager/lb_manager controller aliases; an absent
        alias adds nothing.
        """
        self._all_controllers = [controller for alias in _CONTROLLER_ALIASES for controller in source.get(alias) or []]

    def _resolve_role_controller(self, *, device_role: str, template: dict[str, Any]) -> dict[str, Any] | None:
        """The pre-fetched controller governing this device_role from ``self._all_controllers``, no query.
        Fabric roles match by controller_type alone; firewall/load-balancer by controller_type AND platform.
        Returns None (caller falls back to a CoreStandardGroup) when there is no mapping or match."""
        if device_role not in _FABRIC_ROLES and device_role not in _ROLE_CONTROLLER_TYPE:
            return None

        controllers: list[dict[str, Any]] = getattr(self, "_all_controllers", [])

        controller = None
        if device_role in _FABRIC_ROLES:
            controller = next((c for c in controllers if c.get("controller_type") in _FABRIC_CONTROLLER_TYPES), None)
        else:
            wanted_type = _ROLE_CONTROLLER_TYPE[device_role]
            template_platform_id = (template.get("platform") or {}).get("id") if template else None
            controller = next(
                (
                    c
                    for c in controllers
                    if c.get("controller_type") == wanted_type
                    and (c.get("platform") or {}).get("id") == template_platform_id
                ),
                None,
            )

        if controller is None and controllers:
            # Warn only if the deployment has some controllers but none match this role
            self.logger.warning(
                f"No controller matching role={device_role!r} found among this deployment's "
                f"controllers — devices will be added to the '{device_role}s' group instead. "
                "Assign a matching controller via TopologyDataCenter.controllers to route them there."
            )
        return controller
