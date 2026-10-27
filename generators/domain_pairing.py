"""HA and MLAG domain pairing mixin: the domains DeviceMixin.create_devices() builds around the devices it creates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreStandardGroup

if TYPE_CHECKING:
    import logging
    from .pools import PoolMixin

from utils.limits import GROUP_HA_DOMAINS, GROUP_MLAG_DOMAINS

from .helpers.cable_attributes import cable_attributes, endpoint_label
from .helpers.pairing import pair_device_names
from .mlag import MLAGWiringMixin
from .protocols import (
    CloudNetworkAppliance,
    DcimCable,
    DcimInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    ManagedMLAG,
)


class DomainPairingMixin(MLAGWiringMixin):
    """Mixin providing HA (firewall / load-balancer) and MLAG pairing for DeviceMixin.

    Expects the host class to provide: ``client``, ``logger``, ``deployment_id``,
    ``_resolve_pool`` and PoolMixin's ``resource_lock``.
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator / PoolMixin
    client: Any
    logger: logging.Logger
    # Set once per generator run (dc.py/pod.py/rack.py/colocation_fabric.py/endpoint.py's generate()) to the
    # enclosing physical facility's id (TopologyDataCenter/TopologyColocationZone — never a Pod or Rack).
    # _ensure_ha_cable reads this for the HA-sync cable's own `deployment`, deliberately NOT the HA member
    # device's own `deployment` relationship: pod.py's border-spine pods scope their firewall/load-balancer
    # *devices* to the pod on purpose (see test_pod_border_services.py), but DcimCable.deployment's peer
    # type has no TopologyPod — confirmed live on DC7.
    deployment_id: str
    # Type checking only: the real members of the host's other bases, aliased so nothing is defined at runtime and
    # nothing shadows them in the MRO.
    if TYPE_CHECKING:
        resource_lock = PoolMixin.resource_lock

    async def _standard_group(self, name: str, *, create_description: str | None = None) -> Any:
        """The CoreStandardGroup called ``name``, read once per run.

        ``create_description``: create the group when it is missing (a branch whose
        bootstrap groups were not loaded yet, e.g. an ad-hoc test branch); without
        it a missing group raises NodeNotFoundError.
        """
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
                # Untracked: the role group is shared by every generator that
                # creates this role. Tracked, the creating run would delete it
                # on its next run, which finds it and does not save it again.
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
        """Pair same-role devices two-at-a-time (sorted, odd one unpaired) into
        HA domains, then ensure each pair's HA sync interfaces/cable in the
        same call — no separate add_ha generator/trigger round-trip
        (that used to run async off a ManagedFirewallHA/LoadbalancerHA "updated"
        mutation, and re-fired 2-3x per domain whenever two concurrent callers
        raced on the filter-then-create check below, since every racer's
        allow_upsert=True save was itself an "updated" mutation). Shared by
        dc.py (DC-wide firewall/load-balancer) and pod.py (a border-spine pod's
        own firewall/load-balancer) — never pairs across pods/DCs, both members
        must sit in front of the same fabric to mean anything physically.
        device_kind is CloudNetworkAppliance for a virtual colocation's provider-hosted firewall
        pair (the provider wires it, so it gets no sync interface and no cable)
        and DcimVirtualDevice for the shared production/
        non-production virtual instances dc.py provisions per physical HA
        pair — same pairing logic, no physical cabling involved (mirrors the
        skip _ensure_ha_interfaces itself applies for non-physical devices).
        ha_name overrides the default `{first}-{second}` for a single pair (a customer's dedicated pair is named
        from the owner, not from its two member names).
        An HA domain carries no tenant, not even a customer's dedicated pair:
        the tenant is recorded one level down, on the dedicated
        ManagedFirewallContext/ManagedLoadbalancerContext on it."""
        pairs = pair_device_names(device_names)
        if ha_name is not None and len(pairs) != 1:
            raise ValueError(f"ha_name {ha_name!r} names one pair, got {len(pairs)} from {device_names}")
        for first, second in pairs:
            name = ha_name or f"{first}-{second}"
            # Two overlapping runs (e.g. add_dc and dc_pod_cascade) can both
            # find no pair and both create it: the lookup and the create sit
            # inside one per-pair lock, so the second finds the first's domain.
            async with self.resource_lock(f"ha-domain-{name}"):
                existing = await self.client.filters(kind=ha_kind, name__value=name, include=["capabilities"])
                members: list[Any] | None = None
                if existing:
                    ha_obj = existing[0]
                    self.client.group_context.related_node_ids.append(ha_obj.id)
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
                        },
                    )
                    await ha_obj.save(allow_upsert=True)
                    self.logger.info(f"Created HA domain {name} for {role_label}s")
                    members = devices

            if device_kind is not CloudNetworkAppliance:
                await self._ensure_ha_interfaces(ha_obj, name, device_kind=device_kind, members=members)

    async def _ensure_ha_interfaces(
        self,
        ha_obj: Any,
        ha_name: str,
        *,
        device_kind: type[Any] = DcimPhysicalDevice,
        members: list[Any] | None = None,
    ) -> None:
        """Ensure each member device has an active HA sync interface, and (for
        physical pairs only) a DcimCable between the two sync interfaces.
        Idempotent (see _ensure_ha_pairs's docstring for why that matters —
        this used to be a separate generator invoked via trigger,
        unconditionally create()-ing on every run).

        members lets a caller that already holds the two device nodes (the
        freshly-created-domain path in _ensure_ha_pairs) skip reading them
        again; otherwise they come from ha_obj's capabilities
        (_ha_member_devices)."""
        member_devices = members if members is not None else await self._ha_member_devices(ha_obj, device_kind)
        if not member_devices:
            return
        member_ids = [device_obj.id for device_obj in member_devices]

        is_physical = device_kind is DcimPhysicalDevice

        # One query for every member's sync interfaces, grouped by device. The
        # result keeps one sort order, so each device's first one is the same
        # as a per-device query's.
        if is_physical:
            ha_ifaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=member_ids, role__value="ha", include=["cable"]
            )
        else:
            # The generic kind: a virtual device's NICs are DcimVirtualInterface, a physical one's
            # DcimPhysicalInterface.
            ha_ifaces = await self.client.filters(kind=DcimInterface, device__ids=member_ids, role__value="ha")
        first_ha_iface_by_device: dict[str, Any] = {}
        for iface in ha_ifaces:
            # device is a mandatory Parent relationship (schemas/base/dcim.yml).
            first_ha_iface_by_device.setdefault(iface.device.id, iface)

        sync_ifaces: list[Any] = []
        for device_obj in member_devices:
            sync_iface = first_ha_iface_by_device.get(device_obj.id)
            if sync_iface is None:
                self.logger.error(f"[{device_obj.name.value}] No HA sync interface found (expected role=ha)")
                continue
            sync_ifaces.append((device_obj, sync_iface))

            # A port carrying an HA domain's sync link is in service, so it must
            # not stay at DcimInterface's `free` default — same flip, and same
            # reason, as create_connections() applies to both ends of every cable
            # it lays and _ensure_mlag_pairs applies to the peer-link members.
            # Runs on every call, so a pair created before this was fixed
            # self-heals on its next run.
            #
            # update_group_context=False: a physical sync port belongs to the
            # device's object_template, not to this generator run — it must never
            # become a delete_unused_nodes candidate.
            if getattr(sync_iface, "status").value != "active":
                getattr(sync_iface, "status").value = "active"
                await sync_iface.save(allow_upsert=True, update_group_context=False)

        if is_physical and len(sync_ifaces) == 2:
            await self._ensure_ha_cable(ha_name, sync_ifaces)

    async def _ha_member_devices(self, ha_obj: Any, device_kind: type[Any]) -> list[Any]:
        """The ``device_kind`` members of an HA domain read with ``include=["capabilities"]``.

        That query puts each peer, with its attributes, in the client store, so
        the peers resolve with no read. When any peer cannot be resolved there (no
        typename, not in the store, no name) or is not a ``device_kind``, one
        ``filters`` by id returns the members instead.
        """
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
        """Create the DcimCable between two peer devices' HA sync interfaces —
        physical HA pairs only, called once both member interfaces are known."""
        (dev_a, iface_a), (dev_b, iface_b) = sorted(sync_ifaces, key=lambda pair: pair[0].name.value)
        cable_name = f"CBL-{ha_name}-SYNC"

        existing = await self.client.filters(kind=DcimCable, name__value=cable_name)
        if existing:
            # Keep it in this run's tracking group: a generator body runs inside
            # client.start_tracking(delete_unused_nodes=True), so a node from an
            # earlier run that this run does not touch is deleted as unused.
            # Returning without registering made the cable oscillate: created on
            # one run, deleted as unused at the end of the next.
            self.client.group_context.related_node_ids.append(existing[0].id)
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

        # self.deployment_id, not dev_a's own `deployment` relationship — see
        # the attribute declaration above for why. This happened to give the
        # right answer for every DC-scoped HA pair (dc.py's firewall/
        # load-balancer devices are DC-scoped, so the two ids coincide) but
        # broke on DC7's border-spine pods, whose devices are deliberately
        # pod-scoped: "TopologyPod ... cannot be added to relationship, must
        # be of type: [...TopologyDataCenter]".
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

        devices_by_name is create_devices()'s own created_devices batch,
        keyed by name — every device in device_names was just created/
        upserted there unconditionally, so a fresh MLAG domain's
        capabilities never need a second client.filters() round-trip to
        re-resolve devices already sitting in hand.

        back-to-back needs a role=mlag-peer interface on the template.
        virtual anchors on a loopback (ensure_mlag_wiring's
        _ensure_virtual_peer_link) — only L3/routed roles (leaf, access-leaf)
        can use it. supports_virtual=False (l2-leaf: no loopback, L2-only by
        design) forces back-to-back instead of erroring out — mlag_create is
        one pod-wide setting shared by every role, so a pod with both leaf
        (L3) and l2-leaf (L2-only) roles must fall back for the L2-only ones
        regardless of what's configured for the pod.

        A device left unpaired is its own VLAN domain (no record is created for it).
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

            # Two overlapping runs (a rack's inline run and its own trigger) can
            # both find no domain and both create it: the lookup and the create
            # sit inside one per-domain lock, like _ensure_ha_pairs. Released
            # before ensure_mlag_wiring takes its own mlag-wiring-{id} lock, so
            # the two are never held together.
            async with self.resource_lock(f"mlag-domain-{mlag_name}"):
                # Pass existing id so upsert matches by ID, not hfid lookup — mirrors
                # create_devices()'s own upsert pattern.
                existing = await self.client.filters(kind=ManagedMLAG, name__value=mlag_name)
                existing_mlag = existing[0] if existing else None
                mlag_obj = await self.client.create(
                    kind=ManagedMLAG,
                    data={
                        **({"id": existing_mlag.id} if existing_mlag else {}),
                        "name": mlag_name,
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
