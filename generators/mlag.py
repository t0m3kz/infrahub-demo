"""MLAG peer-link wiring mixin for CommonGenerator/MLAGGenerator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import logging

    from infrahub_sdk import InfrahubClient

from .helpers import get_lag_name, get_loopback_name
from .protocols import (
    DcimCable,
    DcimLAGInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    TopologyPod,
)

_PEER_LINK_LAG_ID = 100


class MLAGWiringMixin:
    """Mixin providing MLAG peer-link wiring, shared by DeviceMixin
    (synchronous, right after creating/finding a ManagedMLAG domain — no
    separate add_mlag generator round-trip for domain creation, closing a
    bulk-regen concurrency storm) and MLAGGenerator (generators/topology/
    mlag.py, off the two remaining ManagedMLAG "updated" triggers:
    capabilities/virtual_peer_link changed outside this flow, e.g. a direct
    API/UI edit or branch merge).

    Expects the host class to provide: ``client``, ``logger``.
    """

    client: InfrahubClient
    logger: logging.Logger

    async def ensure_mlag_wiring(self, mlag_obj: Any, mlag_name: str, *, member_ids: list[str] | None = None) -> None:
        """Wire peer-link interfaces (LAG or virtual loopback) for both
        devices in an MLAG domain. Every peer-link/cable is always
        created/upserted (existing id passed when found, full desired state
        resent every time) — mirrors create_devices()'s own device/loopback
        pattern: re-upserting unconditionally is what keeps a node inside
        this run's tracking group, so delete_unused_nodes doesn't remove it
        as "unused" on a run where nothing actually changed.

        member_ids lets a caller that already knows the two device ids (the
        _ensure_mlag_pairs path — it always has both devices in hand via
        create_devices()'s own batch) skip mlag_obj.capabilities.fetch()
        entirely. The MLAGGenerator trigger path (generators/topology/
        mlag.py, off a "capabilities changed"/"virtual_peer_link changed"
        update made outside this flow) doesn't have devices in hand, so it
        passes member_ids=None and lets this function fetch() them from
        mlag_obj.capabilities.
        """
        if member_ids is None:
            caps = getattr(mlag_obj, "capabilities")
            await caps.fetch()
            member_ids = [peer.id for peer in caps.peers]

        if len(member_ids) != 2:
            self.logger.error(f"[{mlag_name}] Expected exactly 2 peer devices in capabilities, got {len(member_ids)}")
            return

        virtual_peer_link = bool(getattr(mlag_obj, "virtual_peer_link").value)
        self.logger.info(
            f"Processing MLAG domain {mlag_name} ({'virtual' if virtual_peer_link else 'physical'} peer-link)"
        )

        member_devices = await self.client.filters(
            kind=DcimPhysicalDevice, ids=member_ids, include=["deployment", "platform"]
        )
        if len(member_devices) != 2:
            self.logger.error(f"[{mlag_name}] could not resolve both peer devices ({member_ids})")
            return

        peer_link_ifaces: list[Any] = []
        for device_obj in member_devices:
            await self._disconnect_stale_peer_link(device_obj, mlag_name, virtual_peer_link)

            platform_name = ""
            platform_rel = getattr(device_obj, "platform")
            if platform_rel.initialized:
                await platform_rel.fetch()
                platform_name = platform_rel.peer.name.value

            if virtual_peer_link:
                iface = await self._ensure_virtual_peer_link(device_obj, mlag_obj, mlag_name, platform_name)
            else:
                iface = await self._ensure_lag_peer_link(device_obj, mlag_obj, platform_name)
            if iface is not None:
                peer_link_ifaces.append(iface)

        dev_a, dev_b = member_devices[0], member_devices[1]
        if virtual_peer_link:
            await self._disconnect_stale_peer_link_cables(mlag_name, dev_a, dev_b)
        elif len(peer_link_ifaces) == 2:
            await self._ensure_peer_link_cables(mlag_name, dev_a, dev_b)

    async def _disconnect_stale_peer_link(self, device_obj: Any, mlag_name: str, virtual_peer_link: bool) -> None:
        """Delete the peer-link interface node left over from the OTHER mode
        when pod.mlag_create switched since this domain was last wired —
        physical (DcimLAGInterface) and virtual (DcimVirtualInterface
        loopback) peer-links are mutually exclusive representations of the
        same domain."""
        dev_name = device_obj.name.value
        stale_kind, stale_label = (
            (DcimLAGInterface, "DcimLAGInterface")
            if virtual_peer_link
            else (DcimVirtualInterface, "DcimVirtualInterface")
        )
        stale = await self.client.filters(kind=stale_kind, device__ids=[device_obj.id], role__value="mlag-peer")
        if stale:
            stale_obj = stale[0]
            self.logger.info(
                f"  [{dev_name}] Removing stale {stale_label} peer-link {stale_obj.name.value} "
                f"({mlag_name} switched to {'virtual' if virtual_peer_link else 'physical'})"
            )
            await self.client.delete(kind=stale_kind, id=stale_obj.id)

    async def _ensure_lag_peer_link(self, device_obj: Any, mlag_obj: Any, platform_name: str) -> Any | None:
        """Create or upsert a DcimLAGInterface peer-link, bundling ALL
        role=mlag-peer physical interfaces on this device as
        member_interfaces (unlike HA's single sync interface, an MLAG
        peer-link LAG can bundle more than one physical port). Always
        create()+save()'d with the full desired state (existing id passed
        when found) — never a conditional skip — so mlag_domain/
        interface_capabilities/member_interfaces self-heal on every run and
        the node stays inside this run's tracking group."""
        dev_name = device_obj.name.value
        lag_name = get_lag_name(platform_name, _PEER_LINK_LAG_ID)

        member_ifaces = await self.client.filters(
            kind=DcimPhysicalInterface, device__ids=[device_obj.id], role__value="mlag-peer"
        )
        if not member_ifaces:
            self.logger.error(
                f"[{dev_name}] No physical interfaces with role=mlag-peer found — cannot create peer-link LAG"
            )
            return None

        for iface in member_ifaces:
            if getattr(iface, "status").value == "active":
                continue
            getattr(iface, "status").value = "active"
            # update_group_context=False: a physical interface belongs to the device's
            # object_template, not to this generator run — never a delete_unused_nodes candidate.
            await iface.save(allow_upsert=True, update_group_context=False)

        existing_lags = await self.client.filters(
            kind=DcimLAGInterface, device__ids=[device_obj.id], role__value="mlag-peer"
        )
        existing_lag = existing_lags[0] if existing_lags else None

        lag_obj = await self.client.create(
            kind=DcimLAGInterface,
            data={
                **({"id": existing_lag.id} if existing_lag else {}),
                "name": lag_name,
                "device": {"id": device_obj.id},
                "status": "active",
                "role": "mlag-peer",
                "lag_id": _PEER_LINK_LAG_ID,
                "lacp_mode": "active",
                "mtu": 9000,
                "minimum_links": 1,
                "mlag_domain": {"id": mlag_obj.id},
                "interface_capabilities": [{"id": mlag_obj.id}],
                "member_interfaces": [{"id": iface.id} for iface in member_ifaces],
            },
        )
        await lag_obj.save(allow_upsert=True)
        self.logger.info(
            f"  [{dev_name}] {'Updated' if existing_lag else 'Created'} peer-link LAG {lag_name} ({lag_obj.id})"
        )
        return lag_obj

    async def _ensure_virtual_peer_link(
        self, device_obj: Any, mlag_obj: Any, mlag_name: str, platform_name: str
    ) -> Any | None:
        """Create or upsert a loopback virtual peer-link (single per
        device). Always create()+save()'d with the full desired state
        (existing id passed when found) — same self-healing/always-tracked
        rationale as _ensure_lag_peer_link."""
        dev_name = device_obj.name.value
        loopback_name = get_loopback_name(platform_name, 100)

        existing = await self.client.filters(
            kind=DcimVirtualInterface, device__ids=[device_obj.id], role__value="mlag-peer"
        )
        existing_virt = existing[0] if existing else None

        virt_obj = await self.client.create(
            kind=DcimVirtualInterface,
            data={
                **({"id": existing_virt.id} if existing_virt else {}),
                "name": loopback_name,
                "device": {"id": device_obj.id},
                "status": "active",
                "role": "mlag-peer",
                "description": f"MLAG virtual peer-link — {mlag_name}",
                "interface_capabilities": [{"id": mlag_obj.id}],
            },
        )
        await virt_obj.save(allow_upsert=True)
        self.logger.info(
            f"  [{dev_name}] {'Updated' if existing_virt else 'Created'} virtual peer-link "
            f"{loopback_name} ({virt_obj.id})"
        )
        return virt_obj

    async def _resolve_peer_link_deployment_id(self, device_obj: Any) -> str | None:
        """Peer-link cables are filed against the device's deployment's
        PARENT when that deployment is a TopologyPod (a pod-scoped
        leaf/tor's own deployment is the pod; the cable is recorded one
        level up, at the pod's containing room/DC deployment). Falls back
        to the deployment itself for any other deployment kind."""
        deployment_rel = getattr(device_obj, "deployment", None)
        if deployment_rel is None or not deployment_rel.initialized:
            return None
        if deployment_rel.typename == "TopologyPod":
            pod_obj = await self.client.get(kind=TopologyPod, id=deployment_rel.id, include=["parent"])
            parent_rel = getattr(pod_obj, "parent", None)
            if parent_rel is not None and parent_rel.initialized:
                return parent_rel.id
            return None
        return deployment_rel.id

    async def _ensure_peer_link_cables(self, mlag_name: str, dev_a: Any, dev_b: Any) -> None:
        """Create/upsert a DcimCable for each index-matched pair of sorted
        role=mlag-peer physical interfaces between the two devices
        (potentially more than one, unlike HA's single sync cable). Always
        create()+save()'d (existing/orphan id passed when found) — same
        self-healing/always-tracked rationale as the peer-link interfaces."""
        dev_a_name, dev_b_name = dev_a.name.value, dev_b.name.value
        deployment_id = await self._resolve_peer_link_deployment_id(dev_a)

        ifaces_a = sorted(
            await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=[dev_a.id], role__value="mlag-peer", include=["cable"]
            ),
            key=lambda i: i.name.value,
        )
        ifaces_b = sorted(
            await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=[dev_b.id], role__value="mlag-peer", include=["cable"]
            ),
            key=lambda i: i.name.value,
        )
        if not ifaces_a or not ifaces_b:
            return

        for idx, (iface_a, iface_b) in enumerate(zip(ifaces_a, ifaces_b), start=1):
            cable_name = f"CBL-{mlag_name}-PL{idx}"

            existing_cables = await self.client.filters(kind=DcimCable, name__value=cable_name)
            existing_cable = existing_cables[0] if existing_cables else None

            if existing_cable is None:
                # Adopt an orphan cable already connected to either interface
                # (e.g. left over from a rename) instead of creating a duplicate.
                cable_a, cable_b = getattr(iface_a, "cable", None), getattr(iface_b, "cable", None)
                orphan_id = None
                if cable_a is not None and cable_a.initialized:
                    orphan_id = cable_a.id
                elif cable_b is not None and cable_b.initialized:
                    orphan_id = cable_b.id
                if orphan_id:
                    existing_cable = await self.client.get(kind=DcimCable, id=orphan_id)

            cable_data: dict[str, Any] = {
                **({"id": existing_cable.id} if existing_cable else {}),
                "name": cable_name,
                "type": "smf",
                "endpoints": [iface_a.id, iface_b.id],
            }
            if deployment_id:
                cable_data["deployment"] = {"id": deployment_id}
            cable_obj = await self.client.create(kind=DcimCable, data=cable_data)
            await cable_obj.save(allow_upsert=True)
            self.logger.info(
                f"  [{mlag_name}] {'Updated' if existing_cable else 'Created'} {cable_name}: "
                f"{dev_a_name}:{iface_a.name.value} ↔ {dev_b_name}:{iface_b.name.value}"
            )

    async def _disconnect_stale_peer_link_cables(self, mlag_name: str, dev_a: Any, dev_b: Any) -> None:
        """Delete leftover physical peer-link cable(s) from a previous
        back-to-back run — pod.mlag_create switched to virtual, which has
        no cable of its own (the peer-link is a loopback)."""
        counts = []
        for dev in (dev_a, dev_b):
            ifaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=[dev.id], role__value="mlag-peer"
            )
            counts.append(len(ifaces))
        for idx in range(1, max(counts, default=0) + 1):
            cable_name = f"CBL-{mlag_name}-PL{idx}"
            existing = await self.client.filters(kind=DcimCable, name__value=cable_name)
            if existing:
                self.logger.info(f"  [{mlag_name}] Removing stale physical peer-link cable {cable_name}")
                await self.client.delete(kind=DcimCable, id=str(existing[0].id))
