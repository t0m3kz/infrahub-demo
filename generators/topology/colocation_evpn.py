"""EVPN Multi-Site for a colocation zone or virtual colocation: the border-gateway processes and the edge pair.

The two steps of ColocationFabricGenerator (colocation_fabric.py) that follow the device pass: ``_ensure_evpn``
makes the node's edges border gateways of the metro's one Multi-Site site (its ASN, an IPv6 VTEP loopback and an
overlay ManagedBGP per edge) and ``_ensure_edge_pair`` links a zone's two physical edges and peers them (iBGP
FABRIC). A mixin so the generator file keeps the pools, the validation and the devices.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from infrahub_sdk.exceptions import NodeNotFoundError
from infrahub_sdk.protocols import CoreNumberPool

from ..helpers import get_loopback_name
from ..helpers.routing import p2p_prefix_length
from ..protocols import DcimVirtualInterface, ManagedBGP
from ..routing import EVPN_AF
from ..types import CablingOptions
from .colocation_pools import TECHNICAL_IPV6_SLOT, ColocationFabricData, ColoPoolSlot

if TYPE_CHECKING:
    from ..common import CommonGenerator
    from ..connections import CablingMixin
    from ..pools import PoolMixin
    from ..routing import RoutingMixin
    from ..shared_routing import SharedRoutingObjectsMixin

# EVPN Multi-Site border gateway (see _ensure_evpn). Every DC fabric this
# demo builds runs an IPv6 underlay, and a Multi-Site tunnel only forms between
# VTEPs of one address family, so the edge's VTEP is a dedicated IPv6
# loopback rather than its IPv4 Loopback0 (which stays the BGP router-id: a
# router-id is 32-bit and cannot be an IPv6 address). The slice that holds the
# VTEPs is the loopback-ipv6 slot below.
_COLO_VTEP_LOOPBACK_INDEX = 1
# The two edges of a zone share no fabric: one direct link between them (port
# role `interlink`, N9K-C9316D-GX_EDGE's Ethernet1/8) carries a /127 and the
# iBGP FABRIC session (see _ensure_edge_pair). The technical-ipv6 slot holds eight
# /127s, far more than the one link needs.
_COLO_INTERLINK_ROLE = "interlink"
_COLO_P2P_IPV6_PREFIX_LENGTH = p2p_prefix_length("ipv6")
_COLO_FABRIC_UNICAST_AF = ("ipv6", "unicast")


class ColocationEvpnMixin:
    """The EVPN Multi-Site and edge-pair steps of ColocationFabricGenerator.

    Expects the host class to provide what is declared below: its own state and ``_slice_pool``, and the
    methods of PoolMixin, CablingMixin, RoutingMixin and CommonGenerator it is mixed into.
    """

    # Annotations, and aliases of the real members of the host's other bases under TYPE_CHECKING: nothing is
    # defined at runtime, so nothing shadows them in the MRO.
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
        # The generator's own method (it knows the metro's pools); a stub seen only by the type checker.
        async def _slice_pool(self, slot: ColoPoolSlot) -> Any: ...

    async def _metro_asn_pool_id(self) -> str:
        """The id of the parent metro's ASN pool, which the metro's one site AS is drawn from.

        The metro's own generator (add_colocation_metro) creates and attaches
        the pool, and a zone's created-trigger can fire before that task has
        finished. So the query's `parent.fabric_asn_pool` may still be empty:
        wait for the metro's generator, then look the pool up by name with the
        same bounded retry the DC-pool lookups use.
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
        """Make this node's edges EVPN Multi-Site border gateways for the metro's site.

        A stretched segment reaches a colocation through the edge pair, which
        re-originates the DC's EVPN routes into the metro (and back) — the
        border-gateway role, with the METRO as the Multi-Site site: one site AS
        however many zones and virtual colocations carry edges. That needs three
        things the generic device pass does not create:

        * the metro's site ASN, attached to the metro as ``evpn_rt_as`` — the
          site-id and the admin field of its local route-targets, same role as a
          DC's fabric AS (PoolMixin.attach_refs, the same evpn_rt_as link a DC
          gets). Whichever zone or virtual colocation runs first draws it; the
          others find it by its description;
        * an overlay ManagedBGP per edge in that ASN, so the DCI peerings
          (add_virtual_circuit, for a TopologyInterconnectCircuit) have a
          process to attach to;
        * an IPv6 VTEP loopback per edge (LOOPBACK_IPV6_SLOT).

        The DCI sessions (add_virtual_circuit, generators/topology/virtual_circuit.py) and the
        shared Multi-Site VIP (data/demos/30_all/08_interconnects/
        01_colo_onramp/) are not built here: both need the DC end of the dark
        fibre, which this generator cannot see.

        Idempotent: the AS is found by its deterministic description before a
        new one is drawn from the metro's ASN pool (a from_pool on a new node
        would allocate a fresh ASN every run). ensure_shared_as does the
        find-or-create under a lock keyed by that description, which is the
        metro's name, so zones of one metro running concurrently never draw two.
        Every other object upserts by HFID.

        Returns the overlay process id of each edge it saved (edge name -> id),
        so _ensure_edge_pair references them directly instead of re-querying
        by name (search-index lag on objects created earlier in this run).
        """
        name = self.fabric_name
        process_ids: dict[str, str] = {}
        edge_devices: dict[str, str] = getattr(self, "_edge_devices", {})
        if not edge_devices:
            return process_ids
        metro = self.data["parent"]
        # Untracked (track=False): every zone and virtual colocation of the metro finds this one AS, so none of
        # their tracking groups may own it; one that stopped declaring edges would otherwise delete an AS its
        # sibling's overlay processes still use as local_as.
        site_as_id = await self.ensure_shared_as(
            description=f"{metro['name'].lower()} EVPN site ASN",
            asn_pool_id=await self._metro_asn_pool_id(),
            track=False,
        )
        # The query read the metro's current site AS: a re-run with the AS already attached costs no get and no save.
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
        """Link a zone's two physical edges and peer them over that link (iBGP FABRIC).

        The edges are single-box sites with no fabric between them, yet they
        share one Multi-Site site (same AS, same VIP): they need a direct
        link plus a directly connected iBGP session carrying IPv6 unicast
        (VTEP /128 reachability) and l2vpn evpn. A loopback EVPN session is
        impossible — the edges run no IGP.

        Only when EXACTLY two PHYSICAL edges exist in this zone: virtual edges
        (an MCR, a Network Edge) have no port to cable, and a zone with 0 or 1
        physical edge has nothing to pair (info log, no pair). More than two is
        not a pair either. The pair stays inside one zone: edges of two zones
        are joined by circuits, not by this link.

        Everything it writes is owned by this zone and keyed
        deterministically: the technical IPv6 slice, the P2P prefix (keyed by
        the link), the cable, the FABRIC-<a>-<b> peering and the zone's own
        routing key. One lock for the whole step, so overlapping runs of one
        zone never both create any of them.
        """
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
