"""Device creation mixin for CommonGenerator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.exceptions import NodeNotFoundError, ValidationError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreStandardGroup

if TYPE_CHECKING:
    import logging

from .helpers import DeviceNameContext, DeviceNamingConfig, get_loopback_name
from .helpers.pairing import pair_device_names
from .mlag import MLAGWiringMixin
from .protocols import (
    DcimCable,
    DcimInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    ManagedController,
    ManagedHAInterface,
    ManagedMLAG,
)
from .types import DeviceOptions

# device_role values that pair into an HA domain via DeviceOptions.ha_kind
# rather than MLAG — see create_devices()'s pairing dispatch below.
_HA_PAIRED_ROLES = frozenset({"firewall", "load-balancer"})

# Fabric-tier device roles route to a controller by controller_type alone —
# ACI APIC/DCNM/NSX/UCS/DNA Center aren't a 1:1 vendor pairing with the
# device's own platform the way a firewall pairs with its dedicated
# manager, so no platform match is required for these.
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

# firewall/load-balancer route to a controller by controller_type AND a
# platform match against the device's own template — a checkpoint_gaia
# firewall must never route to a Panorama (panos) controller just because
# both happen to be controller_type=security_manager.
_ROLE_CONTROLLER_TYPE: dict[str, str] = {"firewall": "security_manager", "load-balancer": "lb_manager"}


class DeviceMixin(MLAGWiringMixin):
    """Mixin providing device creation methods for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``, ``fabric_name``,
    ``pod_name``, and ``_resolve_pool`` (all present on ``CommonGenerator``).
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    fabric_name: str
    pod_name: str | None
    # CommonGenerator._resolve_pool — annotation only, no method body.
    _resolve_pool: Any
    # PoolMixin.upsert_number_pool — every caller of _ensure_mlag_pairs
    # (RackGenerator, DCTopologyGenerator) already mixes in PoolMixin
    # alongside DeviceMixin; annotation only, no method body here.
    upsert_number_pool: Any
    # Set once per generator run (dc.py/pod.py/rack.py's generate()) by
    # merging their own query's fabric_controllers/security_manager_
    # controllers/lb_manager_controllers aliased fields — see
    # _resolve_role_controller, which reads this via getattr(...,
    # default=[]) since a generator that never sets it (e.g. one that
    # doesn't call create_devices() for a controller-eligible role) simply
    # means "no controllers", not an error.
    _all_controllers: list[dict[str, Any]]

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

        name_override = options.get("name_override")
        if name_override:
            if quantity != 1:
                raise ValueError(f"name_override is only valid with quantity=1, got quantity={quantity}")
            device_names: list[str] = [name_override]
        else:
            naming = DeviceNamingConfig(strategy=naming_convention)
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

        controller_info = self._resolve_role_controller(device_role=device_role, template=template)
        controller = None
        if controller_info is not None:
            controller = await self.client.get(kind=ManagedController, id=controller_info["id"])

        device_group = None
        if controller is None:
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
                if controller is not None:
                    # Controller-managed: tracked via managed_devices below,
                    # not group membership.
                    groups = []
                elif existing_device:
                    groups = [peer.id for peer in existing_device.member_of_groups.peers]
                else:
                    groups = []

                # Ensure the new group is not duplicated
                if device_group is not None and device_group.id not in groups:
                    groups.append(device_group.id)

                primary_address_rel = getattr(existing_device, "primary_address", None) if existing_device else None
                # primary_address_rel is a RelatedNode wrapper — always truthy even when
                # unset (no __bool__ override), and its .peer property raises ValueError
                # instead of returning None when neither id nor hfid is set. Check
                # .initialized before touching .peer at all.
                primary_address_id = (
                    primary_address_rel.peer.id
                    if primary_address_rel is not None and primary_address_rel.initialized
                    else None
                )
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
                    # loopback_ip_rel is a RelatedNode wrapper — always truthy even when
                    # unset (no __bool__ override), and its .peer property raises
                    # ValueError instead of returning None when neither id nor hfid is
                    # set. Check .initialized before touching .peer at all.
                    loopback_ip_id = (
                        loopback_ip_rel.peer.id if loopback_ip_rel is not None and loopback_ip_rel.initialized else None
                    )
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
                verb = "Updated" if node.name.value in existing_devices_map else "Created"
                self.logger.info(f"  - {verb} [{node.get_kind()}] {node.hfid}")

            async for node, error in batch_loopbacks.execute():
                if error:
                    self.logger.error(f"  - Failed to save loopback for {node.device.hfid}: {error}")
                    raise ValidationError(str(error))
                created_loopbacks.append(node)
                verb = "Updated" if node.device.hfid[0] in existing_loopbacks_by_device else "Created"
                self.logger.info(f"  - {verb} [{node.get_kind()}] {node.device.hfid} {node.name.value}")

            # Summary logging
            self.logger.info(
                f"Device creation completed: {len(created_devices)} {device_role}(s) created"
                + (f" with {len(created_loopbacks)} loopback interface(s)" if created_loopbacks else "")
            )

            if controller is not None and created_devices:
                managed_devices = controller.managed_devices
                await managed_devices.fetch()
                existing_managed_ids = {peer.id for peer in managed_devices.peers}
                for node in created_devices:
                    if node.id not in existing_managed_ids:
                        managed_devices.add(node)
                await controller.save(allow_upsert=True)
                self.logger.info(
                    f"Added {len(created_devices)} {device_role}(s) to controller {controller.hfid}'s managed_devices"
                )
        except ValidationError as exc:
            self.logger.error("Batch creation failed with validation error: %s", exc)
            raise

        ha_kind = options.get("ha_kind")
        if ha_kind and device_role in _HA_PAIRED_ROLES:
            await self._ensure_ha_pairs(device_names, ha_kind=ha_kind, role_label=device_role, device_kind=device_kind)

        mlag_create = options.get("mlag_create", "no")
        if mlag_create != "no":
            await self._ensure_mlag_pairs(
                device_names,
                role_label=device_role,
                template=options.get("mlag_peer_template", template),
                mlag_create=mlag_create,
                supports_virtual=options.get("mlag_supports_virtual", True),
            )

        return device_names

    def _resolve_role_controller(self, *, device_role: str, template: dict[str, Any]) -> dict[str, Any] | None:
        """Find the pre-fetched controller (if any) governing this
        device_role, from ``self._all_controllers`` — a flat list the caller
        (dc.py/pod.py/rack.py's generate()) builds once per run by merging
        its own query's fabric_controllers/security_manager_controllers/
        lb_manager_controllers aliased fields (see queries/topology/add/
        dc.gql) — no runtime query here, this data already arrived with the
        rest of the generator's own fetch.

        Fabric roles (spine/leaf/border-leaf/...) match by controller_type
        alone (ACI APIC/DCNM/NSX/UCS aren't a 1:1 vendor pairing with the
        device's own platform). firewall/load-balancer match by
        controller_type AND platform, so e.g. a checkpoint_gaia firewall
        never routes to a Panorama (panos) controller.

        Returns None (no controller — caller falls back to a
        CoreStandardGroup) when the role has no controller mapping, or no
        controller in ``self._all_controllers`` matches.
        """
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
            # Only warn when the deployment has SOME controllers but none
            # match this role — a deployment with no controllers at all is
            # simply not controller-managed, which isn't worth a warning.
            self.logger.warning(
                f"No controller matching role={device_role!r} found among this deployment's "
                f"controllers — devices will be added to the '{device_role}s' group instead. "
                "Assign a matching controller via TopologyDataCenter.controllers to route them there."
            )
        return controller

    async def _ensure_ha_pairs(
        self,
        device_names: list[str],
        *,
        ha_kind: str,
        role_label: str,
        device_kind: type[Any] = DcimPhysicalDevice,
        tenant_id: str | None = None,
    ) -> None:
        """Pair same-role devices two-at-a-time (sorted, odd one unpaired) into
        HA domains, then ensure each pair's HA sync interfaces/ManagedHAInterface/
        cable in the same call — no separate add_ha generator/trigger round-trip
        (that used to run async off a ManagedFirewallHA/LoadbalancerHA "updated"
        mutation, and re-fired 2-3x per domain whenever two concurrent callers
        raced on the filter-then-create check below, since every racer's
        allow_upsert=True save was itself an "updated" mutation). Shared by
        dc.py (DC-wide firewall/load-balancer) and pod.py (a border-spine pod's
        own firewall/load-balancer) — never pairs across pods/DCs, both members
        must sit in front of the same fabric to mean anything physically.
        device_kind is DcimVirtualDevice for the shared production/
        non-production virtual instances dc.py provisions per physical HA
        pair — same pairing logic, no physical cabling involved (mirrors the
        skip _ensure_ha_interfaces itself applies for non-physical devices).
        tenant_id sets .tenant on ha_kind for a dedicated pair (e.g.
        ManagedLoadbalancerHA created for one customer) — only meaningful for
        ha_kind's that carry a tenant relationship (LoadbalancerHA today;
        FirewallHA itself has none — a firewall's dedicated tenant is
        recorded one level down, on its ManagedFirewallContext). Ignored
        (never sent) when None, matching every non-dedicated caller."""
        ha_group = None
        for first, second in pair_device_names(device_names):
            ha_name = f"{first}-{second}-ha"
            existing = await self.client.filters(kind=ha_kind, name__value=ha_name, include=["capabilities"])
            member_ids: list[str] | None = None
            if existing:
                ha_obj = existing[0]
                self.client.group_context.related_node_ids.append(ha_obj.id)
            else:
                devices = await self.client.filters(kind=device_kind, name__values=[first, second])
                if len(devices) != 2:
                    self.logger.error(f"HA pair {first}/{second}: could not resolve both devices.")
                    continue

                if ha_group is None:
                    ha_group = await self.client.get(kind=CoreStandardGroup, name__value="ha_domains")
                ha_obj = await self.client.create(
                    kind=ha_kind,
                    data={
                        "name": ha_name,
                        "status": "active",
                        "capabilities": [{"id": dev.id} for dev in devices],
                        "member_of_groups": [{"id": ha_group.id}],
                        **({"tenant": {"id": tenant_id}} if tenant_id else {}),
                    },
                )
                await ha_obj.save(allow_upsert=True)
                self.logger.info(f"Created HA domain {ha_name} for {role_label}s")
                member_ids = [dev.id for dev in devices]

            await self._ensure_ha_interfaces(ha_obj, ha_name, device_kind=device_kind, member_ids=member_ids)

    async def _ensure_ha_interfaces(
        self,
        ha_obj: Any,
        ha_name: str,
        *,
        device_kind: type[Any] = DcimPhysicalDevice,
        member_ids: list[str] | None = None,
    ) -> None:
        """Ensure each member device has a ManagedHAInterface on its HA sync
        interface, and (for physical pairs only) a DcimCable between the two
        sync interfaces. Purely idempotent: every write is gated on an
        existing-membership check first, so a repeat call on an unchanged
        domain performs zero writes (see _ensure_ha_pairs's docstring for why
        that matters — this used to be a separate generator invoked via
        trigger, unconditionally create()-ing on every run).

        member_ids lets a caller that already knows the two device ids (the
        freshly-created-domain path in _ensure_ha_pairs) skip capabilities.fetch()
        entirely — a just-created ha_obj's capabilities peers only carry the id
        each was created with, no __typename, and RelatedNode.fetch() requires
        both. The "existing domain" path doesn't hit this: its ha_obj came from
        client.filters(..., include=["capabilities"]), which returns real
        typenames from the server."""
        if member_ids is None:
            caps = getattr(ha_obj, "capabilities")
            await caps.fetch()
            member_ids = [peer.id for peer in caps.peers]
        if not member_ids:
            return

        is_physical = device_kind is DcimPhysicalDevice
        member_devices = await self.client.filters(kind=device_kind, ids=member_ids, include=["deployment"])

        existing_ha_ifaces = await self.client.filters(
            kind=ManagedHAInterface, ha_domain__ids=[ha_obj.id], include=["interface_capabilities"]
        )
        existing_sync_iface_ids: set[str] = set()
        for node in existing_ha_ifaces:
            node_caps = getattr(node, "interface_capabilities")
            await node_caps.fetch()
            existing_sync_iface_ids.update(peer.id for peer in node_caps.peers)

        sync_ifaces: list[Any] = []
        for device_obj in member_devices:
            if is_physical:
                device_sync_ifaces = await self.client.filters(
                    kind=DcimPhysicalInterface, device__ids=[device_obj.id], role__value="ha", include=["cable"]
                )
            else:
                # Query the generic DcimInterface kind, not DcimVirtualInterface
                # — most virtual firewall/LB templates (every CloudGuard/PANOS/
                # NetScaler/etc. template except *_CUSTOMER_*) provision eth7 as
                # a TemplateDcimPhysicalInterface even on a virtual device, so
                # assuming "virtual device -> virtual iface" here missed it and
                # tried to create a colliding duplicate.
                device_sync_ifaces = await self.client.filters(
                    kind=DcimInterface, device__ids=[device_obj.id], role__value="ha"
                )
            sync_iface = device_sync_ifaces[0] if device_sync_ifaces else None
            if sync_iface is None and not is_physical:
                # Virtual devices from the *_CUSTOMER_* templates get a fixed
                # eth7 HA sync port (see data/bootstrap's virtual device
                # templates) — create it on demand rather than requiring
                # every template author to remember one.
                sync_iface = await self.client.create(
                    kind=DcimVirtualInterface,
                    data={
                        "name": "eth7",
                        "device": {"id": device_obj.id},
                        "status": "active",
                        "role": "ha",
                        "description": f"HA sync — {device_obj.name.value}",
                    },
                )
                await sync_iface.save(allow_upsert=True)
            if sync_iface is None:
                self.logger.error(f"[{device_obj.name.value}] No HA sync interface found (expected role=ha)")
                continue
            sync_ifaces.append((device_obj, sync_iface))

            if sync_iface.id in existing_sync_iface_ids:
                continue

            suffix = "HA-MIRROR" if "mirror" in sync_iface.name.value.lower() else "HA-SYNC"
            node_name = f"{device_obj.name.value}-{suffix}"
            self.logger.info(
                f"  [{device_obj.name.value}:{sync_iface.name.value}] Creating ManagedHAInterface {node_name}"
            )
            ha_iface = await self.client.create(
                kind=ManagedHAInterface,
                data={
                    "name": node_name,
                    "link_type": "sync",
                    "status": "active",
                    "description": f"HA link — {device_obj.name.value}:{sync_iface.name.value}",
                    "ha_domain": {"id": ha_obj.id},
                    "interface_capabilities": [{"id": sync_iface.id}],
                },
            )
            await ha_iface.save(allow_upsert=True)

        if is_physical and len(sync_ifaces) == 2:
            await self._ensure_ha_cable(ha_name, sync_ifaces)

    async def _ensure_ha_cable(self, ha_name: str, sync_ifaces: list[tuple[Any, Any]]) -> None:
        """Create the DcimCable between two peer devices' HA sync interfaces —
        physical HA pairs only, called once both member interfaces are known."""
        (dev_a, iface_a), (dev_b, iface_b) = sorted(sync_ifaces, key=lambda pair: pair[0].name.value)
        cable_name = f"CBL-{ha_name}-SYNC"

        existing = await self.client.filters(kind=DcimCable, name__value=cable_name)
        if existing:
            return

        existing_cable_a = getattr(iface_a, "cable", None)
        existing_cable_b = getattr(iface_b, "cable", None)
        if (existing_cable_a is not None and existing_cable_a.initialized) or (
            existing_cable_b is not None and existing_cable_b.initialized
        ):
            # Orphan from a partial run — the sync interfaces are already
            # cabled (just not under this expected name). Leave it as-is
            # rather than creating a second, conflicting cable.
            return

        deployment_rel = getattr(dev_a, "deployment", None)
        deployment_id: str | None = None
        if deployment_rel is not None and deployment_rel.initialized:
            deployment_id = deployment_rel.peer.id

        self.logger.info(
            f"  [{ha_name}] Creating HA sync cable {cable_name}: "
            f"{dev_a.name.value}:{iface_a.name.value} ↔ {dev_b.name.value}:{iface_b.name.value}"
        )
        cable_data: dict[str, Any] = {
            "name": cable_name,
            "type": "smf",
            "endpoints": [iface_a.id, iface_b.id],
        }
        if deployment_id:
            cable_data["deployment"] = {"id": deployment_id}
        cable_obj = await self.client.create(kind=DcimCable, data=cable_data)
        await cable_obj.save(allow_upsert=True)

    async def _ensure_mlag_pairs(
        self,
        device_names: list[str],
        *,
        role_label: str,
        template: dict[str, Any],
        mlag_create: Literal["back-to-back", "virtual"],
        supports_virtual: bool = True,
    ) -> None:
        """Pair same-role devices two-at-a-time (sorted, odd one unpaired) into MLAG
        domains, per pod.mlag_create ("back-to-back" / "virtual"), then ensure
        each pair's peer-link interfaces/cable in the same call — no separate
        add_mlag generator/trigger round-trip for domain creation (that used
        to run async off a ManagedMLAG "created" mutation and fired once per
        domain from every concurrently-created ManagedMLAG — ~14 under a bulk
        topology regen — overloading the server). Mirrors
        _ensure_ha_pairs/_ensure_ha_interfaces exactly. MLAGGenerator/mlag.py
        still exists and delegates to the same ensure_mlag_wiring — it's only
        reached now by the two remaining ManagedMLAG "updated" triggers
        (capabilities/virtual_peer_link changed outside this flow, e.g. a
        direct API/UI edit or branch merge).

        back-to-back needs a role=mlag-peer interface on the template.
        virtual anchors on a loopback (ensure_mlag_wiring's
        _ensure_virtual_peer_link) — only L3/routed roles (leaf, access-leaf)
        can use it. supports_virtual=False (l2-leaf: no loopback, L2-only by
        design) forces back-to-back instead of erroring out — mlag_create is
        one pod-wide setting shared by every role, so a pod with both leaf
        (L3) and l2-leaf (L2-only) roles must fall back for the L2-only ones
        regardless of what's configured for the pod.
        """
        if len(device_names) < 2:
            return

        if mlag_create == "virtual" and not supports_virtual:
            self.logger.info(
                f"{role_label} has no routing/loopback — "
                f"using back-to-back MLAG instead of the pod's virtual setting (L2-only role)."
            )
            mlag_create = "back-to-back"

        if mlag_create == "back-to-back" and not any(
            iface.get("role") == "mlag-peer" for iface in template.get("interfaces", [])
        ):
            self.logger.error(
                f"template {template.get('id')} has no mlag-peer interface — "
                f"cannot create back-to-back MLAG for {role_label}s."
            )
            return

        mlag_group = None
        for pair_index, (first, second) in enumerate(pair_device_names(device_names), start=1):
            mlag_name = f"{first}-{second}-mlag"
            existing = await self.client.filters(kind=ManagedMLAG, name__value=mlag_name, include=["capabilities"])
            member_ids: list[str] | None = None
            if existing:
                mlag_obj = existing[0]
                self.client.group_context.related_node_ids.append(mlag_obj.id)
                # mlag_create may have changed since this domain was created (e.g.
                # back-to-back <-> virtual) — ensure_mlag_wiring's peer-link wiring
                # branches on this flag, so it must reflect the current setting, not
                # the one at creation time, or a re-run would silently keep wiring
                # the old mode.
                wants_virtual = mlag_create == "virtual"
                if mlag_obj.virtual_peer_link.value != wants_virtual:
                    mlag_obj.virtual_peer_link.value = wants_virtual
                    await mlag_obj.save(allow_upsert=True)
                    self.logger.info(f"Updated MLAG domain {mlag_name} to {mlag_create} peer-link")
            else:
                devices = await self.client.filters(kind=DcimPhysicalDevice, name__values=[first, second])
                if len(devices) != 2:
                    self.logger.error(f"MLAG pair {first}/{second}: could not resolve both devices.")
                    continue

                if mlag_group is None:
                    mlag_group = await self.client.get(kind=CoreStandardGroup, name__value="mlag_domains")
                mlag_obj = await self.client.create(
                    kind=ManagedMLAG,
                    data={
                        "name": mlag_name,
                        "domain_id": pair_index,
                        "virtual_peer_link": mlag_create == "virtual",
                        "status": "active",
                        "capabilities": [{"id": dev.id} for dev in devices],
                        "member_of_groups": [{"id": mlag_group.id}],
                    },
                )
                await mlag_obj.save(allow_upsert=True)
                self.logger.info(f"Created MLAG domain {mlag_name} ({mlag_create}) for {role_label}s")
                member_ids = [dev.id for dev in devices]

            await self.ensure_mlag_wiring(mlag_obj, mlag_name, member_ids=member_ids)
            await self._ensure_vlan_domain_pool(
                pool_owner_name=mlag_name, parent_kind="ManagedMLAG", parent_id=mlag_obj.id
            )

    async def _ensure_vlan_domain_pool(self, *, pool_owner_name: str, parent_kind: str, parent_id: str) -> None:
        """Create/upsert this VLAN domain's own local VLAN ID pool.

        IEEE 802.1Q VLAN ID has only local significance (within one L2
        domain — an MLAG pair, or a standalone device). Each VLAN domain
        gets its own independent 100-3999 pool so unrelated domains can
        reuse the same numeric VLAN ID for different segments; the real
        DC-wide/fabric-wide segment identifier is ManagedSegmentDeployment.vni.
        """
        await self.upsert_number_pool(
            pool_name=f"{pool_owner_name}-vlan-pool",
            description=f"Local VLAN ID pool for VLAN domain {pool_owner_name}",
            start_range=100,
            end_range=3999,
            node="ManagedVlanDomainSegment",
            node_attribute="vlan_id",
            parent_kind=parent_kind,
            parent_id=parent_id,
            parent_attr="vlan_pool",
        )
