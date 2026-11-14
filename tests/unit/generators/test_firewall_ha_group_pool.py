"""Firewall HA IDs are allocated in the existing domain-pairing flow, not at vendor call sites."""

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from infrahub_sdk.exceptions import NodeNotFoundError

from generators.mixins.domain_pairing import DomainPairingMixin
from utils.limits import FIREWALL_HA_GROUP_MAX, FIREWALL_HA_GROUP_MIN, FIREWALL_HA_GROUP_POOL


@pytest.mark.asyncio
@pytest.mark.parametrize("exists", [False, True])
async def test_pairing_uses_native_number_pool_with_untracked_bootstrap_fallback(exists: bool) -> None:
    """A missing pool is created once under its own lock; HA allocation uses native from_pool semantics."""
    gen: Any = DomainPairingMixin()
    gen.client = MagicMock()
    pool = SimpleNamespace(id="ha-pool", save=AsyncMock())
    gen.client.get = AsyncMock(return_value=pool)
    if not exists:
        gen.client.get.side_effect = NodeNotFoundError({"name": [FIREWALL_HA_GROUP_POOL]})
    gen.client.create = AsyncMock(return_value=pool)
    locks = []

    @asynccontextmanager
    async def lock(key: str) -> AsyncGenerator[None, None]:
        locks.append(key)
        yield

    gen.resource_lock = lock
    assert await gen._firewall_ha_group_data() == {"group_id": {"from_pool": {"id": "ha-pool"}}}
    assert locks == ["firewall-ha-group-pool"]
    if exists:
        gen.client.create.assert_not_awaited()
    else:
        call = gen.client.create.await_args
        assert call is not None
        assert call.kwargs["data"] == {
            "name": FIREWALL_HA_GROUP_POOL,
            "node": "ManagedFirewallHA",
            "node_attribute": "group_id",
            "start_range": FIREWALL_HA_GROUP_MIN,
            "end_range": FIREWALL_HA_GROUP_MAX,
        }
        pool.save.assert_awaited_once_with(allow_upsert=True, update_group_context=False)


def test_authored_ha_pool_mirrors_production_pool_limits(root_dir: Path) -> None:
    """Bootstrap defines the shared reservation domain before any demo's authored HA IDs."""
    data: dict[str, Any] = yaml.safe_load((root_dir / "data/bootstrap/19d_firewall_ha_pool.yml").read_text())
    (pool,) = data["spec"]["data"]
    assert pool == {
        "name": FIREWALL_HA_GROUP_POOL,
        "node": "ManagedFirewallHA",
        "node_attribute": "group_id",
        "start_range": FIREWALL_HA_GROUP_MIN,
        "end_range": FIREWALL_HA_GROUP_MAX,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", [None, 6])
async def test_existing_pair_receives_an_id_only_when_missing(group_id: int | None) -> None:
    """A computed old pair is repaired natively; an authored numeric ID is not rewritten or reallocated."""
    gen: Any = DomainPairingMixin()
    gen.client = MagicMock()
    pair = SimpleNamespace(id="pair", group_id=SimpleNamespace(value=group_id))
    gen.client.filters = AsyncMock(return_value=[pair])
    ref = SimpleNamespace(save=AsyncMock())
    gen.client.create = AsyncMock(return_value=ref)
    gen._firewall_ha_group_data = AsyncMock(return_value={"group_id": {"from_pool": {"id": "pool"}}})
    gen._ensure_ha_interfaces = AsyncMock()

    @asynccontextmanager
    async def lock(key: str) -> AsyncGenerator[None, None]:
        yield

    gen.resource_lock = lock
    await gen._ensure_ha_pairs(["fw-01", "fw-02"], ha_kind="ManagedFirewallHA", role_label="firewall")
    if group_id is None:
        gen.client.create.assert_awaited_once_with(
            kind="ManagedFirewallHA", data={"id": "pair", "group_id": {"from_pool": {"id": "pool"}}}
        )
        ref.save.assert_awaited_once_with(allow_upsert=True)
    else:
        gen._firewall_ha_group_data.assert_not_awaited()
        gen.client.create.assert_not_awaited()
