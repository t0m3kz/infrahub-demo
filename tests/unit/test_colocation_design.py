"""Design-driven pool sizing and envelope handling of the zone / virtual colocation generators.

A zone (TopologyColocationZone) and a virtual colocation (TopologyVirtualColocation) may state a ``design``
(TopologyTemplateColocationZone: the most edge routers, firewalls and load balancers), and their metro may state one
too (TopologyTemplateColocationMetro: the most zones and the zone designs they may use). The designs are the real rows
of data/bootstrap/14c_colocation_templates.yml (tests/unit/simulators/fabric_designs.py). What these tests pin:

- the prefix lengths of every pool a zone slices, derived from its design (hand-made golden table next to the sizing
  functions), the IPv6 slices from the COLO_ZONE_*_IPV6_BITS constants, the fixed fallback without a design;
- which pool the slice is cut from: the metro's supernet ONLY when both tiers have a design, else the bootstrap pool;
- the warnings (never errors): kit without a design, a design the metro was not sized for, a role past the design maximum;
- that the entries past a maximum are skipped deterministically, whatever order the query returns them in;
- that the metro's supernet is wide enough for max_zones zones of every zone design it lists.

The metro side (supernets, ASN block) is in test_colocation_generator.py, the sizing functions in
test_colocation_sizing.py, the check in test_colocation_placement_check.py.
"""

from __future__ import annotations

import itertools
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from infrahub_sdk.exceptions import NodeNotFoundError

from generators.fabric_sizing import (
    COLO_ZONE_LOOPBACK_IPV6_BITS,
    COLO_ZONE_TECHNICAL_IPV6_BITS,
    colo_zone_loopback_bits,
    colo_zone_management_bits,
    colo_zone_technical_bits,
    host_bits_to_prefix_length,
)
from generators.logger import GeneratorError
from generators.pools import PoolMixin
from generators.topology.colocation_fabric import (
    ColocationFabricGenerator,
    ColocationZoneGenerator,
    VirtualColocationGenerator,
)
from generators.topology.colocation_pools import (
    _COLO_LOOPBACK_PREFIX_LENGTH,
    _COLO_MANAGEMENT_PREFIX_LENGTH,
    _COLO_TECHNICAL_PREFIX_LENGTH,
    ATTACHED_SLOTS,
    COLO_POOL_SLOTS,
    LOOPBACK_IPV6_SLOT,
    LOOPBACK_SLOT,
    MANAGEMENT_SLOT,
    TECHNICAL_IPV6_SLOT,
    TECHNICAL_SLOT,
)
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS,
    _VIRTUAL_EDGE_TEMPLATE,
    _made_virtual,
    _made_zone,
)
from tests.unit.simulators.fabric_designs import (
    COLO_METRO_DESIGN_NAMES,
    COLO_ZONE_DESIGN_NAMES,
    colo_metro_design,
    colo_zone_design,
)

# zone design -> (loopback, management, technical) IPv4 prefix lengths, by hand from the design rows:
# EDGE holds 2 devices and 1 link, EDGE_FW 4 and 3, EDGE_FW_LB 6 and 5 (25% headroom, see test_colocation_sizing.py).
_GOLDEN_ZONE_LENGTHS = {
    "EDGE": (29, 29, 30),
    "EDGE_FW": (29, 29, 29),
    "EDGE_FW_LB": (29, 28, 28),
}
# The IPv6 slices are fixed by what they hold (one /128 VTEP per edge, one /127 interlink), not by the design.
_VTEP_IPV6_LENGTH = 120
_INTERLINK_IPV6_LENGTH = 124

_NODE_KINDS = pytest.mark.parametrize(
    ("node", "metro"),
    [(_made_zone, "fr"), (_made_virtual, "am")],
    ids=["zone", "virtual-colocation"],
)
# (metro has a design, node has a design): the supernets are used only when both do.
_COMBINATIONS = pytest.mark.parametrize(
    ("metro_has_design", "node_has_design", "from_metro"),
    [(False, False, False), (True, False, False), (False, True, False), (True, True, True)],
    ids=["neither", "metro-only", "zone-only", "both"],
)


def _sized(node: Any, *, design: str | None = None, metro_design: str | None = None) -> Any:
    """A zone or virtual colocation generator with the designs set and the pool upsert mocked at the SDK boundary."""
    gen = node()
    if design:
        gen.data["design"] = colo_zone_design(design)
    if metro_design:
        gen.data["parent"]["design"] = colo_metro_design(metro_design)
    gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"id-{kw['pool_name']}"))
    gen.attach_refs = AsyncMock()
    return gen


