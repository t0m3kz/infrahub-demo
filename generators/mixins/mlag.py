"""MLAG peer-link wiring mixin for CommonGenerator/MLAGGenerator."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreIPPrefixPool

if TYPE_CHECKING:
    import logging

    from infrahub_sdk import InfrahubClient
    from generators.mixins.connections import CablingMixin
    from generators.mixins.pools import PoolMixin

from generators.helpers import get_lag_name, get_loopback_name
from generators.helpers.cable_attributes import cable_attributes, endpoint_label
from generators.mixins.connections import tracked_save_kwargs
from generators.protocols import (
    DcimCable,
    DcimLAGInterface,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    TopologyPod,
)
from utils.limits import MLAG_CONTROL_IPV4_POOL, MLAG_CONTROL_IPV6_POOL

_PEER_LINK_LAG_ID = 100
_CONTROL_SVI_NAME = "Vlan4094"

# The MLAG control session runs between the peers only and is never routed, so its family is independent of the
# underlay. SONiC MC-LAG (ICCP) accepts IPv4 only; others use IPv6.
_IPV4_CONTROL_PLATFORMS = frozenset({"sonic", "dell_sonic"})
# vPC has no routed control session; its peer-keepalive runs over management
_NO_CONTROL_PLATFORMS = frozenset({"cisco_nxos"})
# family -> (pool name, peer-link prefix length, virtual loopback prefix length)
_CONTROL_POOLS: dict[int, tuple[str, int, int]] = {
    4: (MLAG_CONTROL_IPV4_POOL, 31, 32),
    6: (MLAG_CONTROL_IPV6_POOL, 127, 128),
}


class MLAGWiringMixin:
    """MLAG peer-link wiring, shared by DeviceMixin (right after creating a ManagedMLAG domain) and MLAGGenerator
    (generators/topology/mlag.py, for ManagedMLAG "updated" triggers). The host provides ``client``, ``logger``,
    CablingMixin's ``upsert_p2p_addresses`` and ``_cable_layouts`` and PoolMixin's ``resource_lock``.
    """

    client: InfrahubClient
    logger: logging.Logger
    # Type checking only: aliases of the host's other bases, so nothing shadows them in the MRO
    if TYPE_CHECKING:
        _cable_layouts = CablingMixin._cable_layouts
        resource_lock = PoolMixin.resource_lock
        upsert_p2p_addresses = CablingMixin.upsert_p2p_addresses

    async def ensure_mlag_wiring(
        self, mlag_obj: Any, mlag_name: str, *, member_ids: list[str], track: bool = True
    ) -> None:
        """Wire one MLAG domain, serialized per domain. ``track``: whether this run owns the wiring.
        DeviceMixin._ensure_mlag_pairs does; MLAGGenerator passes track=False and writes untracked, or its cleanup would
        remove a peer-link it stopped reaching. The lock stops two runs creating the same control address.
        """
        async with self.resource_lock(f"mlag-wiring-{mlag_obj.id}"):
            await self._ensure_mlag_wiring_locked(mlag_obj, mlag_name, member_ids=member_ids, track=track)

    async def _ensure_mlag_wiring_locked(
        self, mlag_obj: Any, mlag_name: str, *, member_ids: list[str], track: bool = True
    ) -> None:
        """Wire peer-link interfaces (LAG or virtual loopback) for both devices of an MLAG domain.
        Every peer-link/cable is re-upserted each run (existing id passed), which keeps it in the tracking group.
        ``member_ids``: the two device ids. The pair's interfaces are read once per kind before any write."""
        if len(member_ids) != 2:
            self.logger.error(f"[{mlag_name}] Expected exactly 2 peer devices in capabilities, got {len(member_ids)}")
            return

        virtual_peer_link = bool(getattr(mlag_obj, "virtual_peer_link").value)
        self.logger.info(
            f"Processing MLAG domain {mlag_name} ({'virtual' if virtual_peer_link else 'physical'} peer-link)"
        )

        # include= puts the platform in the client store, so platform.peer resolves without a fetch()
        member_devices = await self.client.filters(
            kind=DcimPhysicalDevice, ids=member_ids, include=["deployment", "platform"]
        )
        if len(member_devices) != 2:
            self.logger.error(f"[{mlag_name}] could not resolve both peer devices ({member_ids})")
            return

        platform_by_id: dict[str, str] = {}
        for device_obj in member_devices:
            platform_rel = getattr(device_obj, "platform")
            platform_by_id[device_obj.id] = platform_rel.peer.name.value if platform_rel.initialized else ""

        control_ip_ids = await self._allocate_control_ips(
            mlag_obj, mlag_name, member_devices, set(platform_by_id.values()), virtual_peer_link, track=track
        )

        device_ids = [device_obj.id for device_obj in member_devices]
        # Physical and virtual peer-links are mutually exclusive: the one for the other mode is stale
        peer_link_kind, stale_kind = (
            (DcimVirtualInterface, DcimLAGInterface) if virtual_peer_link else (DcimLAGInterface, DcimVirtualInterface)
        )
        stale_peer_links = await self._interfaces_by_device(stale_kind, device_ids, "mlag-peer")
        existing_peer_links = await self._interfaces_by_device(peer_link_kind, device_ids, "mlag-peer")
        peer_ports = await self._interfaces_by_device(DcimPhysicalInterface, device_ids, "mlag-peer", include=["cable"])
        existing_svis = (
            await self._interfaces_by_device(DcimVirtualInterface, device_ids, "mlag-control")
            if control_ip_ids and not virtual_peer_link
            else {}
        )

        peer_link_ifaces: list[Any] = []
        # The domain lists every interface it configures: the peer-link, the physical mlag-peer ports and the control
        # SVIs
        domain_ifaces: list[Any] = [port for ports in peer_ports.values() for port in ports]
        for device_obj in member_devices:
            await self._disconnect_stale_peer_link(
                device_obj, stale_peer_links[device_obj.id], mlag_name, virtual_peer_link
            )

            platform_name = platform_by_id[device_obj.id]
            control_ip_id = control_ip_ids.get(device_obj.id)
            existing_peer_link = next(iter(existing_peer_links[device_obj.id]), None)
            if virtual_peer_link:
                iface = await self._ensure_virtual_peer_link(
                    device_obj, mlag_name, platform_name, existing_peer_link, control_ip_id, track=track
                )
            else:
                iface = await self._ensure_lag_peer_link(
                    device_obj, mlag_obj, platform_name, peer_ports[device_obj.id], existing_peer_link, track=track
                )
                if control_ip_id:
                    control_svi = await self._ensure_control_svi(
                        device_obj,
                        mlag_name,
                        control_ip_id,
                        next(iter(existing_svis[device_obj.id]), None),
                        track=track,
                    )
                    domain_ifaces.append(control_svi)
            if iface is not None:
                peer_link_ifaces.append(iface)
                domain_ifaces.append(iface)

        await self._list_domain_interfaces(mlag_obj, mlag_name, domain_ifaces)

        dev_a, dev_b = member_devices[0], member_devices[1]
        if virtual_peer_link:
            await self._disconnect_stale_peer_link_cables(mlag_name, max(len(ports) for ports in peer_ports.values()))
        elif len(peer_link_ifaces) == 2:
            await self._ensure_peer_link_cables(
                mlag_name, dev_a, dev_b, peer_ports[dev_a.id], peer_ports[dev_b.id], track=track
            )

    async def _list_domain_interfaces(self, mlag_obj: Any, mlag_name: str, interfaces: list[Any]) -> None:
        """List the interfaces the domain configures on its ``interfaces`` edge.
        One additive RelationshipAdd, untracked; stale nodes are deleted above and their edges go with them."""
        if not interfaces:
            return
        await mlag_obj.add_relationships(relation_to_update="interfaces", related_nodes=[i.id for i in interfaces])
        self.logger.info(f"  [{mlag_name}] Listed {len(interfaces)} interface(s) on the domain")

    async def _interfaces_by_device(
        self, kind: type[Any], device_ids: list[str], role: str, **kwargs: Any
    ) -> dict[str, list[Any]]:
        """The ``role`` interfaces of ``kind`` on ``device_ids``, grouped by device id, in one query."""
        by_device: dict[str, list[Any]] = {device_id: [] for device_id in device_ids}
        for iface in await self.client.filters(kind=kind, device__ids=device_ids, role__value=role, **kwargs):
            by_device[getattr(iface, "device").id].append(iface)
        return by_device

    async def _disconnect_stale_peer_link(
        self, device_obj: Any, stale: list[Any], mlag_name: str, virtual_peer_link: bool
    ) -> None:
        """Delete the peer-link left over from the OTHER mode when pod.mlag_create switched; ``stale`` is the device's
        role=mlag-peer interfaces of that kind.
        """
        if not stale:
            return
        stale_kind, stale_label = (
            (DcimLAGInterface, "DcimLAGInterface")
            if virtual_peer_link
            else (DcimVirtualInterface, "DcimVirtualInterface")
        )
        stale_obj = stale[0]
        self.logger.info(
            f"  [{device_obj.name.value}] Removing stale {stale_label} peer-link {stale_obj.name.value} "
            f"({mlag_name} switched to {'virtual' if virtual_peer_link else 'physical'})"
        )
        await self.client.delete(kind=stale_kind, id=stale_obj.id)

    async def _ensure_lag_peer_link(
        self,
        device_obj: Any,
        mlag_obj: Any,
        platform_name: str,
        member_ifaces: list[Any],
        existing_lag: Any | None,
        *,
        track: bool = True,
    ) -> Any | None:
        """Create or upsert a DcimLAGInterface peer-link bundling ALL role=mlag-peer physical interfaces
        (``member_ifaces``). Always saved with the full desired state (``existing_lag``'s id passed) so it self-heals
        and stays tracked.
        """
        dev_name = device_obj.name.value
        lag_name = get_lag_name(platform_name, _PEER_LINK_LAG_ID)

        if not member_ifaces:
            self.logger.error(
                f"[{dev_name}] No physical interfaces with role=mlag-peer found — cannot create peer-link LAG"
            )
            return None

        for iface in member_ifaces:
            if getattr(iface, "status").value == "active":
                continue
            getattr(iface, "status").value = "active"
            # update_group_context=False: a physical interface belongs to the object_template, not this run
            await iface.save(allow_upsert=True, update_group_context=False)

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
                "member_interfaces": [{"id": iface.id} for iface in member_ifaces],
            },
        )
        await lag_obj.save(**tracked_save_kwargs(track))
        self.logger.info(
            f"  [{dev_name}] {'Updated' if existing_lag else 'Created'} peer-link LAG {lag_name} ({lag_obj.id})"
        )
        return lag_obj

    async def _ensure_virtual_peer_link(
        self,
        device_obj: Any,
        mlag_name: str,
        platform_name: str,
        existing_virt: Any | None,
        control_ip_id: str | None = None,
        *,
        track: bool = True,
    ) -> Any | None:
        """Create or upsert a loopback virtual peer-link (one per device), always saved like _ensure_lag_peer_link
        (``existing_virt``'s id passed).
        """
        dev_name = device_obj.name.value
        loopback_name = get_loopback_name(platform_name, 100)

        virt_obj = await self.client.create(
            kind=DcimVirtualInterface,
            data={
                **({"id": existing_virt.id} if existing_virt else {}),
                "name": loopback_name,
                "device": {"id": device_obj.id},
                "status": "active",
                "role": "mlag-peer",
                "description": f"MLAG virtual peer-link — {mlag_name}",
                **({"ip_address": {"id": control_ip_id}} if control_ip_id else {}),
            },
        )
        await virt_obj.save(**tracked_save_kwargs(track))
        self.logger.info(
            f"  [{dev_name}] {'Updated' if existing_virt else 'Created'} virtual peer-link "
            f"{loopback_name} ({virt_obj.id})"
        )
        return virt_obj

    async def _allocate_control_ips(
        self,
        mlag_obj: Any,
        mlag_name: str,
        member_devices: list[Any],
        platforms: set[str],
        virtual_peer_link: bool,
        *,
        track: bool = True,
    ) -> dict[str, str]:
        """Allocate the MLAG control-session addresses and return {device_id: IpamIPAddress id}. One /31 (SONiC) or /127
        per domain, stable per domain id; the lower address goes to the device sorting first by name. A virtual
        peer-link carries it on its loopback (/32, /128). Returns {} for vPC or an unloaded pool, and removes a stale
        control SVI.
        """
        if platforms & _NO_CONTROL_PLATFORMS:
            await self._disconnect_stale_control_svis(member_devices, mlag_name)
            return {}

        family = 4 if platforms & _IPV4_CONTROL_PLATFORMS else 6
        pool_name, prefix_length, loopback_length = _CONTROL_POOLS[family]
        # Control pools are static bootstrap data read once per run; a missing pool is not remembered
        pool_memo: dict[str, Any] = self.__dict__.setdefault("_control_pool_memo", {})
        pool = pool_memo.get(pool_name)
        if pool is None:
            pools = await self.client.filters(kind=CoreIPPrefixPool, name__value=pool_name)
            if not pools:
                self.logger.warning(f"[{mlag_name}] Pool {pool_name} not found — skipping MLAG control addressing")
                return {}
            pool = pool_memo[pool_name] = pools[0]

        if virtual_peer_link:
            await self._disconnect_stale_control_svis(member_devices, mlag_name)

        control_prefix = await self.client.allocate_next_ip_prefix(
            resource_pool=pool,
            identifier=f"mlag-control__{mlag_obj.id}",
            prefix_length=prefix_length,
            member_type="address",
            data={"role": "technical", "is_pool": True},
        )
        if control_prefix is None:
            self.logger.error(f"[{mlag_name}] Pool {pool_name} returned no prefix — exhausted?")
            return {}
        self.logger.info(f"  [{mlag_name}] MLAG control prefix {control_prefix.display_label}")

        ips = await self.upsert_p2p_addresses(
            control_prefix,
            address_length=loopback_length if virtual_peer_link else prefix_length,
            description=f"MLAG control — {mlag_name}",
            track=track,
        )
        return {
            device_obj.id: ip_obj.id
            for device_obj, ip_obj in zip(sorted(member_devices, key=lambda d: d.name.value), ips)
        }

    async def _ensure_control_svi(
        self, device_obj: Any, mlag_name: str, control_ip_id: str, existing_svi: Any | None, *, track: bool = True
    ) -> Any:
        """Create or upsert the Vlan4094 SVI that carries the control session over a back-to-back peer-link; returns the
        saved SVI.
        """
        svi_obj = await self.client.create(
            kind=DcimVirtualInterface,
            data={
                **({"id": existing_svi.id} if existing_svi else {}),
                "name": _CONTROL_SVI_NAME,
                "device": {"id": device_obj.id},
                "status": "active",
                "role": "mlag-control",
                "description": f"MLAG control — {mlag_name}",
                "ip_address": {"id": control_ip_id},
            },
        )
        await svi_obj.save(**tracked_save_kwargs(track))
        self.logger.info(
            f"  [{device_obj.name.value}] {'Updated' if existing_svi else 'Created'} MLAG control SVI "
            f"{_CONTROL_SVI_NAME} ({svi_obj.id})"
        )
        return svi_obj

    async def _disconnect_stale_control_svis(self, member_devices: list[Any], mlag_name: str) -> None:
        """Delete control SVIs left from a back-to-back run (a virtual peer-link uses its loopback; vPC has no control
        session).
        """
        stale = await self._interfaces_by_device(
            DcimVirtualInterface, [device_obj.id for device_obj in member_devices], "mlag-control"
        )
        for device_obj in member_devices:
            for stale_obj in stale[device_obj.id]:
                self.logger.info(
                    f"  [{device_obj.name.value}] Removing stale MLAG control SVI {stale_obj.name.value} ({mlag_name})"
                )
                await self.client.delete(kind=DcimVirtualInterface, id=stale_obj.id)

    async def _resolve_peer_link_deployment_id(self, device_obj: Any) -> str | None:
        """Peer-link cables are filed against the PARENT of the device's deployment when that is a TopologyPod, else the
        deployment itself.
        """
        deployment_rel = getattr(device_obj, "deployment", None)
        if deployment_rel is None or not deployment_rel.initialized:
            return None
        if deployment_rel.typename == "TopologyPod":
            # Every MLAG pair of a run sits in the same pod: read its parent once
            parent_memo: dict[str, str | None] = self.__dict__.setdefault("_pod_parent_id_memo", {})
            if deployment_rel.id not in parent_memo:
                pod_obj = await self.client.get(kind=TopologyPod, id=deployment_rel.id, include=["parent"])
                parent_rel = getattr(pod_obj, "parent", None)
                parent_memo[deployment_rel.id] = (
                    parent_rel.id if parent_rel is not None and parent_rel.initialized else None
                )
            return parent_memo[deployment_rel.id]
        return deployment_rel.id

    async def _ensure_peer_link_cables(
        self,
        mlag_name: str,
        dev_a: Any,
        dev_b: Any,
        ports_a: list[Any],
        ports_b: list[Any],
        *,
        track: bool = True,
    ) -> None:
        """Create/upsert a DcimCable for each index-matched pair of sorted role=mlag-peer ports
        (``ports_a``/``ports_b``). Saved like the peer-link interfaces. The by-name lookup stays per cable: adopting an
        orphan renames it.
        """
        dev_a_name, dev_b_name = dev_a.name.value, dev_b.name.value
        deployment_id = await self._resolve_peer_link_deployment_id(dev_a)

        ifaces_a = sorted(ports_a, key=lambda i: i.name.value)
        ifaces_b = sorted(ports_b, key=lambda i: i.name.value)
        if not ifaces_a or not ifaces_b:
            return

        layouts = await self._cable_layouts([dev_a.id, dev_b.id])
        for idx, (iface_a, iface_b) in enumerate(zip(ifaces_a, ifaces_b), start=1):
            cable_name = f"CBL-{mlag_name}-PL{idx}"

            existing_cables = await self.client.filters(kind=DcimCable, name__value=cable_name)
            existing_cable_id: str | None = existing_cables[0].id if existing_cables else None

            if existing_cable_id is None:
                # Adopt an orphan cable already on either interface (e.g. after a rename); the ports came with
                # include=["cable"]
                cable_a, cable_b = getattr(iface_a, "cable", None), getattr(iface_b, "cable", None)
                if cable_a is not None and cable_a.initialized:
                    existing_cable_id = cable_a.id
                elif cable_b is not None and cable_b.initialized:
                    existing_cable_id = cable_b.id

            cable_data: dict[str, Any] = {
                **({"id": existing_cable_id} if existing_cable_id else {}),
                "name": cable_name,
                "type": "smf",
                "endpoints": [iface_a.id, iface_b.id],
                **cable_attributes(
                    "smf",
                    [
                        endpoint_label(dev_a_name, iface_a.name.value),
                        endpoint_label(dev_b_name, iface_b.name.value),
                    ],
                    layouts.get(dev_a.id),
                    layouts.get(dev_b.id),
                ),
            }
            if deployment_id:
                cable_data["deployment"] = {"id": deployment_id}
            cable_obj = await self.client.create(kind=DcimCable, data=cable_data)
            await cable_obj.save(**tracked_save_kwargs(track))
            self.logger.info(
                f"  [{mlag_name}] {'Updated' if existing_cable_id else 'Created'} {cable_name}: "
                f"{dev_a_name}:{iface_a.name.value} ↔ {dev_b_name}:{iface_b.name.value}"
            )

    async def _disconnect_stale_peer_link_cables(self, mlag_name: str, port_count: int) -> None:
        """Delete leftover physical peer-link cables from a back-to-back run; ``port_count`` is the most role=mlag-peer
        ports either device has.
        """
        cable_names = [f"CBL-{mlag_name}-PL{idx}" for idx in range(1, port_count + 1)]
        if not cable_names:
            return
        found = {
            cable.name.value: cable for cable in await self.client.filters(kind=DcimCable, name__values=cable_names)
        }
        for cable_name in cable_names:
            if cable_name in found:
                self.logger.info(f"  [{mlag_name}] Removing stale physical peer-link cable {cable_name}")
                await self.client.delete(kind=DcimCable, id=str(found[cable_name].id))
