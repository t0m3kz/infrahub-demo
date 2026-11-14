"""HA and MLAG domain pairing mixin: the domains DeviceMixin.create_devices() builds around the devices it creates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreNumberPool, CoreStandardGroup

if TYPE_CHECKING:
    import logging
    from generators.mixins.pools import PoolMixin

from generators.helpers.cable_attributes import cable_attributes, endpoint_label
from generators.helpers.pairing import pair_device_names
from generators.mixins.mlag import MLAGWiringMixin
from generators.protocols import (
    CloudNetworkAppliance,
    DcimCable,
    DcimInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    ManagedMLAG,
)
from utils.limits import (
    FIREWALL_HA_GROUP_MAX,
    FIREWALL_HA_GROUP_MIN,
    FIREWALL_HA_GROUP_POOL,
    GROUP_HA_DOMAINS,
    GROUP_MLAG_DOMAINS,
)
from utils.segment_vlan import mlag_peer_devices


class DomainPairingMixin(MLAGWiringMixin):
    """HA (firewall / load-balancer) and MLAG pairing for DeviceMixin.
    The host provides ``client``, ``logger``, ``deployment_id``, ``_resolve_pool`` and PoolMixin's ``resource_lock``."""

    # Attribute declarations for the type checker, provided by CommonGenerator / InfrahubGenerator / PoolMixin
    client: Any
    logger: logging.Logger
    # Set per run to the enclosing physical facility id (never a Pod or Rack). _ensure_ha_cable uses it for the HA-sync
    # cable's `deployment`, not the member's own, because DcimCable.deployment cannot be a TopologyPod and border-spine
    # pods scope their devices to the pod.
    deployment_id: str
    # Type checking only: aliases of the host's other bases, so nothing shadows them in the MRO
    if TYPE_CHECKING:
        resource_lock = PoolMixin.resource_lock

    async def _standard_group(self, name: str, *, create_description: str | None = None) -> Any:
        """The CoreStandardGroup called ``name``, read once per run.
        ``create_description``: create the group when missing; otherwise a missing group raises NodeNotFoundError."""
        memo: dict[str, Any] = self.__dict__.setdefault("_standard_group_memo", {})
        if name not in memo:
            try:
                memo[name] = await self.client.get(kind=CoreStandardGroup, name__value=name)
            except NodeNotFoundError:
                if create_description is None:
                    raise
                group = await self.client.create(
                    kind=CoreStandardGroup, data={"name": name, "description": create_description}
                )
                # Untracked: the role group is shared by every generator that creates this role
                await group.save(allow_upsert=True, update_group_context=False)
                self.logger.info(f"Created missing group '{name}'")
                memo[name] = group
        return memo[name]

    async def _ensure_ha_pairs(
        self,
        device_names: list[str],
        *,
        ha_kind: str,
        role_label: str,
        device_kind: type[Any] = DcimPhysicalDevice,
        ha_name: str | None = None,
    ) -> None:
        """Pair same-role devices two-at-a-time (sorted, odd one unpaired) into HA domains and ensure each pair's sync
        interfaces and cable. ``device_kind`` CloudNetworkAppliance means no sync interface or cable; ``ha_name``
        overrides `{first}-{second}`.
        """
        pairs = pair_device_names(device_names)
        if ha_name is not None and len(pairs) != 1:
            raise ValueError(f"ha_name {ha_name!r} names one pair, got {len(pairs)} from {device_names}")
        for first, second in pairs:
            name = ha_name or f"{first}-{second}"
            # The lookup and create sit in one per-pair lock, so overlapping runs do not both create the pair
            async with self.resource_lock(f"ha-domain-{name}"):
                existing = await self.client.filters(kind=ha_kind, name__value=name, include=["capabilities"])
                members: list[Any] | None = None
                if existing:
                    ha_obj = existing[0]
                    self.client.group_context.related_node_ids.append(ha_obj.id)
                    if (
                        ha_kind == "ManagedFirewallHA"
                        and getattr(getattr(ha_obj, "group_id", None), "value", None) is None
                    ):
                        ref = await self.client.create(
                            kind=ha_kind, data={"id": ha_obj.id, **await self._firewall_ha_group_data()}
                        )
                        await ref.save(allow_upsert=True)
                else:
                    devices = await self.client.filters(kind=device_kind, name__values=[first, second])
                    if len(devices) != 2:
                        self.logger.error(f"HA pair {first}/{second}: could not resolve both devices.")
                        continue

                    ha_group = await self._standard_group(GROUP_HA_DOMAINS)
                    ha_obj = await self.client.create(
                        kind=ha_kind,
                        data={
                            "name": name,
                            "status": "active",
                            "capabilities": [{"id": dev.id} for dev in devices],
                            "member_of_groups": [{"id": ha_group.id}],
                            **(await self._firewall_ha_group_data() if ha_kind == "ManagedFirewallHA" else {}),
                        },
                    )
                    await ha_obj.save(allow_upsert=True)
                    self.logger.info(f"Created HA domain {name} for {role_label}s")
                    members = devices

            if device_kind is not CloudNetworkAppliance:
                await self._ensure_ha_interfaces(ha_obj, name, device_kind=device_kind, members=members)

    async def _firewall_ha_group_data(self) -> dict[str, Any]:
        """Allocate missing firewall HA IDs natively; authored IDs are never rewritten.
        The pool indexes authored values too, so a new virtual pair cannot reuse a physical pair's ID."""
        async with self.resource_lock("firewall-ha-group-pool"):
            try:
                pool = await self.client.get(kind=CoreNumberPool, name__value=FIREWALL_HA_GROUP_POOL)
            except NodeNotFoundError:
                pool = await self.client.create(
                    kind=CoreNumberPool,
                    data={
                        "name": FIREWALL_HA_GROUP_POOL,
                        "node": "ManagedFirewallHA",
                        "node_attribute": "group_id",
                        "start_range": FIREWALL_HA_GROUP_MIN,
                        "end_range": FIREWALL_HA_GROUP_MAX,
                    },
                )
                await pool.save(allow_upsert=True, update_group_context=False)
        return {"group_id": {"from_pool": {"id": pool.id}}}

    async def _ensure_ha_interfaces(
        self,
        ha_obj: Any,
        ha_name: str,
        *,
        device_kind: type[Any] = DcimPhysicalDevice,
        members: list[Any] | None = None,
    ) -> None:
        """Ensure each member has an active HA sync interface and, for physical pairs, a DcimCable between them
        (idempotent). ``members`` lets a caller skip re-reading devices it holds; otherwise they come from ha_obj's
        capabilities.
        """
        member_devices = members if members is not None else await self._ha_member_devices(ha_obj, device_kind)
        if not member_devices:
            return
        member_ids = [device_obj.id for device_obj in member_devices]

        is_physical = device_kind is DcimPhysicalDevice

        # One query for every member's sync interfaces, grouped by device
        if is_physical:
            ha_ifaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=member_ids, role__value="ha", include=["cable"]
            )
        else:
            # The generic kind: a virtual device's NICs are DcimVirtualInterface, a physical one's DcimPhysicalInterface
            ha_ifaces = await self.client.filters(kind=DcimInterface, device__ids=member_ids, role__value="ha")
        first_ha_iface_by_device: dict[str, Any] = {}
        for iface in ha_ifaces:
            # device is a mandatory Parent relationship (schemas/base/dcim.yml)
            first_ha_iface_by_device.setdefault(iface.device.id, iface)

        sync_ifaces: list[Any] = []
        for device_obj in member_devices:
            sync_iface = first_ha_iface_by_device.get(device_obj.id)
            if sync_iface is None:
                self.logger.error(f"[{device_obj.name.value}] No HA sync interface found (expected role=ha)")
                continue
            sync_ifaces.append((device_obj, sync_iface))

            # A sync port is in service, so it must not stay at the `free` default (as create_connections() does for
            # cable ends); runs every call so older pairs self-heal. update_group_context=False: the port belongs to the
            # device's object_template, not this run.
            if getattr(sync_iface, "status").value != "active":
                getattr(sync_iface, "status").value = "active"
                await sync_iface.save(allow_upsert=True, update_group_context=False)

        if is_physical and len(sync_ifaces) == 2:
            await self._ensure_ha_cable(ha_name, sync_ifaces)

    async def _ha_member_devices(self, ha_obj: Any, device_kind: type[Any]) -> list[Any]:
        """The ``device_kind`` members of an HA domain, read with ``include=["capabilities"]``.
        Peers resolve from the client store; if any cannot, one ``filters`` by id returns the members instead."""
        peers = list(getattr(ha_obj, "capabilities").peers)
        if not peers:
            return []
        try:
            resolved = [peer.peer for peer in peers if peer.typename == device_kind.__name__]
            if len(resolved) == len(peers) and all(isinstance(device_obj.name.value, str) for device_obj in resolved):
                return resolved
        except (NodeNotFoundError, ValueError, AttributeError):
            pass
        return await self.client.filters(kind=device_kind, ids=[peer.id for peer in peers])

    async def _ensure_ha_cable(self, ha_name: str, sync_ifaces: list[tuple[Any, Any]]) -> None:
        """Create the DcimCable between two peer devices' HA sync interfaces (physical HA pairs only)."""
        (dev_a, iface_a), (dev_b, iface_b) = sorted(sync_ifaces, key=lambda pair: pair[0].name.value)
        cable_name = f"CBL-{ha_name}-SYNC"

        existing = await self.client.filters(kind=DcimCable, name__value=cable_name)
        if existing:
            # Keep it in this run's tracking group, or the next run deletes it as unused and the cable oscillates
            self.client.group_context.related_node_ids.append(existing[0].id)
            return

        existing_cable_a = getattr(iface_a, "cable", None)
        existing_cable_b = getattr(iface_b, "cable", None)
        if (existing_cable_a is not None and existing_cable_a.initialized) or (
            existing_cable_b is not None and existing_cable_b.initialized
        ):
            # Orphan from a partial run: the sync interfaces are already cabled under another name, so leave it
            return

        # self.deployment_id, not dev_a's own `deployment`: border-spine pods scope devices to the pod, which the
        # cable's relationship refuses
        deployment_id = getattr(self, "deployment_id", None)

        self.logger.info(
            f"  [{ha_name}] Creating HA sync cable {cable_name}: "
            f"{dev_a.name.value}:{iface_a.name.value} ↔ {dev_b.name.value}:{iface_b.name.value}"
        )
        layouts = await self._cable_layouts([dev_a.id, dev_b.id])
        cable_data: dict[str, Any] = {
            "name": cable_name,
            "type": "smf",
            "endpoints": [iface_a.id, iface_b.id],
            **cable_attributes(
                "smf",
                [
                    endpoint_label(dev_a.name.value, iface_a.name.value),
                    endpoint_label(dev_b.name.value, iface_b.name.value),
                ],
                layouts.get(dev_a.id),
                layouts.get(dev_b.id),
            ),
        }
        if deployment_id:
            cable_data["deployment"] = {"id": deployment_id}
        cable_obj = await self.client.create(kind=DcimCable, data=cable_data)
        await cable_obj.save(allow_upsert=True)

    async def _ensure_mlag_pairs(
        self,
        device_names: list[str],
        *,
        devices_by_name: dict[str, Any],
        role_label: str,
        template: dict[str, Any],
        mlag_create: Literal["back-to-back", "virtual"],
        supports_virtual: bool = True,
    ) -> None:
        """Pair same-role devices two-at-a-time into MLAG domains per pod.mlag_create, then ensure the peer-link
        interfaces and cable. ``supports_virtual=False`` (l2-leaf) forces back-to-back; an unpaired device is its own
        VLAN domain. Mirrors _ensure_ha_pairs.
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

        for pair_index, (first, second) in enumerate(pair_device_names(device_names), start=1):
            mlag_name = f"{first}-{second}-mlag"
            first_dev, second_dev = devices_by_name.get(first), devices_by_name.get(second)
            if first_dev is None or second_dev is None:
                self.logger.error(f"MLAG pair {first}/{second}: could not resolve both devices.")
                continue

            mlag_group = await self._standard_group(GROUP_MLAG_DOMAINS)

            # The lookup and create sit in one per-domain lock (released before ensure_mlag_wiring takes its own), as in
            # _ensure_ha_pairs
            async with self.resource_lock(f"mlag-domain-{mlag_name}"):
                # Pass the existing id so the upsert matches by ID, not HFID
                existing = await self.client.filters(kind=ManagedMLAG, name__value=mlag_name)
                existing_mlag = existing[0] if existing else None
                mlag_obj = await self.client.create(
                    kind=ManagedMLAG,
                    data={
                        **({"id": existing_mlag.id} if existing_mlag else {}),
                        "name": mlag_name,
                        "peer_devices": mlag_peer_devices([first, second]),
                        "domain_id": pair_index,
                        "virtual_peer_link": mlag_create == "virtual",
                        "status": "active",
                        "capabilities": [{"id": first_dev.id}, {"id": second_dev.id}],
                        "member_of_groups": [{"id": mlag_group.id}],
                    },
                )
                await mlag_obj.save(allow_upsert=True)
            self.logger.info(
                f"{'Updated' if existing_mlag else 'Created'} MLAG domain {mlag_name} ({mlag_create}) for {role_label}s"
            )

            await self.ensure_mlag_wiring(mlag_obj, mlag_name, member_ids=[first_dev.id, second_dev.id])