def _calls(gen: Any) -> dict[str, dict[str, Any]]:
    """The ensure_sliced_pool kwargs of a run, keyed by pool name."""
    return {c.kwargs["pool_name"]: c.kwargs for c in gen.ensure_sliced_pool.await_args_list}


def _flow(gen: Any, order: list[str] | None = None) -> Any:
    """Mock every step of generate() so a test can look at what it does between them."""
    calls = order if order is not None else []
    gen.wait_for_parent_generators = AsyncMock(side_effect=lambda *a: calls.append("wait") or False)
    gen._ensure_fabric_pools = AsyncMock(side_effect=lambda: calls.append("pools"))
    gen._create_fabric_devices = AsyncMock(return_value={})
    gen._cable_services = AsyncMock()
    gen._ensure_evpn = AsyncMock(return_value={})
    gen._ensure_edge_pair = AsyncMock()
    gen.ensure_context_vlan_pool = AsyncMock()
    return gen


def _entry(role: str, quantity: int, template_id: str, template: dict[str, Any] | None = None) -> dict[str, Any]:
    """A cleaned fabric_templates row; the default edge template exposes every service port role."""
    base = template or _EDGE_TEMPLATE_WITH_ALL_SERVICE_PORTS
    return {"role": role, "quantity": quantity, "template": {**base, "id": template_id}}


def _lengths(gen: Any) -> tuple[int, int, int]:
    calls = _calls(gen)
    return (
        calls[LOOPBACK_SLOT.name(gen.fabric_name)]["prefix_length"],
        calls[MANAGEMENT_SLOT.name(gen.fabric_name)]["prefix_length"],
        calls[TECHNICAL_SLOT.name(gen.fabric_name)]["prefix_length"],
    )


