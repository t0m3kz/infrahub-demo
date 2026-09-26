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
