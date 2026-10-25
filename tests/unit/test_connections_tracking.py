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
from generators.protocols import DcimVirtualInterface, IpamIPAddress

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
    gen.client.filters = AsyncMock(return_value=[])
    gen.client.create = AsyncMock()
    return gen


def _address(address: str, ip_id: str) -> Any:
    """An IpamIPAddress node as the server returns it."""
    node = AsyncMock(id=ip_id)
    node.address = MagicMock(value=address)
    return node


def _prefix(value: str = "100.65.0.0/31") -> Any:
    """A P2P prefix node as allocate_next_ip_prefix() returns it."""
    prefix = MagicMock()
    prefix.prefix.value = value
    prefix.ip_namespace = MagicMock(id="ns-default")
    return prefix


def _sub_iface(*, existing_peer_ids: list[str], gen: Any = None) -> Any:
    """A created DcimVirtualInterface; with ``gen``, the stored node its capabilities are read from.

    The created node does not know the capabilities the server holds, so the helper reads them off one
    get(include=["capabilities"]) of the stored node, which is ``node.stored`` here (saved when a capability
    is added).
    """
    node = MagicMock(id="sub-1")
    node.save = AsyncMock()
    node.stored = MagicMock(id="sub-1")
    node.stored.save = AsyncMock()
    node.stored.capabilities.peers = [MagicMock(id=peer_id) for peer_id in existing_peer_ids]
    node.stored.capabilities.add = MagicMock()
    if gen is not None:
        gen.client.create = AsyncMock(return_value=node)
        gen.client.get = AsyncMock(return_value=node.stored)
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
        existing = [_address("100.65.0.0/31", "ip-a"), _address("100.65.0.1/31", "ip-b")]
        gen.client.filters = AsyncMock(return_value=existing)

        result = await gen.upsert_p2p_addresses(_prefix(), track=False)

        assert result == existing
        gen.client.create.assert_not_called()
        for ip in existing:
            ip.save.assert_awaited_once_with(**_UNTRACKED)

    @pytest.mark.asyncio
    async def test_track_false_with_description_rewrite_is_untracked(self) -> None:
        """The description-rewrite path (create with the found id) honours track too."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(
            return_value=[_address("100.65.0.0/31", "ip-a"), _address("100.65.0.1/31", "ip-b")]
        )
        rewritten = [AsyncMock(id="ip-a"), AsyncMock(id="ip-b")]
        gen.client.create = AsyncMock(side_effect=rewritten)

        await gen.upsert_p2p_addresses(_prefix(), description="ctx link", track=False)

        assert [c.kwargs["data"]["id"] for c in gen.client.create.call_args_list] == ["ip-a", "ip-b"]
        for ip in rewritten:
            ip.save.assert_awaited_once_with(**_UNTRACKED)


class TestUpsertIpAddress:
    """upsert_ip_address() is the one find-or-create of an address, also for an address no prefix offset names."""

    @pytest.mark.asyncio
    async def test_namespace_by_id_is_queried_and_created_by_id(self) -> None:
        """A cloud instance's own address: the namespace is known by id only."""
        gen = _make_gen()
        created = AsyncMock(id="ip-a")
        gen.client.create = AsyncMock(return_value=created)

        result = await gen.upsert_ip_address(
            address="10.30.1.11/24", ip_namespace={"id": "ns-prod"}, description="instance", track=False
        )

        assert result is created
        assert gen.client.filters.await_args.kwargs["ip_namespace__ids"] == ["ns-prod"]
        assert gen.client.create.await_args_list[0].kwargs["data"] == {
            "address": "10.30.1.11/24",
            "ip_namespace": {"id": "ns-prod"},
            "description": "instance",
        }
        created.save.assert_awaited_once_with(**_UNTRACKED)

    @pytest.mark.asyncio
    async def test_existing_address_without_description_is_resaved_as_is(self) -> None:
        gen = _make_gen()
        existing = AsyncMock(id="ip-a")
        existing.address = MagicMock(value="10.30.1.11/24")
        gen.client.filters = AsyncMock(return_value=[existing])

        assert await gen.upsert_ip_address(address="10.30.1.11/24", ip_namespace={"id": "ns-prod"}) is existing
        gen.client.create.assert_not_called()
        existing.save.assert_awaited_once_with(**_TRACKED)


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
        node = _sub_iface(existing_peer_ids=[], gen=gen)

        result = await self._run(gen, track=None)

        assert result is node
        node.save.assert_awaited_once_with(**_TRACKED)
        node.stored.save.assert_awaited_once_with(**_TRACKED)

    @pytest.mark.asyncio
    async def test_track_false_makes_both_saves_untracked(self) -> None:
        """track=False: neither the sub-interface save nor the capability re-save claims the node."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=[], gen=gen)

        await self._run(gen, track=False)

        node.save.assert_awaited_once_with(**_UNTRACKED)
        node.stored.save.assert_awaited_once_with(**_UNTRACKED)
        data = gen.client.create.call_args.kwargs["data"]
        assert data["name"] == "Ethernet1/25.3000"
        assert data["ip_address"] == {"id": "ip-a"}

    @pytest.mark.asyncio
    async def test_track_false_with_capability_already_linked_saves_once_untracked(self) -> None:
        """Re-run with the capability already linked: one save, still untracked."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1"], gen=gen)

        await self._run(gen, track=False)

        node.save.assert_awaited_once_with(**_UNTRACKED)
        node.stored.save.assert_not_awaited()


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
        found = [_address("100.66.0.1/29", "ip-a"), _address("100.66.0.4/29", "ip-b")]
        gen.client.filters = AsyncMock(return_value=found)

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

    async def _run(self, gen: Any, extras: list[Any], **kwargs: Any) -> Any:
        """ensure_vlan_subinterface for a context leg with the given extra capability nodes."""
        return await gen.ensure_vlan_subinterface(
            device_id="fw-1",
            device_name="FW1",
            trunk_iface=_trunk(),
            vlan_id_value=3400,
            capability_obj=MagicMock(id="ctx-1"),
            extra_capability_objs=extras,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_only_missing_capabilities_are_added_in_one_resave(self) -> None:
        """The stored capabilities are read once; only the ones not linked yet are added, then one re-save."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1"], gen=gen)

        await self._run(gen, [MagicMock(id="ex-1"), MagicMock(id="ex-2")])

        assert [c.args[0] for c in node.stored.capabilities.add.call_args_list] == [{"id": "ex-1"}, {"id": "ex-2"}]
        node.stored.save.assert_awaited_once_with(**_TRACKED)
        gen.client.get.assert_awaited_once_with(kind=DcimVirtualInterface, id="sub-1", include=["capabilities"])

    @pytest.mark.asyncio
    async def test_everything_already_linked_saves_once(self) -> None:
        """All wanted capabilities are linked: nothing is added and the stored node is not saved."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1", "ex-1"], gen=gen)

        await self._run(gen, [MagicMock(id="ex-1")])

        node.save.assert_awaited_once_with(**_TRACKED)
        node.stored.save.assert_not_awaited()
        node.stored.capabilities.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_capability_ids_are_linked_without_the_nodes(self) -> None:
        """A caller holding only ids passes capability_ids; duplicates of a node's id are linked once."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=["ctx-1"], gen=gen)

        await self._run(gen, [], capability_ids=["ex-9", "ctx-1", "ex-9"])

        assert [c.args[0] for c in node.stored.capabilities.add.call_args_list] == [{"id": "ex-9"}]
        node.stored.save.assert_awaited_once_with(**_TRACKED)

    @pytest.mark.asyncio
    async def test_capability_ids_alone_are_enough(self) -> None:
        """No capability node at all, only ids: the capabilities are still read and linked."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=[], gen=gen)

        await gen.ensure_vlan_subinterface(
            device_id="d", device_name="D", trunk_iface=_trunk(), vlan_id_value=3000, capability_ids=["cap-1"]
        )

        assert [c.args[0] for c in node.stored.capabilities.add.call_args_list] == [{"id": "cap-1"}]

    @pytest.mark.asyncio
    async def test_a_failed_capability_read_is_logged_and_returns_none(self) -> None:
        """The helper's error policy is unchanged: a failure is logged as an error and the result is None."""
        gen = _make_gen()
        _sub_iface(existing_peer_ids=[], gen=gen)
        gen.client.get = AsyncMock(side_effect=RuntimeError("boom"))

        assert await self._run(gen, []) is None
        gen.logger.error.assert_called_once()