class TestPoolSlotTable:
    """ColoPoolSlot / COLO_POOL_SLOTS is the one table of names and sizes both tiers read."""

    def test_ipv6_constants_and_the_lengths_they_give(self) -> None:
        """8 host bits is a /120 (256 VTEP /128s), 4 host bits a /124 (eight /127 interlinks)."""
        assert COLO_ZONE_LOOPBACK_IPV6_BITS == 8
        assert COLO_ZONE_TECHNICAL_IPV6_BITS == 4
        assert host_bits_to_prefix_length(COLO_ZONE_LOOPBACK_IPV6_BITS, ipv6=True) == _VTEP_IPV6_LENGTH
        assert host_bits_to_prefix_length(COLO_ZONE_TECHNICAL_IPV6_BITS, ipv6=True) == _INTERLINK_IPV6_LENGTH

    def test_slot_names_are_unique_and_five(self) -> None:
        """Three IPv4 pools and two IPv6 pools; one suffix each, so no two slices collide on a name."""
        suffixes = [slot.suffix for slot in COLO_POOL_SLOTS]
        assert len(suffixes) == len(set(suffixes)) == 5
        assert [slot.name("fr2") for slot in COLO_POOL_SLOTS] == [
            "fr2-loopback-pool",
            "fr2-management-pool",
            "fr2-technical-pool",
            "fr2-loopback-ipv6-pool",
            "fr2-technical-ipv6-pool",
        ]

    def test_only_the_ipv4_slots_are_attached_to_the_owner(self) -> None:
        """The IPv6 slots have no relationship: they are found by name."""
        assert ATTACHED_SLOTS == (LOOPBACK_SLOT, MANAGEMENT_SLOT, TECHNICAL_SLOT)
        assert [slot.attribute for slot in ATTACHED_SLOTS] == ["loopback_pool", "management_pool", "technical_pool"]
        assert LOOPBACK_IPV6_SLOT.attribute is None
        assert TECHNICAL_IPV6_SLOT.attribute is None

    def test_zone_kinds_loopback_and_management_are_address_pools_technical_are_prefix_pools(self) -> None:
        """create_devices() feeds loopback/management pools to allocate_next_ip_address(); P2P links get prefixes."""
        assert {slot.suffix: slot.zone_kind for slot in COLO_POOL_SLOTS} == {
            "loopback-pool": "address",
            "management-pool": "address",
            "technical-pool": "prefix",
            "loopback-ipv6-pool": "address",
            "technical-ipv6-pool": "prefix",
        }

    def test_fallback_lengths_are_the_fixed_ones(self) -> None:
        """Without a design every slot answers with its fixed length (and the IPv6 ones are the same with one)."""
        assert [slot.zone_length(None) for slot in COLO_POOL_SLOTS] == [
            _COLO_LOOPBACK_PREFIX_LENGTH,
            _COLO_MANAGEMENT_PREFIX_LENGTH,
            _COLO_TECHNICAL_PREFIX_LENGTH,
            _VTEP_IPV6_LENGTH,
            _INTERLINK_IPV6_LENGTH,
        ]
        assert (_COLO_LOOPBACK_PREFIX_LENGTH, _COLO_MANAGEMENT_PREFIX_LENGTH, _COLO_TECHNICAL_PREFIX_LENGTH) == (
            28,
            28,
            26,
        )

    @pytest.mark.parametrize("design_name", COLO_ZONE_DESIGN_NAMES)
    def test_ipv6_slot_sizes_do_not_depend_on_the_design(self, design_name: str) -> None:
        design = colo_zone_design(design_name)

        assert LOOPBACK_IPV6_SLOT.zone_length(design) == LOOPBACK_IPV6_SLOT.zone_length(None) == _VTEP_IPV6_LENGTH
        assert (
            TECHNICAL_IPV6_SLOT.zone_length(design) == TECHNICAL_IPV6_SLOT.zone_length(None) == _INTERLINK_IPV6_LENGTH
        )

    def test_golden_table_covers_every_bootstrap_zone_design(self) -> None:
        """A new zone design must add its expected prefix lengths here."""
        assert set(_GOLDEN_ZONE_LENGTHS) == set(COLO_ZONE_DESIGN_NAMES)

    @pytest.mark.parametrize("metro_name", COLO_METRO_DESIGN_NAMES)
    def test_metro_supernets_hold_max_zones_slices_of_every_listed_zone_design(self, metro_name: str) -> None:
        """Consistency of the two tiers: 2**metro host bits >= max_zones * 2**zone host bits, for all five slots."""
        metro = colo_metro_design(metro_name)
        assert metro["zone_templates"]
        for zone in metro["zone_templates"]:
            for slot in COLO_POOL_SLOTS:
                total_bits = 128 if slot.ipv6 else 32
                metro_host_bits = total_bits - slot.metro_length(metro)
                zone_host_bits = total_bits - slot.zone_length(zone)
                assert 2**metro_host_bits >= metro["max_zones"] * 2**zone_host_bits, (
                    metro_name,
                    zone["name"],
                    slot.suffix,
                )


