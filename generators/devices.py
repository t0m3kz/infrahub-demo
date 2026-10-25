"""Device creation mixin for CommonGenerator."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, cast

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.protocols import CoreIPAddressPool

if TYPE_CHECKING:
    import logging

from utils.limits import ROLE_GROUP_NAMES

from .domain_pairing import DomainPairingMixin
from .helpers import DeviceNameContext, DeviceNamingConfig, get_loopback_name
from .protocols import (
    CloudNetworkAppliance,
    DcimPhysicalDevice,
    DcimVirtualDevice,
    DcimVirtualInterface,
    ManagedController,
)
from .types import DeviceOptions, NamingConvention
from .virtual_nics import VirtualNicMixin

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

# Switch roles that carry customer segments (servers cable into leaf/tor/
# l2-leaf/access-leaf, see generators/topology/endpoint.py; an office's
# access-switch is the fabric edge its hosts attach to) or act as an EVPN
# Multi-Site border gateway (border-leaf, edge — generators/topology/
# segment.py's _BORDER_GATEWAY_ROLES). A physical one that is not MLAG-paired
# is its own VLAN domain, created with the device — see
# _ensure_standalone_vlan_domains.
VLAN_DOMAIN_ROLES = frozenset({"leaf", "tor", "l2-leaf", "access-leaf", "access-switch", "border-leaf", "edge"})


# A generator query's role-bucketed controller aliases (see queries/topology/
# add/dc.gql), merged into one list by set_controllers_from.
_CONTROLLER_ALIASES = ("fabric_controllers", "security_manager_controllers", "lb_manager_controllers")

# firewall/load-balancer route to a controller by controller_type AND a
# platform match against the device's own template — a checkpoint_gaia
# firewall must never route to a Panorama (paloalto_panos) controller just because
# both happen to be controller_type=security_manager.
_ROLE_CONTROLLER_TYPE: dict[str, str] = {"firewall": "security_manager", "load-balancer": "lb_manager"}

# device_role -> HA node kind. Pairing itself happens inside create_devices()
# (DeviceOptions.ha_kind) — this only picks the right kind per role.
HA_KIND_BY_ROLE: dict[str, str] = {
    "firewall": "ManagedFirewallHA",
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
    """Peer id of a RelatedNode, or None when it is missing or unset.

    A RelatedNode wrapper is always truthy even when unset (no __bool__
    override), and its .peer property raises ValueError instead of returning
    None when neither id nor hfid is set. Check .initialized before touching
    .peer at all.
    """
    return rel.peer.id if rel is not None and rel.initialized else None


class DeviceMixin(DomainPairingMixin, VirtualNicMixin):
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
    # Set once per generator run via set_controllers_from() — see
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
        naming_convention: NamingConvention = "flat",
        options: DeviceOptions | None = None,
        *,
        hosting_device: Any | None = None,
    ) -> list[str]:
        """Create devices using batch creation.

        Uses self.fabric_name and self.pod_name (if set) from instance variables.
        See ``DeviceOptions`` for available option keys. The device owner comes from
        ``template["owner"]``; ``hosting_device`` places a virtual device on its host.
        """
        # Normalize options
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
            # Both are DC-level fabric tiers sharing one fabric-scoped loopback pool
            # (see dc.py's "dc-fabric-loopback" allocation) — neither is owned by
            # any one pod's own loopback pool.
            loopback_pool_name = f"{fabric_name}-dc-fabric-loopback-pool"
        else:
            # Other devices (spine, leaf, etc.) use pod-level loopback pool
            # device_prefix already includes fabric-pod combination when pod_name is present
            loopback_pool_name = f"{device_prefix}-loopback-pool"

        device_kind = CloudNetworkAppliance if appliance else DcimVirtualDevice if virtual else DcimPhysicalDevice

        owner_id = _object_id(template.get("owner"))
        hosting_device_id = _object_id(hosting_device)

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
            # include= loads the managed_devices peer ids with the controller, so
            # the attach below needs no fetch() (a re-get plus a full-node read
            # of every device the controller manages).
            controller = await self.client.get(
                kind=ManagedController, id=controller_info["id"], include=["managed_devices"]
            )

        device_group = None
        if controller is None:
            group_name = ROLE_GROUP_NAMES.get(device_role) or f"{device_role}s"
            # Keep generators robust on branches where bootstrap groups were not
            # loaded yet (e.g. ad-hoc test branches): a missing group is created.
            device_group = await self._standard_group(
                group_name, create_description=f"Auto-created by generator for role {device_role}"
            )
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
                existing_managed_ids = {peer.id for peer in controller.managed_devices.peers}
                missing_ids = [node.id for node in created_devices if node.id not in existing_managed_ids]
                # A RelationshipAdd of just these devices, not a save of the whole peer list: every
                # rack and pod of the deployments one controller manages attaches to it concurrently,
                # and a save lets the last writer drop the others' devices. It stays untracked: the
                # controller is data-loaded and shared, not this run's output.
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
        paired_names: set[str] = set()
        if mlag_create != "no":
            paired_names = await self._ensure_mlag_pairs(
                device_names,
                devices_by_name={node.name.value: node for node in created_devices},
                role_label=device_role,
                template=template,
                mlag_create=mlag_create,
                supports_virtual=options.get("mlag_supports_virtual", True),
            )

        if device_role in VLAN_DOMAIN_ROLES and not virtual:
            await self._ensure_standalone_vlan_domains(
                [node for node in created_devices if node.name.value not in paired_names]
            )

        return device_names

    async def create_ha_role_devices(
        self,
        *,
        role: Literal["firewall", "load-balancer"],
        entries: list[dict[str, Any]],
        deployment_id: str,
        naming_convention: NamingConvention,
        indexes: list[int],
        appliance_template_kind: str | None = None,
        appliance_options: DeviceOptions | None = None,
    ) -> list[tuple[dict[str, Any], list[str]]]:
        """Create HA-paired firewall/load-balancer devices for one fabric_templates role.

        Shared shape used by dc.py (DC-wide), pod.py (per-pod border-spine),
        and colocation_fabric.py (zone / virtual colocation on-ramp): each entry's devices are paired
        into an HA domain two-at-a-time by create_devices() itself (any
        quantity — an odd device is left unpaired). No loopback allocation —
        these appliances sit off the underlay/overlay, not in it.

        Returns (entry, created_names) per fabric_templates entry rather than
        one flat list, so a caller that needs per-entry follow-up (colocation_fabric.py
        registers virtual and physical entries apart) still has the entry at hand.

        appliance_template_kind: when given, an entry whose own
        template["template_kind"] matches it gets DeviceOptions.appliance=True
        plus ``appliance_options`` (its account)
        (colocation_fabric.py's on-ramp appliances can themselves be provider-hosted,
        unlike dc.py's/pod.py's, which are always physical).
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
        """Set ``self._all_controllers`` from a query's fabric_controllers/
        security_manager_controllers/lb_manager_controllers aliases — an
        absent or null alias contributes nothing."""
        self._all_controllers = [controller for alias in _CONTROLLER_ALIASES for controller in source.get(alias) or []]

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
        never routes to a Panorama (paloalto_panos) controller.

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
