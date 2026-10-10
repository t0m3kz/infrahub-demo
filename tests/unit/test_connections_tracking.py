"""Tracking-ownership flags on CablingMixin's shared write helpers.

ensure_vlan_subinterface() and upsert_p2p_addresses() are reached by callers
that own what they write (a dedicated FirewallContext, a segment's own
termination) and by callers that do not (the shared FirewallContext every
customer on a cluster reaches). ``track`` makes that a per-call decision:
the default keeps the plain upsert (the run claims the node), track=False
adds update_group_context=False so no run's delete_unused_nodes can reclaim it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.common import CommonGenerator
from generators.connections import CablingMixin, tracked_save_kwargs

_UNTRACKED = {"allow_upsert": True, "update_group_context": False}
_TRACKED = {"allow_upsert": True}


class _Gen(CablingMixin, CommonGenerator):
    """Minimal host for CablingMixin (CommonGenerator supplies _safe_rel_add)."""


def _make_gen() -> Any:
    """A _Gen with a mocked client/logger, bypassing InfrahubGenerator.__init__."""
    gen = _Gen.__new__(_Gen)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.client.get = AsyncMock(return_value=None)
    gen.client.create = AsyncMock()
    return gen


def _prefix(value: str = "100.65.0.0/31") -> Any:
    """A P2P prefix node as allocate_next_ip_prefix() returns it."""
    prefix = MagicMock()
    prefix.prefix.value = value
    prefix.ip_namespace = MagicMock(id="ns-default")
    return prefix


def _sub_iface(*, existing_peer_ids: list[str]) -> Any:
    """A created DcimVirtualInterface whose interface_capabilities already hold existing_peer_ids."""
    node = MagicMock(id="sub-1")
    node.save = AsyncMock()
    node.interface_capabilities.fetch = AsyncMock()
    node.interface_capabilities.peers = [MagicMock(id=peer_id) for peer_id in existing_peer_ids]
    return node


def _trunk() -> Any:
    """A physical trunk interface named Ethernet1/25."""
    trunk = MagicMock(id="trunk-1")
    trunk.name.value = "Ethernet1/25"
    return trunk


class TestTrackedSaveKwargs:
    """The single place the track flag turns into save() kwargs."""

    def test_track_true_is_a_plain_upsert(self) -> None:
        """Tracked writes keep today's exact call shape."""
        assert tracked_save_kwargs(True) == _TRACKED

    def test_track_false_opts_out_of_the_group(self) -> None:
        """Untracked writes add update_group_context=False."""
        assert tracked_save_kwargs(False) == _UNTRACKED


class TestUpsertP2pAddressesTrack:
    """upsert_p2p_addresses(track=...) decides who owns both ends of the link."""

    @pytest.mark.asyncio
    async def test_default_tracks_both_new_addresses(self) -> None:
        """Default: both newly created addresses are claimed by the run."""
        gen = _make_gen()
        created = [AsyncMock(id="ip-a"), AsyncMock(id="ip-b")]
        gen.client.create = AsyncMock(side_effect=created)

        result = await gen.upsert_p2p_addresses(_prefix())

        assert result == created
        for ip in created:
            ip.save.assert_awaited_once_with(**_TRACKED)

    @pytest.mark.asyncio
    async def test_track_false_saves_new_addresses_untracked(self) -> None:
        """track=False: both new addresses are written but not claimed."""
        gen = _make_gen()
        created = [AsyncMock(id="ip-a"), AsyncMock(id="ip-b")]
        gen.client.create = AsyncMock(side_effect=created)

        await gen.upsert_p2p_addresses(_prefix(), track=False)

        for ip in created:
            ip.save.assert_awaited_once_with(**_UNTRACKED)

    @pytest.mark.asyncio
    async def test_track_false_resaves_existing_addresses_untracked(self) -> None:
        """track=False on a re-run: existing addresses are reused (query
        before create kept) and re-saved untracked — never claimed."""
        gen = _make_gen()
        existing = [AsyncMock(id="ip-a"), AsyncMock(id="ip-b")]
        gen.client.get = AsyncMock(side_effect=existing)

        result = await gen.upsert_p2p_addresses(_prefix(), track=False)

        assert result == existing
        gen.client.create.assert_not_called()
        for ip in existing:
            ip.save.assert_awaited_once_with(**_UNTRACKED)

    @pytest.mark.asyncio
    async def test_track_false_with_description_rewrite_is_untracked(self) -> None:
        """The description-rewrite path (create with the found id) honours track too."""
        gen = _make_gen()
        gen.client.get = AsyncMock(side_effect=[MagicMock(id="ip-a"), MagicMock(id="ip-b")])
        rewritten = [AsyncMock(id="ip-a"), AsyncMock(id="ip-b")]
        gen.client.create = AsyncMock(side_effect=rewritten)

        await gen.upsert_p2p_addresses(_prefix(), description="ctx link", track=False)

        assert [c.kwargs["data"]["id"] for c in gen.client.create.call_args_list] == ["ip-a", "ip-b"]
        for ip in rewritten:
            ip.save.assert_awaited_once_with(**_UNTRACKED)


