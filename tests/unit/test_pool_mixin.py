"""Unit tests for PoolMixin.allocate_resource_pools' namespace scoping
(generators/pools.py).

"management" (true OOB) gets the MANAGEMENT VRF; "technical" (fabric P2P)
and "loopback" stay in `default` (the global table) — EVPN-VXLAN VTEP
loopbacks and underlay BGP peering can't depend on a VRF being provisioned.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from generators.pools import PoolMixin


def _make_gen() -> Any:
    gen = PoolMixin.__new__(PoolMixin)
    gen.client = AsyncMock()
    gen.logger = MagicMock()
    gen.branch_name = "main"
    gen.fabric_name = "DC1"
    gen.pod_name = None
    # allocate_resource_pools() serializes concurrent callers for the same
    # (strategy, id) via acquire_resource_lock/release_resource_lock — not
    # under test here, and the lock's own client.create(kind=CoreStandardGroup)
    # call would otherwise show up in gen.client.create.await_args_list
    # alongside the pool creates these tests actually inspect.
    gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
    gen.release_resource_lock = AsyncMock()
    return gen


def _ip_namespaces_by_pool(create_calls: list[Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for call in create_calls:
        data = call.kwargs["data"]
        result[data["name"]] = data["ip_namespace"]["hfid"]
    return result


class TestAllocateResourcePoolsNamespaceScoping:
    def test_management_pool_uses_management_namespace(self):
        gen = _make_gen()
        gen.client.get = AsyncMock(return_value=MagicMock(name__value="DC1-technical-pool"))
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock())
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock())
        created_pool = MagicMock()
        created_pool.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_pool)

        asyncio.run(
            gen.allocate_resource_pools(
                strategy="fabric",
                pools={"management": 24},
                id="dc-1",
            )
        )

        namespaces = _ip_namespaces_by_pool(gen.client.create.await_args_list)
        assert namespaces["DC1-management-pool"] == ["MANAGEMENT"]

    def test_technical_and_loopback_pools_stay_on_default(self):
        gen = _make_gen()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock())
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock())
        created_pool = MagicMock()
        created_pool.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_pool)

        asyncio.run(
            gen.allocate_resource_pools(
                strategy="fabric",
                pools={"technical": 24, "loopback": 28},
                id="dc-1",
            )
        )

        namespaces = _ip_namespaces_by_pool(gen.client.create.await_args_list)
        assert namespaces["DC1-technical-pool"] == ["default"]
        assert namespaces["DC1-loopback-pool"] == ["default"]


class TestAllocateResourcePoolsSerializesConcurrentCallers:
    """Two overlapping generator runs for the same (strategy, id) must not
    both create a pool with the same name — see allocate_resource_pools()'s
    own comment for the reproduced DC4 hyper-spine collision this guards
    against. Only the lock/unlock discipline is under test here; the pool
    creation itself is covered above."""

    def test_lock_is_acquired_before_creating_and_released_after(self) -> None:
        gen = _make_gen()
        gen._get_parent_pool_with_retry = AsyncMock(return_value=MagicMock())
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock())
        created_pool = MagicMock()
        created_pool.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created_pool)

        calls: list[str] = []
        gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: calls.append(f"acquire:{key}") or "lock-id")
        gen.release_resource_lock = AsyncMock(side_effect=lambda lock_id: calls.append(f"release:{lock_id}"))

        asyncio.run(
            gen.allocate_resource_pools(
                strategy="fabric",
                pools={"technical": 24},
                id="dc-1",
            )
        )

        # Acquired before any pool creation, released only after it finished,
        # and scoped to this exact (strategy, id) — a different DC's call
        # would compute a different key and never contend with this one.
        assert calls == ["acquire:pool-alloc-fabric-dc-1", "release:lock-id"]

    def test_lock_is_released_even_if_pool_creation_raises(self) -> None:
        """A crash mid-allocation must not leave the lock held — the module's
        own docstring calls a stale-lock reclaim a fallback, not the norm."""
        gen = _make_gen()
        gen._get_parent_pool_with_retry = AsyncMock(side_effect=RuntimeError("boom"))

        gen.acquire_resource_lock = AsyncMock(return_value="lock-id")
        gen.release_resource_lock = AsyncMock()

        try:
            asyncio.run(
                gen.allocate_resource_pools(
                    strategy="fabric",
                    pools={"technical": 24},
                    id="dc-1",
                )
            )
        except RuntimeError:
            pass

        gen.release_resource_lock.assert_awaited_once_with("lock-id")


class TestEnsureFirewallContextPools:
    """The P2P slice is optional: DC contexts need only the VLAN pool."""

    def _gen(self) -> Any:
        gen = _make_gen()
        gen.upsert_number_pool = AsyncMock()
        gen.ensure_sliced_pool = AsyncMock()
        return gen

    def test_vlan_pool_only_when_no_parent_pool_is_given(self) -> None:
        gen = self._gen()

        asyncio.run(gen.ensure_firewall_context_pools(name="dc1", vlan_start=3000))

        assert gen.upsert_number_pool.await_args.kwargs["pool_name"] == "dc1-fw-context-vlan-pool"
        gen.ensure_sliced_pool.assert_not_awaited()

    def test_p2p_slice_is_created_when_a_parent_pool_is_given(self) -> None:
        gen = self._gen()

        asyncio.run(
            gen.ensure_firewall_context_pools(
                name="fr",
                vlan_start=3000,
                parent_pool_name="FW-Context-P2P-IPv4",
                slice_prefix_length=24,
                default_prefix_length=31,
            )
        )

        gen.upsert_number_pool.assert_awaited_once()
        kwargs = gen.ensure_sliced_pool.await_args.kwargs
        assert (kwargs["pool_name"], kwargs["parent_pool_name"], kwargs["prefix_length"]) == (
            "fr-fw-context-p2p-pool",
            "FW-Context-P2P-IPv4",
            24,
        )
        assert kwargs["default_prefix_length"] == 31
