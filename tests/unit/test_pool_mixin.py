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

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError

from generators.firewall_context import FIREWALL_CONTEXT, LOADBALANCER_CONTEXT
from generators.logger import GeneratorError
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


class TestEnsureContextVlanPool:
    """A fabric's context VLAN pool per ContextSpec; the legs address from the per-VRF bootstrap pools."""

    def test_firewall_pool_is_only_the_vlan_pool(self) -> None:
        gen = _make_gen()
        gen.upsert_number_pool = AsyncMock()
        gen.ensure_sliced_pool = AsyncMock()

        asyncio.run(gen.ensure_context_vlan_pool(name="dc1", spec=FIREWALL_CONTEXT))

        kwargs = gen.upsert_number_pool.call_args.kwargs
        assert kwargs["pool_name"] == "dc1-fw-context-vlan-pool"
        assert (kwargs["start_range"], kwargs["end_range"]) == (3000, 3099)
        assert (kwargs["node"], kwargs["node_attribute"]) == ("ManagedFirewallContext", "vlan_id")
        gen.ensure_sliced_pool.assert_not_awaited()

    def test_lb_pool_is_the_upper_half_of_the_band_under_its_own_lock(self) -> None:
        gen = _make_gen()
        gen.upsert_number_pool = AsyncMock()
        calls: list[str] = []
        gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: calls.append(key) or "lock-id")
        gen.release_resource_lock = AsyncMock()

        asyncio.run(gen.ensure_context_vlan_pool(name="dc1", spec=LOADBALANCER_CONTEXT))

        kwargs = gen.upsert_number_pool.call_args.kwargs
        assert kwargs["pool_name"] == "dc1-lb-context-vlan-pool"
        assert (kwargs["start_range"], kwargs["end_range"]) == (3100, 3199)
        assert (kwargs["node"], kwargs["node_attribute"]) == ("ManagedLoadbalancerContext", "vlan_id")
        assert calls == ["lb-context-pools-dc1"]


def _target_node() -> MagicMock:
    node = MagicMock()
    node.save = AsyncMock()
    return node


class TestAttachRefs:
    """The one place pool / evpn_rt_as references are written onto a generator's target node."""

    @pytest.mark.asyncio
    async def test_sets_every_ref_with_one_fetch_and_one_untracked_plain_save(self) -> None:
        gen = _make_gen()
        node = _target_node()
        gen.client.get = AsyncMock(return_value=node)

        await gen.attach_refs(
            kind="TopologyDataCenter", node_id="dc-1", refs={"loopback_pool": "lo-1", "vni_pool": "vni-1"}
        )

        gen.client.get.assert_awaited_once_with(kind="TopologyDataCenter", id="dc-1")
        assert node.loopback_pool == {"id": "lo-1"}
        assert node.vni_pool == {"id": "vni-1"}
        node.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_none_refs_are_skipped_and_all_none_touches_nothing(self) -> None:
        gen = _make_gen()
        node = _target_node()
        node.fabric_asn_pool = "untouched"
        gen.client.get = AsyncMock(return_value=node)

        await gen.attach_refs(kind="TopologyDataCenter", node_id="dc-1", refs={"fabric_asn_pool": None, "x": "p-1"})
        assert node.fabric_asn_pool == "untouched"
        assert node.x == {"id": "p-1"}

        gen.client.get.reset_mock()
        await gen.attach_refs(kind="TopologyDataCenter", node_id="dc-1", refs={"fabric_asn_pool": None})
        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rerun_writes_the_same_refs(self) -> None:
        """Idempotent: a second run attaches the identical ids (and re-saves, by design)."""
        gen = _make_gen()
        node = _target_node()
        gen.client.get = AsyncMock(return_value=node)

        for _ in range(2):
            await gen.attach_refs(kind="TopologyPod", node_id="pod-1", refs={"loopback_pool": "lo-1"})

        assert node.loopback_pool == {"id": "lo-1"}
        assert node.save.await_count == 2

    @pytest.mark.asyncio
    async def test_missing_node_is_fatal_not_skipped(self) -> None:
        gen = _make_gen()
        gen.client.get = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="TopologyPod", identifier={})
        )

        with pytest.raises(GeneratorError, match="TopologyPod pod-1: node not found"):
            await gen.attach_refs(kind="TopologyPod", node_id="pod-1", refs={"loopback_pool": "lo-1"})

        gen.logger.error.assert_called_once()

    @pytest.mark.asyncio
    async def test_save_failure_propagates(self) -> None:
        gen = _make_gen()
        node = _target_node()
        node.save = AsyncMock(side_effect=RuntimeError("write rejected"))
        gen.client.get = AsyncMock(return_value=node)

        with pytest.raises(RuntimeError, match="write rejected"):
            await gen.attach_refs(kind="TopologyPod", node_id="pod-1", refs={"loopback_pool": "lo-1"})

    @pytest.mark.asyncio
    async def test_pod_strategy_attaches_loopback_and_prefix_pool_in_one_save(self) -> None:
        gen = _make_gen()
        gen.pod_name = "dc1-pod1"
        gen.ensure_sliced_pool = AsyncMock(
            side_effect=lambda **kw: MagicMock(id=f"{kw['pool_name']}-id", hfid=[kw["pool_name"]])
        )
        pod = _target_node()
        gen.client.get = AsyncMock(return_value=pod)

        await gen._allocate_resource_pools_locked(strategy="pod", pools={"technical": 24, "loopback": 28}, id="pod-1")

        assert pod.prefix_pool == {"id": "dc1-pod1-technical-pool-id"}
        assert pod.loopback_pool == {"id": "dc1-pod1-loopback-pool-id"}
        pod.save.assert_awaited_once_with(update_group_context=False)

    @pytest.mark.asyncio
    async def test_fabric_strategy_never_touches_a_pod(self) -> None:
        gen = _make_gen()
        gen.ensure_sliced_pool = AsyncMock(return_value=MagicMock(id="p"))

        await gen._allocate_resource_pools_locked(strategy="fabric", pools={"technical": 24}, id="dc-1")

        gen.client.get.assert_not_awaited()


class TestFabricParentPoolFamily:
    """allocate_resource_pools slices each fabric pool from the parent of the family the
    prefix lengths (helpers.routing) are computed for."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("protocol", "management", "technical", "loopback"),
        [
            ("ipv4", "Management-IPv4", "Technical-IPv4", "Loopback-IPv4"),
            ("ipv6", "Management-IPv4", "Technical-IPv6", "Loopback-IPv6"),
            ("dual_stack", "Management-IPv4", "Technical-IPv6", "Loopback-IPv4"),
        ],
    )
    async def test_parent_pool_per_underlay_protocol(
        self, protocol: str, management: str, technical: str, loopback: str
    ) -> None:
        gen = _make_gen()
        gen.ensure_sliced_pool = AsyncMock(return_value=MagicMock(id="pool"))

        await gen.allocate_resource_pools(
            strategy="fabric",
            pools={"management": 26, "technical": 117, "loopback": 120},
            id="dc-1",
            underlay_protocol=protocol,
        )

        parents = {c.kwargs["role"]: c.kwargs["parent_pool_name"] for c in gen.ensure_sliced_pool.await_args_list}
        assert parents == {"management": management, "technical": technical, "loopback": loopback}
