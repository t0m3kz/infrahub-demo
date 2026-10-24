"""The edge pair (interlink cable, /127 and FABRIC iBGP session) of ColocationFabricGenerator.

The generator lives in generators/topology/colocation_fabric.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.unit.colocation_zone_generator_helpers import _PROCESSES, _created, _made_virtual, _made_zone


def _pair_generator(*, legs: list | None = None) -> Any:
    """A generator whose pair step runs for real; only the SDK edges are mocked."""
    gen = _made_zone()
    pool = MagicMock(id="pool-ipv6")
    gen.ensure_sliced_pool = AsyncMock(return_value=pool)
    iface_a, iface_b = MagicMock(id="if-a"), MagicMock(id="if-b")
    gen.create_chain_cabling = AsyncMock(return_value=[[(iface_a, iface_b)]] if legs is None else legs)
    gen._ensure_routing_password = AsyncMock(return_value="key-1")
    gen.ensure_address_family = AsyncMock(side_effect=lambda afi, safi: f"af-{afi}-{safi}")
    created = MagicMock()
    created.save = AsyncMock()
    gen.client.create = AsyncMock(return_value=created)
    gen._pool = pool
    return gen


class TestEdgePair:
    """_ensure_edge_pair: interlink cable + /127 + FABRIC iBGP session, inside one zone."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("edges", [[], ["eg-fr201"], ["eg-fr201", "eg-fr202", "eg-fr203"]])
    async def test_anything_but_two_physical_edges_gets_no_pair(self, edges: list[str]) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=edges, overlay_process_ids=_PROCESSES)

        gen.ensure_sliced_pool.assert_not_awaited()
        gen.create_chain_cabling.assert_not_awaited()
        gen.client.create.assert_not_awaited()
        gen.acquire_resource_lock.assert_not_awaited()
        gen.logger.info.assert_called()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_whole_step_runs_under_the_zone_keyed_lock(self) -> None:
        """One lock per zone for the whole step: overlapping runs of one zone never both create any of it."""
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        gen.acquire_resource_lock.assert_awaited_once_with("edge-pair-fr2")
        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_the_lock_is_released_when_the_step_fails(self) -> None:
        gen = _pair_generator()
        gen.create_chain_cabling = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        gen.release_resource_lock.assert_awaited_once_with("lock-id")

    @pytest.mark.asyncio
    async def test_ipv6_technical_pool_is_a_zone_owned_slice(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr202", "eg-fr201"], overlay_process_ids=_PROCESSES)

        kwargs = gen.ensure_sliced_pool.await_args_list[-1].kwargs
        assert kwargs["pool_name"] == "fr2-technical-ipv6-pool"
        assert kwargs["parent_pool_name"] == "Technical-IPv6"
        assert kwargs["kind"] == "prefix"
        assert kwargs["role"] == "technical"

    @pytest.mark.asyncio
    async def test_cabled_by_interlink_role_with_a_127_from_that_pool(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr202", "eg-fr201"], overlay_process_ids=_PROCESSES)

        kwargs = gen.create_chain_cabling.await_args_list[-1].kwargs
        assert kwargs["hops"] == [
            {"devices": ["eg-fr201"], "down_role": "interlink"},
            {"devices": ["eg-fr202"], "up_role": "interlink"},
        ]
        assert kwargs["options"]["pool"] is gen._pool
        assert kwargs["options"]["p2p_prefix_length"] == 127

    @pytest.mark.asyncio
    async def test_peering_name_password_roles_and_families(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr202", "eg-fr201"], overlay_process_ids=_PROCESSES)

        (peering,) = _created(gen, "ManagedBGPPeering")
        assert peering["name"] == "FABRIC-eg-fr201-eg-fr202"
        assert peering["session_type"] == "IBGP"
        assert peering["peering_role"] == "fabric"
        assert peering["ttl"] == 1
        assert peering["password"] == {"id": "key-1"}
        assert peering["address_families"] == [{"id": "af-ipv6-unicast"}, {"id": "af-l2vpn-evpn"}]
        assert peering["capabilities"] == [{"id": "if-a"}, {"id": "if-b"}]
        assert gen._ensure_routing_password.await_args_list[-1].kwargs["name"] == "fr2-fabric-key"

    @pytest.mark.asyncio
    async def test_process_ids_come_from_the_evpn_step_not_a_name_query(self) -> None:
        gen = _pair_generator()
        gen.client.get = AsyncMock(side_effect=AssertionError("no lookup by name"))
        gen.client.filters = AsyncMock(side_effect=AssertionError("no lookup by name"))

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        (peering,) = _created(gen, "ManagedBGPPeering")
        assert peering["bgp_processes"] == [{"id": "bgp-1"}, {"id": "bgp-2"}]

    @pytest.mark.asyncio
    async def test_missing_overlay_process_skips_the_pair(self) -> None:
        gen = _pair_generator()

        await gen._ensure_edge_pair(
            physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids={"eg-fr201": "bgp-1"}
        )

        gen.create_chain_cabling.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_interlink_ports_writes_no_peering(self) -> None:
        gen = _pair_generator(legs=[[]])

        await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        gen.client.create.assert_not_awaited()
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_rerun_converges_on_the_same_keys(self) -> None:
        """Idempotent: a second run issues the same deterministic names and identifiers."""
        gen = _pair_generator()

        for _ in range(2):
            await gen._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)

        first, second = _created(gen, "ManagedBGPPeering")
        assert first == second
        assert gen.ensure_sliced_pool.await_count == 2
        assert {c.kwargs["pool_name"] for c in gen.ensure_sliced_pool.await_args_list} == {"fr2-technical-ipv6-pool"}
        assert gen.release_resource_lock.await_count == 2

    @pytest.mark.asyncio
    async def test_two_zones_of_one_metro_get_independent_pairs(self) -> None:
        """The pair stays inside one zone: FR2 and FR6 key their lock, pool and key by their own names."""
        fr2 = _pair_generator()
        fr6 = _pair_generator()
        fr6.fabric_name = "fr6"

        await fr2._ensure_edge_pair(physical_edge_names=["eg-fr201", "eg-fr202"], overlay_process_ids=_PROCESSES)
        await fr6._ensure_edge_pair(
            physical_edge_names=["eg-fr601", "eg-fr602"],
            overlay_process_ids=_PROCESSES
            | {
                "eg-fr601": "b-1",
                "eg-fr602": "b-2",
            },
        )

        assert fr2.acquire_resource_lock.await_args_list[-1].args == ("edge-pair-fr2",)
        assert fr6.acquire_resource_lock.await_args_list[-1].args == ("edge-pair-fr6",)
        assert fr6.ensure_sliced_pool.await_args_list[-1].kwargs["pool_name"] == "fr6-technical-ipv6-pool"

    @pytest.mark.asyncio
    async def test_virtual_colocation_never_forms_a_pair(self) -> None:
        """Two virtual edges are not physical: generate() hands the pair step an empty list, so it logs and returns."""
        gen = _made_virtual()
        gen.create_devices = AsyncMock(return_value=["eg-am-ne01", "eg-am-ne02"])
        gen.create_chain_cabling = AsyncMock()
        gen.ensure_sliced_pool = AsyncMock()

        names = await gen._create_fabric_devices(templates=gen.data["fabric_templates"])
        await gen._ensure_edge_pair(
            physical_edge_names=names.get("edge", []),
            overlay_process_ids={"eg-am-ne01": "b-1", "eg-am-ne02": "b-2"},
        )

        gen.create_chain_cabling.assert_not_awaited()
        gen.ensure_sliced_pool.assert_not_awaited()
        gen.acquire_resource_lock.assert_not_awaited()