class TestZoneDesignSizing:
    """With a design the slices are sized by it (ColoPoolSlot.zone_length), whatever the metro has."""

    @_NODE_KINDS
    @pytest.mark.parametrize("design_name", COLO_ZONE_DESIGN_NAMES)
    @pytest.mark.asyncio
    async def test_loopback_management_and_technical_lengths_follow_the_design(
        self, node: Any, metro: str, design_name: str
    ) -> None:
        """The three IPv4 pools, against the hand-made table and the sizing functions."""
        gen = _sized(node, design=design_name)

        await gen._ensure_fabric_pools()

        design = colo_zone_design(design_name)
        assert _lengths(gen) == _GOLDEN_ZONE_LENGTHS[design_name]
        assert _lengths(gen) == (
            host_bits_to_prefix_length(colo_zone_loopback_bits(design), ipv6=False),
            host_bits_to_prefix_length(colo_zone_management_bits(design), ipv6=False),
            host_bits_to_prefix_length(colo_zone_technical_bits(design), ipv6=False),
        )

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_pool_find_or_create_runs_under_a_lock_keyed_by_the_node_name(self, node: Any, metro: str) -> None:
        """Two overlapping runs of one zone would otherwise create twin pools handing out the same addresses."""
        gen = _sized(node, design="EDGE_FW")

        await gen._ensure_fabric_pools()

        gen.acquire_resource_lock.assert_awaited_once_with(f"colocation-pools-{gen.fabric_name}")
        gen.release_resource_lock.assert_awaited_once()

    @_NODE_KINDS
    @pytest.mark.parametrize("design_name", [None, *COLO_ZONE_DESIGN_NAMES])
    @pytest.mark.asyncio
    async def test_vtep_slice_is_a_slash_120_with_or_without_a_design(
        self, node: Any, metro: str, design_name: str | None
    ) -> None:
        """The VTEP loopback pool comes from COLO_ZONE_LOOPBACK_IPV6_BITS, an address pool in Loopback-IPv6."""
        gen = _sized(node, design=design_name)

        await gen._ensure_fabric_pools()

        call = _calls(gen)[LOOPBACK_IPV6_SLOT.name(gen.fabric_name)]
        assert call["prefix_length"] == _VTEP_IPV6_LENGTH
        assert call["parent_pool_name"] == "Loopback-IPv6"
        assert call["kind"] == "address"
        assert gen._vtep_pool.id == f"id-{gen.fabric_name}-loopback-ipv6-pool"

    @_NODE_KINDS
    @pytest.mark.parametrize("design_name", [None, *COLO_ZONE_DESIGN_NAMES])
    @pytest.mark.asyncio
    async def test_interlink_slice_is_a_slash_124_with_or_without_a_design(
        self, node: Any, metro: str, design_name: str | None
    ) -> None:
        """The edge pair's interlink pool comes from COLO_ZONE_TECHNICAL_IPV6_BITS, a prefix pool in Technical-IPv6."""
        gen = _sized(node, design=design_name)

        await gen._slice_pool(TECHNICAL_IPV6_SLOT)

        call = _calls(gen)[f"{gen.fabric_name}-technical-ipv6-pool"]
        assert call["prefix_length"] == _INTERLINK_IPV6_LENGTH
        assert call["parent_pool_name"] == "Technical-IPv6"
        assert call["kind"] == "prefix"
        assert call["role"] == "technical"

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_no_design_uses_the_fixed_fallback_lengths(self, node: Any, metro: str) -> None:
        gen = _sized(node)

        await gen._ensure_fabric_pools()

        assert _lengths(gen) == (28, 28, 26)

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_the_three_ipv4_pools_are_attached_to_the_node_with_the_design_too(
        self, node: Any, metro: str
    ) -> None:
        gen = _sized(node, design="EDGE_FW", metro_design="S")

        await gen._ensure_fabric_pools()

        gen.attach_refs.assert_awaited_once()
        kwargs = gen.attach_refs.await_args_list[-1].kwargs
        assert kwargs["kind"] is type(gen)._node_kind
        assert kwargs["refs"] == {
            "loopback_pool": f"id-{gen.fabric_name}-loopback-pool",
            "management_pool": f"id-{gen.fabric_name}-management-pool",
            "technical_pool": f"id-{gen.fabric_name}-technical-pool",
        }


class TestParentPoolPreload:
    """The five parents of a node's slices are read once, not one get per slice."""

    @_NODE_KINDS
    @_COMBINATIONS
    @pytest.mark.asyncio
    async def test_all_parents_are_preloaded_in_one_read(
        self, node: Any, metro: str, metro_has_design: bool, node_has_design: bool, from_metro: bool
    ) -> None:
        """Whatever the slices are cut from (metro supernets or bootstrap pools), the preload names exactly those."""
        gen = _sized(
            node, design="EDGE_FW" if node_has_design else None, metro_design="S" if metro_has_design else None
        )
        gen.preload_parent_pools = AsyncMock()

        await gen._ensure_fabric_pools()

        gen.preload_parent_pools.assert_awaited_once()
        call = gen.preload_parent_pools.await_args
        assert call is not None
        (names,) = call.args
        expected = [slot.name(metro) if from_metro else slot.bootstrap_pool for slot in COLO_POOL_SLOTS]
        assert list(names) == expected

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_preloaded_parents_are_what_the_slices_are_cut_from(self, node: Any, metro: str) -> None:
        """With the pools in the preload, no per-slice get of a parent pool is made (real preload, real lookup)."""
        gen = _sized(node)
        gen.ensure_sliced_pool = PoolMixin.ensure_sliced_pool.__get__(gen)
        bootstrap = [slot.bootstrap_pool for slot in COLO_POOL_SLOTS]
        pools = [MagicMock(id=f"id-{name}") for name in bootstrap]
        for pool, name in zip(pools, bootstrap):
            pool.name = MagicMock(value=name)
        gen.client.filters = AsyncMock(return_value=pools)
        gen.client.get = AsyncMock()
        gen.client.allocate_next_ip_prefix = AsyncMock(return_value=MagicMock())
        created = MagicMock()
        created.save = AsyncMock()
        gen.client.create = AsyncMock(return_value=created)

        await gen._ensure_fabric_pools()

        gen.client.get.assert_not_awaited()
        assert gen.client.filters.await_count == 1
        assert gen.client.allocate_next_ip_prefix.await_count == 4