class TestEnsureVlanSubinterfaceWithoutCapability:
    """A sub-interface the caller links itself (an office's WAN exit) has no capability and its own role."""

    @pytest.mark.asyncio
    async def test_no_capability_means_one_save_and_no_capability_round_trip(self) -> None:
        """No capability asked for: one save, and no read of the stored node at all."""
        gen = _make_gen()
        node = _sub_iface(existing_peer_ids=[], gen=gen)

        result = await gen.ensure_vlan_subinterface(
            device_id="ds-1",
            device_name="ds-c005-office01",
            trunk_iface=_trunk(),
            vlan_id_value=3800,
            ip_address_id="ip-a",
        )

        assert result is node
        node.save.assert_awaited_once_with(**_TRACKED)
        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_role_defaults_to_service_and_can_be_given(self) -> None:
        """The sub-interface role is "service" unless the caller gives one."""
        gen = _make_gen()
        _sub_iface(existing_peer_ids=[], gen=gen)

        await gen.ensure_vlan_subinterface(device_id="d", device_name="D", trunk_iface=_trunk(), vlan_id_value=3000)
        await gen.ensure_vlan_subinterface(
            device_id="d", device_name="D", trunk_iface=_trunk(), vlan_id_value=3800, role="uplink"
        )

        roles = [c.kwargs["data"]["role"] for c in gen.client.create.call_args_list]
        assert roles == ["service", "uplink"]


