"""Mixin realizing a segment's LOCAL VLAN ID per VLAN domain (MLAG pair or
standalone device).

Shared by VxlanSegmentGenerator (generators/topology/segment.py, border
gateway realization for a stretched segment) and AppInstanceSegmentGenerator
(generators/topology/app_instance_segment.py, customer-port realization
driven by an AppComponent's instances) — both need the same (segment, VLAN
domain) -> local VLAN ID allocation, just reached from different device sets.

Independent VLAN domains may reuse the same numeric VLAN ID, since IEEE
802.1Q VLAN ID has only local significance (unlike VNI, which is the real
DC-wide/fabric-wide segment identifier allocated in segment.py).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .helpers.pools import CUSTOMER_VLAN_ID_MAX, CUSTOMER_VLAN_ID_MIN
from .protocols import ManagedStandaloneVlanDomain, ManagedVlanDomainSegment


class VlanDomainMixin:
    """Mixin providing (segment, VLAN domain) -> local VLAN ID realization.

    Expects the host class to provide: ``client``, ``logger``,
    ``upsert_number_pool`` (PoolMixin — present on CommonGenerator subclasses
    that also mix in PoolMixin).
    """

    client: Any
    logger: logging.Logger
    # PoolMixin.upsert_number_pool — declared as a plain Callable attribute,
    # not a method, so it never shadows the real implementation via MRO (same
    # convention as EndpointUplinkMixin's cross-mixin attributes).
    upsert_number_pool: Callable[..., Awaitable[Any]]

    async def _resolve_vlan_domain(self, device: Any) -> tuple[str, str]:
        """Return (domain_kind, domain_id) for a leaf/tor device: its
        ManagedMLAG if paired, else the device itself is its own standalone
        VLAN domain. Requires device.capabilities to already be fetched
        (batch-included by the caller to avoid an N+1 query pattern)."""
        caps = getattr(device, "capabilities", None)
        if caps is not None:
            for peer in caps.peers:
                if peer.typename == "ManagedMLAG":
                    return "ManagedMLAG", peer.id
        return "DcimPhysicalDevice", device.id

    async def _ensure_standalone_vlan_domain(self, device: Any) -> tuple[str, str]:
        """Create/upsert the ManagedStandaloneVlanDomain (and its vlan_pool)
        for a non-MLAG device, lazily — only when it's actually assigned a
        segment, avoiding speculative pool creation for idle leafs/tors.
        Returns (domain id, pool id).

        Both are upserted on every run, existing or not: the generator deletes
        what a run does not save, so a rerun that only read them back would
        delete them. Segments are generated in parallel; both upserts are keyed
        by name, so concurrent runs converge on the same domain and pool.
        """
        domain_name = f"{device.name.value}-vlan-domain"
        domain_obj = await self.client.create(
            kind=ManagedStandaloneVlanDomain,
            data={"name": domain_name, "status": "active", "capabilities": [{"id": device.id}]},
        )
        await domain_obj.save(allow_upsert=True)
        domain_id = domain_obj.id
        pool = await self.upsert_number_pool(
            pool_name=f"{domain_name}-vlan-pool",
            description=f"Local VLAN ID pool for standalone VLAN domain {domain_name}",
            start_range=CUSTOMER_VLAN_ID_MIN,
            end_range=CUSTOMER_VLAN_ID_MAX,
            node="ManagedVlanDomainSegment",
            node_attribute="vlan_id",
            parent_kind="ManagedStandaloneVlanDomain",
            parent_id=domain_id,
            parent_attr="vlan_pool",
        )
        return domain_id, pool.id

    async def _ensure_vlan_domain_segment(
        self, segment_id: str, segment_name: str, domain_id: str, pool_id: str | None = None
    ) -> None:
        """Upsert one ManagedVlanDomainSegment (segment, VLAN domain) pair,
        allocating vlan_id from that domain's own pool via from_pool.
        Idempotent: an existing record for this pair keeps its vlan_id and is
        only saved again, so this run's tracking group keeps it (an unsaved
        one is deleted). A known pool_id (a standalone domain this run just
        ensured) skips reading it back from the domain.
        """
        existing = await self.client.filters(
            kind=ManagedVlanDomainSegment,
            segment__ids=[segment_id],
            vlan_domain__ids=[domain_id],
        )
        if existing:
            await existing[0].save(allow_upsert=True)  # register with tracker
            return

        if not pool_id:
            domain = await self.client.get(kind="ManagedGenericVlanDomain", id=domain_id, include=["vlan_pool"])
            vlan_pool_rel = getattr(domain, "vlan_pool", None)
            pool_id = getattr(vlan_pool_rel, "id", None) if vlan_pool_rel else None
        if not pool_id:
            self.logger.error(
                f"VLAN domain {domain_id} has no vlan_pool — cannot allocate VLAN ID for segment {segment_name}"
            )
            return

        vlan_identifier = f"{segment_id}-{domain_id}-vlan"
        activation = await self.client.create(
            kind=ManagedVlanDomainSegment,
            data={
                "segment": {"id": segment_id},
                "vlan_domain": {"id": domain_id},
                "vlan_id": {"from_pool": {"id": pool_id}, "identifier": vlan_identifier},
            },
        )
        await activation.save(allow_upsert=True)
        self.logger.info(f"  Allocated VLAN ID from domain {domain_id}'s pool for segment {segment_name}")

    async def _realize_segment_on_devices(
        self, segment_id: str, segment_name: str, devices: list[Any]
    ) -> dict[str, str | None]:
        """Realize the segment's local VLAN ID once per distinct VLAN domain of
        ``devices`` (capabilities included). Returns {domain_id: pool_id}."""
        domain_pools = await self._ensure_vlan_domains_for_devices(devices)
        for domain_id, pool_id in domain_pools.items():
            await self._ensure_vlan_domain_segment(segment_id, segment_name, domain_id, pool_id)
        return domain_pools

    async def _ensure_vlan_domains_for_devices(self, devices: list[Any]) -> dict[str, str | None]:
        """Resolve each device's VLAN domain and return {domain_id: pool_id}
        for every distinct domain touched — shared by callers that then upsert
        one ManagedVlanDomainSegment per domain via _ensure_vlan_domain_segment.

        Each device's own resolution is independent — a standalone domain is
        keyed by that device's own name, and an MLAG-paired device only reads
        its shared peer id — so these run concurrently; the gather's results
        are merged into one dict afterwards, single-threaded.
        """
        resolved = await asyncio.gather(*(self._resolve_device_vlan_domain(device) for device in devices))
        domain_pools: dict[str, str | None] = {}
        for domain_id, pool_id in resolved:
            domain_pools[domain_id] = domain_pools.get(domain_id) or pool_id
        return domain_pools

    async def _resolve_device_vlan_domain(self, device: Any) -> tuple[str, str | None]:
        """One device's (domain_id, pool_id) — the per-device body gathered by
        _ensure_vlan_domains_for_devices."""
        domain_kind, domain_id = await self._resolve_vlan_domain(device)
        pool_id = None
        if domain_kind == "DcimPhysicalDevice":
            domain_id, pool_id = await self._ensure_standalone_vlan_domain(device)
        return domain_id, pool_id