class TestSliceParent:
    """The slice is cut from the metro's supernet ONLY when both the metro and the node have a design."""

    @_NODE_KINDS
    @_COMBINATIONS
    @pytest.mark.asyncio
    async def test_parent_pool_of_every_slot(
        self, node: Any, metro: str, metro_has_design: bool, node_has_design: bool, from_metro: bool
    ) -> None:
        """All five slices of the four combinations: `{metro}-{suffix}` or the bootstrap pool."""
        gen = _sized(
            node, design="EDGE_FW" if node_has_design else None, metro_design="S" if metro_has_design else None
        )

        await gen._ensure_fabric_pools()
        await gen._slice_pool(TECHNICAL_IPV6_SLOT)

        assert gen._slices_from_metro is from_metro
        calls = _calls(gen)
        assert len(calls) == 5
        for slot in COLO_POOL_SLOTS:
            expected = slot.name(metro) if from_metro else slot.bootstrap_pool
            assert calls[slot.name(gen.fabric_name)]["parent_pool_name"] == expected

    @_NODE_KINDS
    @_COMBINATIONS
    @pytest.mark.asyncio
    async def test_the_length_follows_the_node_design_whatever_the_parent(
        self, node: Any, metro: str, metro_has_design: bool, node_has_design: bool, from_metro: bool
    ) -> None:
        """The metro design never changes the zone's size: the zone design, else the fallback."""
        gen = _sized(
            node, design="EDGE_FW_LB" if node_has_design else None, metro_design="M" if metro_has_design else None
        )

        await gen._ensure_fabric_pools()

        assert _lengths(gen) == ((29, 28, 28) if node_has_design else (28, 28, 26))

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_supernet_names_are_the_metros_not_the_nodes(self, node: Any, metro: str) -> None:
        """The metro name is lower-cased from the parent, even when the node name is another one (AM-NE under AM)."""
        gen = _sized(node, design="EDGE", metro_design="S")

        await gen._ensure_fabric_pools()

        parents = {call["parent_pool_name"] for call in _calls(gen).values()}
        assert parents == {
            f"{metro}-loopback-pool",
            f"{metro}-management-pool",
            f"{metro}-technical-pool",
            f"{metro}-loopback-ipv6-pool",
        }


class TestWaitForTheMetroGenerator:
    """The metro's supernets are written by add_colocation_metro, whose created-trigger can still be running."""

    @_NODE_KINDS
    @_COMBINATIONS
    @pytest.mark.asyncio
    async def test_waits_only_when_it_will_slice_from_the_metro(
        self, node: Any, metro: str, metro_has_design: bool, node_has_design: bool, from_metro: bool
    ) -> None:
        gen = node()
        if node_has_design:
            gen.data["design"] = colo_zone_design("EDGE_FW")
        if metro_has_design:
            gen.data["parent"]["design"] = colo_metro_design("S")
        order: list[str] = []
        _flow(gen, order)

        await gen.generate({gen.graphql_root_key: [gen.data]})

        if from_metro:
            gen.wait_for_parent_generators.assert_awaited_once_with(("add_colocation_metro",), gen.data["parent"]["id"])
            assert order == ["wait", "pools"]
        else:
            gen.wait_for_parent_generators.assert_not_awaited()
            assert order == ["pools"]


