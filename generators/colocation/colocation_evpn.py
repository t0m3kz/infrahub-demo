"""EVPN Multi-Site for a colocation zone or virtual colocation: border-gateway processes and the edge pair.
ColocationFabricGenerator steps after the device pass: ``_ensure_evpn`` and ``_ensure_edge_pair`` (iBGP FABRIC)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreNumberPool

from generators.colocation.colocation_pools import TECHNICAL_IPV6_SLOT, ColocationFabricData, ColoPoolSlot
from generators.helpers import get_loopback_name
from generators.helpers.routing import p2p_prefix_length
from generators.mixins.routing import EVPN_AF
from generators.mixins.types import CablingOptions
from generators.protocols import DcimVirtualInterface, ManagedBGP

if TYPE_CHECKING:
    from generators.mixins.common import CommonGenerator
    from generators.mixins.connections import CablingMixin
    from generators.mixins.pools import PoolMixin
    from generators.mixins.routing import RoutingMixin
    from generators.mixins.shared_routing import SharedRoutingObjectsMixin

# EVPN Multi-Site border gateway: a tunnel forms only between VTEPs of one address family and the fabrics run an IPv6
# underlay, so the VTEP is a dedicated IPv6 loopback (the IPv4 Loopback0 stays the 32-bit router-id).
_COLO_VTEP_LOOPBACK_INDEX = 1
# The two edges of a zone share no fabric: one direct link (role `interlink`) carries a /127 and the iBGP FABRIC
# session.
_COLO_INTERLINK_ROLE = "interlink"
_COLO_P2P_IPV6_PREFIX_LENGTH = p2p_prefix_length("ipv6")
_COLO_FABRIC_UNICAST_AF = ("ipv6", "unicast")


class ColocationEvpnMixin:
    """The EVPN Multi-Site and edge-pair steps of ColocationFabricGenerator; needs ``_slice_pool`` and the mixed-in
    bases.
    """

    # Annotations and TYPE_CHECKING aliases of the host's other bases; nothing is defined at runtime
    client: Any
    logger: Any
    data: ColocationFabricData
    fabric_name: str
    _label: ClassVar[str]
    _edge_devices: dict[str, str]
    _vtep_pool: Any
    if TYPE_CHECKING:
        _ensure_routing_password = SharedRoutingObjectsMixin._ensure_routing_password
        _get_parent_pool_with_retry = PoolMixin._get_parent_pool_with_retry
        attach_refs = PoolMixin.attach_refs
        create_chain_cabling = CablingMixin.create_chain_cabling
        ensure_address_family = RoutingMixin.ensure_address_family
        ensure_shared_as = SharedRoutingObjectsMixin.ensure_shared_as
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock
        upsert_bgp_peering = RoutingMixin.upsert_bgp_peering
        wait_for_parent_generators = CommonGenerator.wait_for_parent_generators

    if TYPE_CHECKING:
        # The generator's own method; a stub for the type checker
        async def _slice_pool(self, slot: ColoPoolSlot) -> Any: ...

    async def _metro_asn_pool_id(self) -> str:
        """The id of the parent metro's ASN pool; waits for add_colocation_metro, then looks it up by name with bounded
        retry.
        """
        metro = self.data.get("parent") or {}
        pool = metro.get("fabric_asn_pool") or {}
        if pool.get("id"):
            return pool["id"]

        metro_name = metro.get("name", "")
        await self.wait_for_parent_generators(("add_colocation_metro",), metro["id"])
        try:
            found = await self._get_parent_pool_with_retry(f"{metro_name.lower()}-asn-pool", kind=CoreNumberPool)
        except NodeNotFoundError:
            self.fail(
                f"Colocation {self._label} {self.fabric_name}: metro {metro_name} has no ASN pool "
                f"'{metro_name.lower()}-asn-pool' (add_colocation_metro creates it) — cannot draw the metro's "
                "EVPN site AS"
            )
        return found.id

    async def _ensure_evpn(self) -> dict[str, str]:
        """Make this node's edges EVPN Multi-Site border gateways: the metro's site ASN, an overlay ManagedBGP and an
        IPv6 VTEP loopback per edge. DCI sessions and the Multi-Site VIP are not built here. Returns edge name ->
        overlay process id (avoids a by-name re-query).
        """
        name = self.fabric_name
        process_ids: dict[str, str] = {}
        edge_devices: dict[str, str] = getattr(self, "_edge_devices", {})
        if not edge_devices:
            return process_ids
        metro = self.data["parent"]
        # Untracked: every zone of the metro shares this AS, so no tracking group may own it
        site_as_id = await self.ensure_shared_as(
            description=f"{metro['name'].lower()} EVPN site ASN",
            asn_pool_id=await self._metro_asn_pool_id(),
            track=False,
        )
        # The query read the metro's site AS: a re-run with it attached costs no get and no save
        await self.attach_refs(
            kind="TopologySegmentHosting",
            node_id=metro["id"],
            refs={"evpn_rt_as": site_as_id},
            current={"evpn_rt_as": (metro.get("evpn_rt_as") or {}).get("id")},
        )

        loopbacks = await self.client.filters(
            kind=DcimVirtualInterface,
            device__name__values=sorted(edge_devices),
            role__values=["loopback", "loopback-vtep"],
            include=["device", "ip_address"],
        )
        by_device: dict[tuple[str, str], Any] = {
            (loopback.device.peer.name.value, loopback.role.value): loopback for loopback in loopbacks
        }
        for edge_name, platform in sorted(edge_devices.items()):
            router_loopback = by_device.get((edge_name, "loopback"))
            if router_loopback is None or not router_loopback.ip_address.initialized:
                self.logger.error(
                    f"Colocation {self._label} {name}: {edge_name} has no addressed loopback — skipping its EVPN process"
                )
                continue
            device = router_loopback.device.peer
            vtep = by_device.get((edge_name, "loopback-vtep"))
            vtep_ip: Any
            if vtep is not None and vtep.ip_address.initialized:
                vtep_ip = {"id": vtep.ip_address.id}
            else:
                vtep_ip = await self.client.allocate_next_ip_address(
                    resource_pool=self._vtep_pool,
                    identifier=f"{edge_name}-vtep",
                    prefix_length=128,
                    data={"description": f"EVPN VTEP loopback for {edge_name}"},
                )
            vtep_obj = await self.client.create(
                kind=DcimVirtualInterface,
                data={
                    **({"id": vtep.id} if vtep is not None else {}),
                    "name": get_loopback_name(platform, _COLO_VTEP_LOOPBACK_INDEX),
                    "description": "EVPN VTEP source (IPv6)",
                    "device": {"id": device.id},
                    "status": "active",
                    "role": "loopback-vtep",
                    "ip_address": vtep_ip,
                },
            )
            await vtep_obj.save(allow_upsert=True)
            bgp = await self.client.create(
                kind=ManagedBGP,
                data={
                    "name": f"{edge_name}-bgp-overlay",
                    "description": f"EVPN Multi-Site border-gateway process for {edge_name}",
                    "status": "active",
                    "local_as": {"id": site_as_id},
                    "router_id": {"id": router_loopback.ip_address.id},
                    "capabilities": [{"id": device.id}],
                    "process_role": "overlay",
                },
            )
            await bgp.save(allow_upsert=True)
            process_ids[edge_name] = bgp.id
            self.logger.info(f"Colocation {self._label} {name}: ensured EVPN VTEP + overlay BGP for {edge_name}")
        return process_ids

    async def _ensure_edge_pair(self, *, physical_edge_names: list[str], overlay_process_ids: dict[str, str]) -> None:
        """Link a zone's two physical edges and peer them over that link (iBGP FABRIC: IPv6 unicast and l2vpn evpn).
        Only for exactly two physical edges; everything is zone-owned and keyed deterministically, under one lock."""
        name = self.fabric_name
        if len(physical_edge_names) != 2:
            self.logger.info(
                f"Colocation {self._label} {name}: {len(physical_edge_names)} physical edge(s) — an edge pair "
                "needs exactly 2, no interlink or FABRIC session"
            )
            return
        first, second = sorted(physical_edge_names)
        processes = [overlay_process_ids.get(first), overlay_process_ids.get(second)]
        if not all(processes):
            self.logger.warning(
                f"Colocation {self._label} {name}: {first}/{second} have no overlay BGP process from this run — "
                "skipping the edge pair (see the EVPN step above)"
            )
            return

        async with self.resource_lock(f"edge-pair-{name}"):
            pool = await self._slice_pool(TECHNICAL_IPV6_SLOT)
            legs = await self.create_chain_cabling(
                hops=[
                    {"devices": [first], "down_role": _COLO_INTERLINK_ROLE},
                    {"devices": [second], "up_role": _COLO_INTERLINK_ROLE},
                ],
                options=CablingOptions(pool=pool, p2p_prefix_length=_COLO_P2P_IPV6_PREFIX_LENGTH),
            )
            pairs = legs[0] if legs else []
            if not pairs:
                self.logger.warning(
                    f"Colocation {self._label} {name}: no {_COLO_INTERLINK_ROLE}-role port pair on "
                    f"{first}/{second} — no FABRIC session"
                )
                return
            interface_ids = [iface.id for pair in pairs for iface in pair]

            key_id = await self._ensure_routing_password(
                name=f"{name}-fabric-key", description=f"iBGP FABRIC session auth key for {self._label} {name}"
            )
            if key_id is None:
                return
            address_families = [
                await self.ensure_address_family(*_COLO_FABRIC_UNICAST_AF),
                await self.ensure_address_family(*EVPN_AF),
            ]
            await self.upsert_bgp_peering(
                name=f"FABRIC-{first}-{second}",
                description=(
                    f"Colocation {self._label} {name} edge pair {first} <-> {second}: "
                    "IPv6 unicast + EVPN over the interlink"
                ),
                status="active",
                session_type="IBGP",
                peering_role="fabric",
                process_ids=[str(process_id) for process_id in processes],
                address_family_ids=address_families,
                password_id=key_id,
                interface_ids=interface_ids,
            )
            self.logger.info(
                f"Colocation {self._label} {name}: ensured edge pair {first} <-> {second} (interlink + FABRIC session)"
            )