class TestFindAddressesBatch:
    """upsert_prefix_addresses() looks the whole prefix up in one read (find-before-create is kept)."""

    @pytest.mark.asyncio
    async def test_one_read_for_all_offsets_filtered_by_namespace(self) -> None:
        """Four offsets of a transit /29: one filters() carrying the four addresses and the namespace, no get."""
        gen = _make_gen()
        gen.client.create = AsyncMock(side_effect=[AsyncMock(id=f"ip-{i}") for i in range(4)])

        await gen.upsert_prefix_addresses(_prefix("100.66.0.8/29"), offsets=(1, 4, 5, 6))

        gen.client.filters.assert_awaited_once_with(
            kind=IpamIPAddress,
            address__values=["100.66.0.9/29", "100.66.0.12/29", "100.66.0.13/29", "100.66.0.14/29"],
            ip_namespace__ids=["ns-default"],
        )
        gen.client.get.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_found_nodes_are_matched_by_address_not_by_position(self) -> None:
        """The server returns the nodes in any order; only the missing address is created."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(return_value=[_address("100.65.0.1/31", "ip-b")])
        created = AsyncMock(id="ip-new")
        gen.client.create = AsyncMock(return_value=created)

        result = await gen.upsert_p2p_addresses(_prefix("100.65.0.0/31"))

        assert [c.kwargs["data"]["address"] for c in gen.client.create.call_args_list] == ["100.65.0.0/31"]
        assert [node.id for node in result] == ["ip-new", "ip-b"]

    @pytest.mark.asyncio
    async def test_ipv6_addresses_match_whatever_spelling_the_server_returns(self) -> None:
        """A /127 found as 2001:0db8:0:0:0:0:0:0/127 (expanded) still counts as found: no duplicate is created."""
        gen = _make_gen()
        gen.client.filters = AsyncMock(
            return_value=[_address("2001:0db8:0:0:0:0:0:0/127", "ip-a"), _address("2001:db8::1/127", "ip-b")]
        )

        result = await gen.upsert_p2p_addresses(_prefix("2001:db8::/127"))

        gen.client.create.assert_not_called()
        assert [node.id for node in result] == ["ip-a", "ip-b"]

    @pytest.mark.asyncio
    async def test_a_single_address_is_one_filtered_read_not_a_get(self) -> None:
        """One address takes the same batched read as a prefix's addresses."""
        gen = _make_gen()
        gen.client.create = AsyncMock(return_value=AsyncMock(id="ip-a"))

        await gen.upsert_ip_address(address="10.0.0.1/24", ip_namespace={"id": "ns-prod"})

        gen.client.filters.assert_awaited_once_with(
            kind=IpamIPAddress, address__values=["10.0.0.1/24"], ip_namespace__ids=["ns-prod"]
        )
        gen.client.get.assert_not_awaited()