class TestMissingSupernet:
    """A supernet (or bootstrap pool) that does not exist fails the run: slicing would otherwise be silent nonsense."""

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_missing_metro_supernet_fails_the_run_naming_the_pool(self, node: Any, metro: str) -> None:
        gen = _sized(node, design="EDGE", metro_design="S")
        gen.ensure_sliced_pool = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="CoreIPPrefixPool", identifier={})
        )

        with pytest.raises(GeneratorError, match=f"pool '{metro}-loopback-pool'.*does not exist"):
            await gen._ensure_fabric_pools()

        gen.logger.error.assert_called_once()
        assert "add_colocation_metro" in gen.logger.error.call_args.args[0]
        gen.attach_refs.assert_not_awaited()

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_missing_bootstrap_pool_fails_the_same_way(self, node: Any, metro: str) -> None:
        gen = _sized(node)
        gen.ensure_sliced_pool = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="CoreIPPrefixPool", identifier={})
        )

        with pytest.raises(GeneratorError, match="pool 'Loopback-IPv4'"):
            await gen._ensure_fabric_pools()

        gen.attach_refs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_real_lookup_asks_for_the_supernet_by_name(self) -> None:
        """Through the real ensure_sliced_pool: the retrying parent lookup is called with the metro's supernet."""
        gen = _sized(_made_zone, design="EDGE", metro_design="S")
        del gen.ensure_sliced_pool
        gen._get_parent_pool_with_retry = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="CoreIPPrefixPool", identifier={})
        )

        with pytest.raises(GeneratorError, match="fr-loopback-pool"):
            await gen._ensure_fabric_pools()

        gen._get_parent_pool_with_retry.assert_awaited_once_with("fr-loopback-pool")

    @pytest.mark.asyncio
    async def test_missing_interlink_supernet_fails_the_edge_pair_slice(self) -> None:
        gen = _sized(_made_zone, design="EDGE", metro_design="S")
        gen.ensure_sliced_pool = AsyncMock(
            side_effect=NodeNotFoundError(branch_name="main", node_type="CoreIPPrefixPool", identifier={})
        )

        with pytest.raises(GeneratorError, match="fr-technical-ipv6-pool"):
            await gen._slice_pool(TECHNICAL_IPV6_SLOT)


class TestDesignWarnings:
    """Findings against the envelope are warnings (an error would fail the task), once per finding."""

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_kit_without_a_design_warns_once_about_the_fallback(self, node: Any, metro: str) -> None:
        gen = _flow(node())

        await gen.generate({gen.graphql_root_key: [gen.data]})

        warnings = [c.args[0] for c in gen.logger.warning.call_args_list]
        assert len(warnings) == 1
        assert "no design" in warnings[0]
        assert f"/{_COLO_LOOPBACK_PREFIX_LENGTH}" in warnings[0]
        assert f"/{_COLO_TECHNICAL_PREFIX_LENGTH}" in warnings[0]
        gen.logger.error.assert_not_called()
        # The run still goes on, with the fallback sizes.
        gen._ensure_fabric_pools.assert_awaited_once()

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_kit_without_a_design_under_a_metro_with_one_still_falls_back(self, node: Any, metro: str) -> None:
        """The metro's envelope does not size a zone that has none: bootstrap pools, fixed lengths, one warning."""
        gen = _sized(node, metro_design="S")
        _flow(gen)
        gen._ensure_fabric_pools = ColocationFabricGenerator._ensure_fabric_pools.__get__(gen)

        await gen.generate({gen.graphql_root_key: [gen.data]})

        assert len([c for c in gen.logger.warning.call_args_list if "no design" in c.args[0]]) == 1
        assert _lengths(gen) == (28, 28, 26)
        assert {call["parent_pool_name"] for call in _calls(gen).values()} == {
            "Loopback-IPv4",
            "Management-IPv4",
            "Technical-IPv4",
            "Loopback-IPv6",
        }

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_a_design_that_holds_the_kit_warns_about_nothing(self, node: Any, metro: str) -> None:
        gen = _sized(node, design="EDGE", metro_design="S")
        _flow(gen)

        await gen.generate({gen.graphql_root_key: [gen.data]})

        gen.logger.warning.assert_not_called()
        gen.logger.error.assert_not_called()

    @_NODE_KINDS
    @pytest.mark.asyncio
    async def test_no_kit_means_no_design_warning(self, node: Any, metro: str) -> None:
        """A zone with no kit is legal and common: nothing to size, nothing to warn about."""
        gen = _flow(node(fabric_templates=[]))

        await gen.generate({gen.graphql_root_key: [gen.data]})

        gen.logger.warning.assert_not_called()

    @_NODE_KINDS
    def test_design_not_among_the_metro_designs_zone_templates_warns_and_keeps_the_entries(
        self, node: Any, metro: str
    ) -> None:
        """The metro's supernets were not sized for it; the check errors, the generator only warns."""
        gen = _sized(node, metro_design="S")
        gen.data["design"] = {**colo_zone_design("EDGE"), "name": "ODD"}
        entries = gen.data["fabric_templates"]

        assert gen._valid_fabric_templates() == entries

        warnings = [c.args[0] for c in gen.logger.warning.call_args_list]
        assert len(warnings) == 1
        assert "'ODD'" in warnings[0]
        assert "zone_templates" in warnings[0]
        assert "['EDGE', 'EDGE_FW', 'EDGE_FW_LB']" in warnings[0]
        gen.logger.error.assert_not_called()

    @_NODE_KINDS
    def test_design_among_the_metro_designs_zone_templates_does_not_warn(self, node: Any, metro: str) -> None:
        gen = _sized(node, design="EDGE_FW_LB", metro_design="S")

        gen._valid_fabric_templates()

        gen.logger.warning.assert_not_called()

    @_NODE_KINDS
    def test_a_design_with_no_metro_design_is_never_compared(self, node: Any, metro: str) -> None:
        gen = _sized(node)
        gen.data["design"] = {**colo_zone_design("EDGE"), "name": "ODD"}

        gen._valid_fabric_templates()

        gen.logger.warning.assert_not_called()


