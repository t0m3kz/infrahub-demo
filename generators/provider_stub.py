"""ProviderStubMixin: the Loopback0 and the ManagedBGP process of a provider-side stub, built when absent.

A stub is the provider's device of a facility (an AWS VPN gateway, a Zscaler node), shared by every connection that
lands on it and never deleted with one, so everything written here is UNTRACKED (``update_group_context=False``; the
address is a pool mutation). Authored objects win: the stub's loopback and process are looked up by device first and
reused, never duplicated.

- the Loopback0 address comes from ``Provider-Stub-Loopback-IPv4`` with the identifier ``stub-lo0__<device id>``
  (idempotent: a run that stopped after the allocation gets the same address back);
- the process is ``<stub>-bgp`` in the connection's ``provider_as``, router-id the loopback address;
- a lock per stub device (``pc-device-<id>``, taken one at a time in id order, released before the caller builds
  anything else) serializes connections sharing a stub; under it the process is read again, so the connection that
  lost the race checks its ``provider_as`` against the winner's and fails on a difference.

Refusals precede the first write of the stub step: no pool while an address has to be allocated, a process whose AS
is not ``provider_as``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from infrahub_sdk.protocols import CoreIPAddressPool

from utils.limits import PROVIDER_STUB_LOOPBACK_POOL

from .protocols import DcimVirtualInterface, ManagedBGP

if TYPE_CHECKING:
    from .common import CommonGenerator
    from .pools import PoolMixin

STUB_LOOPBACK = "Loopback0"
STUB_LOOPBACK_PREFIX_LENGTH = 32


def _peer_id(rel: Any) -> str | None:
    """The peer id of a relationship that was included, None when unset."""
    return rel.id if rel is not None and getattr(rel, "initialized", True) and getattr(rel, "id", None) else None


class ProviderStubMixin:
    """Host: a CommonGenerator with PoolMixin (``client``, ``logger``, ``fail``, ``resource_lock``)."""

    client: Any
    logger: Any
    # Type checking only: the real members of the host's other bases, aliased so nothing is defined at runtime and
    # nothing shadows them in the MRO.
    if TYPE_CHECKING:
        fail = CommonGenerator.fail
        resource_lock = PoolMixin.resource_lock

    async def ensure_provider_stubs(
        self, connection: str, stubs: Iterable[tuple[str, str]], provider_as_id: str | None
    ) -> None:
        """Give every stub ``(device id, device name)`` its Loopback0 and its one process; sorted by id."""
        stubs = sorted(stubs)
        if not stubs:
            return
        if not provider_as_id:
            self.fail(f"{connection}: provider_as is needed to build the stub process")
        pool = await self._stub_pool_if_needed(connection, [device_id for device_id, _ in stubs])
        for device_id, device_name in stubs:
            async with self.resource_lock(f"pc-device-{device_id}"):
                await self._ensure_stub(connection, device_id, device_name, provider_as_id, pool)

    async def _loopbacks(self, device_ids: list[str]) -> dict[str, Any]:
        """The Loopback0 of each device (by device first: an authored one is reused)."""
        found = await self.client.filters(
            kind=DcimVirtualInterface,
            device__ids=device_ids,
            name__values=[STUB_LOOPBACK],
            include=["ip_address", "device"],
        )
        return {device_id: node for node in found if (device_id := _peer_id(getattr(node, "device")))}

    async def _stub_pool_if_needed(self, connection: str, device_ids: list[str]) -> Any:
        """The loopback pool when some stub still lacks an address; the run fails here, before any write, without it."""
        loopbacks = await self._loopbacks(device_ids)
        if all(_peer_id(getattr(loopbacks.get(i), "ip_address", None)) for i in device_ids):
            return None
        pool = await self.client.get(
            kind=CoreIPAddressPool, name__value=PROVIDER_STUB_LOOPBACK_POOL, raise_when_missing=False
        )
        if pool is None:
            self.fail(
                f"{connection}: pool '{PROVIDER_STUB_LOOPBACK_POOL}' not found: it allocates the stub router-ids "
                "(data/bootstrap/26b_provider_stub_pool.yml)"
            )
        return pool

    async def _ensure_stub(self, connection: str, device_id: str, device_name: str, as_id: str, pool: Any) -> None:
        processes = await self.client.filters(kind=ManagedBGP, capabilities__ids=[device_id], include=["local_as"])
        if processes:
            wrong = [p.name.value for p in processes if _peer_id(getattr(p, "local_as", None)) != as_id]
            if len(processes) > 1 or wrong:
                self.fail(
                    f"{connection}: {device_name} already runs {len(processes)} BGP process(es)"
                    + (f" ({', '.join(wrong)} not in the connection's provider_as)" if wrong else "")
                    + ": a stub runs exactly one, in the AS of every connection that shares it"
                )
            return
        loopback = (await self._loopbacks([device_id])).get(device_id)
        address_id = _peer_id(getattr(loopback, "ip_address", None))
        if address_id is None:
            if pool is None:  # a concurrent run emptied the address between the read and the lock
                pool = await self.client.get(kind=CoreIPAddressPool, name__value=PROVIDER_STUB_LOOPBACK_POOL)
            address = await self.client.allocate_next_ip_address(
                resource_pool=pool,
                identifier=f"stub-lo0__{device_id}",
                prefix_length=STUB_LOOPBACK_PREFIX_LENGTH,
                data={"description": f"Router-id of {device_name}"},
            )
            address_id = address.id
            node = await self.client.create(
                kind=DcimVirtualInterface,
                data={
                    **({"id": loopback.id} if loopback is not None else {}),
                    "name": STUB_LOOPBACK,
                    "description": f"Router-id of {device_name}",
                    "device": {"id": device_id},
                    "status": "active",
                    "role": "loopback",
                    "ip_address": {"id": address_id},
                },
            )
            await node.save(allow_upsert=True, update_group_context=False)
            self.logger.info(f"  {device_name}: {STUB_LOOPBACK} {address.address.value}")
        process = await self.client.create(
            kind=ManagedBGP,
            data={
                "name": f"{device_name}-bgp",
                "description": f"BGP process of the provider stub {device_name}",
                "status": "active",
                "local_as": {"id": as_id},
                "router_id": {"id": address_id},
                "capabilities": [{"id": device_id}],
            },
        )
        await process.save(allow_upsert=True, update_group_context=False)
        self.logger.info(f"  {device_name}: BGP process {device_name}-bgp")