class TestEnsureVlanSubinterfaceTrack:
    """ensure_vlan_subinterface(track=...) covers the sub-interface save and its capability re-save."""

    async def _run(self, gen: Any, *, track: bool | None) -> Any:
        """Call ensure_vlan_subinterface with or without an explicit track kwarg."""
        kwargs: dict[str, Any] = {
            "device_id": "fw-1",
            "device_name": "FW1",
            "trunk_iface": _trunk(),
            "vlan_id_value": 3000,
            "capability_obj": MagicMock(id="ctx-1"),
            "ip_address_id": "ip-a",
        }
        if track is not None:
            kwargs["track"] = track
        return await gen.ensure_vlan_subinterface(**kwargs)

    @pytest.mark.asyncio
    async def test_default_tracks_both_saves(self) -> None:
        """Default: the sub-interface and its capability-link re-save are both tracked."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=[])
        gen.client.create = AsyncMock(return_value=node)

        result = await self._run(gen, track=None)

        assert result is node
        assert node.save.await_args_list == [((), _TRACKED), ((), _TRACKED)]

    @pytest.mark.asyncio
    async def test_track_false_makes_both_saves_untracked(self) -> None:
        """track=False: neither the sub-interface save nor the capability re-save claims the node."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=[])
        gen.client.create = AsyncMock(return_value=node)

        await self._run(gen, track=False)

        assert node.save.await_args_list == [((), _UNTRACKED), ((), _UNTRACKED)]
        data = gen.client.create.call_args.kwargs["data"]
        assert data["name"] == "Ethernet1/25.3000"
        assert data["ip_address"] == {"id": "ip-a"}

    @pytest.mark.asyncio
    async def test_track_false_with_capability_already_linked_saves_once_untracked(self) -> None:
        """Re-run with the capability already linked: one save, still untracked."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1"])
        gen.client.create = AsyncMock(return_value=node)

        await self._run(gen, track=False)

        node.save.assert_awaited_once_with(**_UNTRACKED)


class TestUpsertPrefixAddresses:
    """upsert_prefix_addresses() is the offset-addressed core; upsert_p2p_addresses() is its (0, 1) case."""

    @pytest.mark.asyncio
    async def test_addresses_land_on_the_requested_offsets_with_the_prefix_length(self) -> None:
        gen = _make_gen()
        created = {offset: AsyncMock(id=f"ip-{offset}") for offset in (1, 4, 5, 6)}
        gen.client.create = AsyncMock(side_effect=[created[1], created[4], created[5], created[6]])

        result = await gen.upsert_prefix_addresses(_prefix("100.66.0.8/29"), offsets=(1, 4, 5, 6))

        assert result == created
        addresses = [c.kwargs["data"]["address"] for c in gen.client.create.call_args_list]
        assert addresses == ["100.66.0.9/29", "100.66.0.12/29", "100.66.0.13/29", "100.66.0.14/29"]
        for ip in created.values():
            ip.save.assert_awaited_once_with(**_TRACKED)

    @pytest.mark.asyncio
    async def test_found_addresses_are_resaved_with_the_callers_tracking(self) -> None:
        gen = _make_gen()
        found = [AsyncMock(id="ip-a"), AsyncMock(id="ip-b")]
        gen.client.get = AsyncMock(side_effect=found)

        await gen.upsert_prefix_addresses(_prefix("100.66.0.0/29"), offsets=(1, 4), track=False)

        gen.client.create.assert_not_called()
        for ip in found:
            ip.save.assert_awaited_once_with(**_UNTRACKED)

    @pytest.mark.asyncio
    async def test_p2p_wrapper_keeps_its_list_of_two_in_offset_order(self) -> None:
        gen = _make_gen()
        first, second = AsyncMock(id="ip-0"), AsyncMock(id="ip-1")
        gen.client.create = AsyncMock(side_effect=[first, second])

        result = await gen.upsert_p2p_addresses(_prefix("100.65.0.0/31"))

        assert result == [first, second]
        assert [c.kwargs["data"]["address"] for c in gen.client.create.call_args_list] == [
            "100.65.0.0/31",
            "100.65.0.1/31",
        ]


class TestEnsureVlanSubinterfaceExtraCapabilities:
    """A firewall-context leg carries its context AND the exchange(s) it belongs to."""

    async def _run(self, gen: Any, extras: list[Any]) -> Any:
        return await gen.ensure_vlan_subinterface(
            device_id="fw-1",
            device_name="FW1",
            trunk_iface=_trunk(),
            vlan_id_value=3400,
            capability_obj=MagicMock(id="ctx-1"),
            extra_capability_objs=extras,
        )

    @pytest.mark.asyncio
    async def test_only_missing_capabilities_are_added_in_one_resave(self) -> None:
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1"])
        gen.client.create = AsyncMock(return_value=node)
        added: list[str] = []
        gen._safe_rel_add = AsyncMock(side_effect=lambda rel, obj: added.append(obj.id))

        await self._run(gen, [MagicMock(id="ex-1"), MagicMock(id="ex-2")])

        assert added == ["ex-1", "ex-2"]
        assert node.save.await_count == 2

    @pytest.mark.asyncio
    async def test_everything_already_linked_saves_once(self) -> None:
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1", "ex-1"])
        gen.client.create = AsyncMock(return_value=node)

        await self._run(gen, [MagicMock(id="ex-1")])

        node.save.assert_awaited_once_with(**_TRACKED)