class TestOverMaxEntries:
    """A role whose total passes the design maximum is skipped whole, judged in template-id order."""

    @staticmethod
    def _zone_with(entries: list[dict[str, Any]], design: dict[str, Any]) -> Any:
        gen = _made_zone(fabric_templates=entries)
        gen.data["design"] = design
        return gen

    @staticmethod
    def _ids(entries: list[dict[str, Any]]) -> list[str]:
        return [entry["template"]["id"] for entry in entries]

    def test_role_total_exactly_at_the_maximum_is_kept(self) -> None:
        entries = [_entry("edge", 2, "t-edge"), _entry("firewall", 2, "t-fw", _EDGE_TEMPLATE)]
        gen = self._zone_with(entries, colo_zone_design("EDGE_FW"))

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    def test_every_role_is_judged_against_its_own_maximum(self) -> None:
        """Edge, firewall and load-balancer totals do not share a budget."""
        entries = [
            _entry("edge", 2, "t-edge"),
            _entry("firewall", 2, "t-fw", _EDGE_TEMPLATE),
            _entry("load-balancer", 2, "t-lb", _EDGE_TEMPLATE),
        ]
        gen = self._zone_with(entries, colo_zone_design("EDGE_FW_LB"))

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize(
        ("role", "design_name", "key", "maximum"),
        [
            ("edge", "EDGE", "max_edges", 2),
            ("firewall", "EDGE_FW", "max_firewalls", 2),
            ("load-balancer", "EDGE_FW_LB", "max_load_balancers", 2),
            ("load-balancer", "EDGE_FW", "max_load_balancers", 0),
            ("firewall", "EDGE", "max_firewalls", 0),
        ],
    )
    def test_one_entry_past_the_maximum_is_skipped_with_a_warning_naming_the_limit(
        self, role: str, design_name: str, key: str, maximum: int
    ) -> None:
        """Beyond the envelope the pools are too small: the entry is dropped before any device exists."""
        edge = _entry("edge", 2, "t-edge")
        beyond = _entry(role, maximum + 1, "t-beyond", _EDGE_TEMPLATE)
        gen = self._zone_with([edge, beyond] if role != "edge" else [beyond], colo_zone_design(design_name))

        kept = gen._valid_fabric_templates()

        assert "t-beyond" not in self._ids(kept)
        warnings = [c.args[0] for c in gen.logger.warning.call_args_list]
        assert len(warnings) == 1
        assert f"{key}={maximum}" in warnings[0]
        assert f"role={role!r}" in warnings[0]
        assert design_name in warnings[0]
        gen.logger.error.assert_not_called()

    def test_an_entry_that_only_partly_fits_is_skipped_whole(self) -> None:
        """Two edges at max_edges 2 with one already counted: the second entry (quantity 2) goes, not half of it."""
        first, second = _entry("edge", 1, "t-a"), _entry("edge", 2, "t-b")
        gen = self._zone_with([first, second], colo_zone_design("EDGE"))

        assert gen._valid_fabric_templates() == [first]
        assert gen.logger.warning.call_count == 1

    def test_without_a_design_nothing_is_skipped(self) -> None:
        """No envelope, no cap here (the generator warns about the fallback once per run, the check errors)."""
        entries = [_entry("edge", 10, "t-edge"), _entry("edge", 10, "t-edge-2")]
        gen = _made_zone(fabric_templates=entries)

        assert gen._valid_fabric_templates() == entries
        gen.logger.warning.assert_not_called()

    @pytest.mark.parametrize("order", list(itertools.permutations(range(3))), ids=lambda o: "".join(map(str, o)))
    def test_same_entries_are_kept_whatever_order_the_query_returns_them_in(self, order: tuple[int, ...]) -> None:
        """Judged in template-id order (t-1 then t-2 then t-3) against max_edges 3: t-3 is always the one skipped.

        The kept list keeps the QUERY order, so the device names (allocated in that order) stay stable."""
        design = {**colo_zone_design("EDGE_FW_LB"), "max_edges": 3}
        rows = [_entry("edge", 1, "t-3"), _entry("edge", 2, "t-1"), _entry("edge", 1, "t-2")]
        entries = [rows[i] for i in order]
        gen = self._zone_with(entries, design)

        kept = gen._valid_fabric_templates()

        assert sorted(self._ids(kept)) == ["t-1", "t-2"]
        assert self._ids(kept) == [entry["template"]["id"] for entry in entries if entry["template"]["id"] != "t-3"]
        assert gen.logger.warning.call_count == 1
        assert "(quantity 1)" in gen.logger.warning.call_args.args[0]

    @pytest.mark.parametrize("reverse", [False, True])
    def test_equal_template_entries_tie_break_on_quantity_not_on_query_order(self, reverse: bool) -> None:
        """Two entries of one role on one template: the smaller quantity is judged first, whatever the query order."""
        design = {**colo_zone_design("EDGE_FW_LB"), "max_edges": 2}
        rows = [_entry("edge", 2, "t-same"), _entry("edge", 1, "t-same")]
        gen = self._zone_with(list(reversed(rows)) if reverse else rows, design)

        kept = gen._valid_fabric_templates()

        assert [entry["quantity"] for entry in kept] == [1]

    def test_a_rerun_with_a_reordered_query_skips_the_same_entry(self) -> None:
        """Idempotency: the same skip decision on a second run, so the device set does not flip between runs."""
        design = {**colo_zone_design("EDGE_FW_LB"), "max_edges": 3}
        rows = [_entry("edge", 1, "t-3"), _entry("edge", 2, "t-1"), _entry("edge", 1, "t-2")]
        first = self._zone_with(rows, design)
        second = self._zone_with(list(reversed(rows)), design)

        assert sorted(self._ids(first._valid_fabric_templates())) == sorted(self._ids(second._valid_fabric_templates()))

    def test_virtual_colocation_is_held_to_its_design_too(self) -> None:
        gen = _made_virtual(fabric_templates=[_entry("edge", 3, "t-v", _VIRTUAL_EDGE_TEMPLATE)])
        gen.data["design"] = colo_zone_design("EDGE")

        assert gen._valid_fabric_templates() == []
        gen.logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipped_entries_never_reach_the_device_pass(self) -> None:
        """generate() hands only the kept entries to _create_fabric_devices."""
        kept, skipped = _entry("edge", 2, "t-a"), _entry("edge", 2, "t-b")
        gen = _flow(self._zone_with([kept, skipped], colo_zone_design("EDGE")))

        await gen.generate({"TopologyColocationZone": [gen.data]})

        assert gen._create_fabric_devices.await_args_list[-1].kwargs["templates"] == [kept]

    @pytest.mark.asyncio
    async def test_when_every_entry_is_skipped_the_run_is_a_no_op(self) -> None:
        """A load balancer under a design that holds none: nothing usable, so no pools and no devices."""
        gen = _flow(self._zone_with([_entry("load-balancer", 2, "t-lb", _EDGE_TEMPLATE)], colo_zone_design("EDGE")))

        await gen.generate({"TopologyColocationZone": [gen.data]})

        gen._ensure_fabric_pools.assert_not_awaited()
        gen._create_fabric_devices.assert_not_awaited()
        gen.logger.error.assert_not_called()


class TestSubclassesShareTheDesignFlow:
    """The flow is the base class's: each subclass differs only in what it may host."""

    def test_both_subclasses_inherit_the_sizing_and_envelope_helpers(self) -> None:
        for cls in (ColocationZoneGenerator, VirtualColocationGenerator):
            assert cls._slice_pool is ColocationFabricGenerator._slice_pool
            assert cls._within_design is ColocationFabricGenerator._within_design
            assert cls._ensure_fabric_pools is ColocationFabricGenerator._ensure_fabric_pools
