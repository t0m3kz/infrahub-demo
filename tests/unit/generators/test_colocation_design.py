"""Design-driven pool sizing of the zone / virtual colocation generators (generators/colocation/colocation_fabric.py): the
slices their plan cuts, from where, and what stays unwritten when a supernet is missing. The envelope the design sets on the
entries is in test_colocation_envelope.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from generators.colocation.colocation_fabric import ColocationZoneGenerator, VirtualColocationGenerator
from generators.colocation.colocation_pools import (
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
from generators.model.errors import GeneratorError
from generators.model.sizing import (
    COLO_ZONE_LOOPBACK_IPV6_BITS,
    COLO_ZONE_TECHNICAL_IPV6_BITS,
    colo_zone_loopback_bits,
    colo_zone_management_bits,
    colo_zone_technical_bits,
    host_bits_to_prefix_length,
)
from tests.unit.colocation_zone_generator_helpers import (
    _EDGE_TEMPLATE,
    _VIRTUAL_EDGE_TEMPLATE,
    _virtual,
    _zone,
    seed_nodes,
    zone_env,
)
from tests.unit.simulators.fabric_designs import (
    COLO_METRO_DESIGN_NAMES,
    COLO_ZONE_DESIGN_NAMES,
    colo_metro_design,
    colo_zone_design,
)

# zone design -> (loopback, management, technical) IPv4 prefix lengths, by hand from the design rows: EDGE holds 2
# devices and 1 link, EDGE_FW 4 and 3, EDGE_FW_LB 6 and 5 (25% headroom, see test_colocation_sizing.py).
_GOLDEN_ZONE_LENGTHS = {
    "EDGE": (29, 29, 30),
    "EDGE_FW": (29, 29, 29),
    "EDGE_FW_LB": (29, 28, 28),
}
# The IPv6 slices are fixed by what they hold (one /128 VTEP per edge, one /127 interlink), not by the design.
_VTEP_IPV6_LENGTH = 120
_INTERLINK_IPV6_LENGTH = 124

_NODES = pytest.mark.parametrize(
    ("cls", "root", "payload", "metro", "template"),
    [
        (ColocationZoneGenerator, "TopologyColocationZone", _zone, "fr", _EDGE_TEMPLATE),
        (VirtualColocationGenerator, "TopologyVirtualColocation", _virtual, "am", _VIRTUAL_EDGE_TEMPLATE),
    ],
    ids=["zone", "virtual-colocation"],
)
# (metro has a design, node has a design): the supernets are used only when both do.
_COMBINATIONS = pytest.mark.parametrize(
    ("metro_has_design", "node_has_design", "from_metro"),
    [(False, False, False), (True, False, False), (False, True, False), (True, True, True)],
    ids=["neither", "metro-only", "zone-only", "both"],
)


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    cls: type[Any],
    root: str,
    payload: Any,
    metro: str,
    template: dict[str, Any],
    *,
    design: str | None = None,
    metro_design: str | None = None,
    edges: int = 2,
    supernets: bool = True,
) -> tuple[Any, Any]:
    """A node with ``edges`` edges of ``template`` (and the designs, when given) run on the in-memory client; the metro's
    supernets exist unless ``supernets`` is off. Returns the generator and the client.
    """
    monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
    node = payload(fabric_templates=[{"role": "edge", "quantity": edges, "template": template}])
    if design:
        node["design"] = colo_zone_design(design)
    if metro_design:
        node["parent"]["design"] = colo_metro_design(metro_design)
    gen, client = zone_env(
        monkeypatch,
        cls=cls,
        metro_pools=tuple(slot.name(metro) for slot in COLO_POOL_SLOTS) if supernets else (),
    )
    seed_nodes(client, node)
    await gen.generate({root: [node]})
    return gen, client


def _pool(client: Any, name: str) -> dict[str, Any]:
    (pool,) = [node.data for node in client.store.values() if node.data.get("name") == name]
    return pool


def _lengths(client: Any, name: str) -> tuple[int, int, int]:
    return tuple(
        _pool(client, slot.name(name))["default_prefix_length"]
        for slot in (LOOPBACK_SLOT, MANAGEMENT_SLOT, TECHNICAL_SLOT)
    )


def _parents(client: Any) -> dict[str, str]:
    """The pool each slice was cut from, by slice name."""
    return {identifier: client.store[pool_id].data["name"] for pool_id, identifier in client.allocations}


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


@pytest.mark.asyncio
class TestZoneDesignSizing:
    """With a design the slices are sized by it (ColoPoolSlot.zone_length), whatever the metro has."""

    @_NODES
    @pytest.mark.parametrize("design_name", COLO_ZONE_DESIGN_NAMES)
    async def test_loopback_management_and_technical_lengths_follow_the_design(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        root: str,
        payload: Any,
        metro: str,
        template: Any,
        design_name: str,
    ) -> None:
        """The three IPv4 pools, against the hand-made table and the sizing functions."""
        gen, client = await _run(monkeypatch, cls, root, payload, metro, template, design=design_name, edges=1)

        design = colo_zone_design(design_name)
        assert _lengths(client, gen.fabric_name) == _GOLDEN_ZONE_LENGTHS[design_name]
        assert _lengths(client, gen.fabric_name) == (
            host_bits_to_prefix_length(colo_zone_loopback_bits(design), ipv6=False),
            host_bits_to_prefix_length(colo_zone_management_bits(design), ipv6=False),
            host_bits_to_prefix_length(colo_zone_technical_bits(design), ipv6=False),
        )

    @_NODES
    async def test_pool_find_or_create_runs_under_a_lock_keyed_by_the_node_name(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        """Two overlapping runs of one zone would otherwise create twin pools handing out the same addresses."""
        gen, _ = await _run(monkeypatch, cls, root, payload, metro, template, design="EDGE_FW", edges=1)

        assert f"colocation-pools-{gen.fabric_name}" in gen.locked_keys

    @_NODES
    @pytest.mark.parametrize("design_name", [None, *COLO_ZONE_DESIGN_NAMES])
    async def test_vtep_slice_is_a_slash_120_with_or_without_a_design(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        root: str,
        payload: Any,
        metro: str,
        template: Any,
        design_name: str | None,
    ) -> None:
        """The VTEP loopback pool comes from COLO_ZONE_LOOPBACK_IPV6_BITS, an address pool in Loopback-IPv6."""
        gen, client = await _run(monkeypatch, cls, root, payload, metro, template, design=design_name, edges=1)

        name = LOOPBACK_IPV6_SLOT.name(gen.fabric_name)
        assert _pool(client, name)["default_prefix_length"] == _VTEP_IPV6_LENGTH
        assert _pool(client, name)["default_address_type"] == "IpamIPAddress"
        assert _parents(client)[name] == "Loopback-IPv6"

    @_NODES
    @pytest.mark.parametrize("design_name", [None, *COLO_ZONE_DESIGN_NAMES])
    async def test_interlink_slice_is_a_slash_124_with_or_without_a_design(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        root: str,
        payload: Any,
        metro: str,
        template: Any,
        design_name: str | None,
    ) -> None:
        """The edge pair's interlink pool comes from COLO_ZONE_TECHNICAL_IPV6_BITS, a prefix pool in Technical-IPv6;
        a node without an edge pair (a virtual colocation, one edge) cuts none.
        """
        gen, client = await _run(monkeypatch, cls, root, payload, metro, template, design=design_name)

        name = TECHNICAL_IPV6_SLOT.name(gen.fabric_name)
        if cls is VirtualColocationGenerator:
            assert name not in _parents(client)
            return
        assert _pool(client, name)["default_prefix_length"] == _INTERLINK_IPV6_LENGTH
        assert _pool(client, name)["default_prefix_type"] == "IpamPrefix"
        assert _parents(client)[name] == "Technical-IPv6"

    @_NODES
    async def test_no_design_uses_the_fixed_fallback_lengths(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        gen, client = await _run(monkeypatch, cls, root, payload, metro, template, edges=1)

        assert _lengths(client, gen.fabric_name) == (28, 28, 26)

    @_NODES
    async def test_the_three_ipv4_pools_are_attached_to_the_node_with_the_design_too(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        gen, client = await _run(
            monkeypatch, cls, root, payload, metro, template, design="EDGE_FW", metro_design="S", edges=1
        )

        node = client.store[gen.deployment_id].data
        names = {node_id: n.data["name"] for node_id, n in client.store.items() if "name" in n.data}
        assert {key: names[node[key]] for key in ("loopback_pool", "management_pool", "technical_pool")} == {
            "loopback_pool": f"{gen.fabric_name}-loopback-pool",
            "management_pool": f"{gen.fabric_name}-management-pool",
            "technical_pool": f"{gen.fabric_name}-technical-pool",
        }
        (kwargs,) = [kwargs for _, node_id, _, kwargs in client.saves if node_id == gen.deployment_id]
        assert kwargs == {"allow_upsert": True, "update_group_context": False}


@pytest.mark.asyncio
class TestSliceParent:
    """The slice is cut from the metro's supernet ONLY when both the metro and the node have a design."""

    @_NODES
    @_COMBINATIONS
    async def test_parent_pool_of_every_slot(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        root: str,
        payload: Any,
        metro: str,
        template: Any,
        metro_has_design: bool,
        node_has_design: bool,
        from_metro: bool,
    ) -> None:
        """All five slices of the four combinations (the node has two physical edges): `{metro}-{suffix}` or the
        bootstrap pool.
        """
        gen, client = await _run(
            monkeypatch,
            cls,
            root,
            payload,
            metro,
            template,
            design="EDGE_FW" if node_has_design else None,
            metro_design="S" if metro_has_design else None,
        )

        assert gen._slices_from_metro is from_metro
        parents = _parents(client)
        slots = [slot for slot in COLO_POOL_SLOTS if slot is not TECHNICAL_IPV6_SLOT or cls is ColocationZoneGenerator]
        for slot in slots:
            expected = slot.name(metro) if from_metro else slot.bootstrap_pool
            assert parents[slot.name(gen.fabric_name)] == expected

    @_NODES
    @_COMBINATIONS
    async def test_the_length_follows_the_node_design_whatever_the_parent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        root: str,
        payload: Any,
        metro: str,
        template: Any,
        metro_has_design: bool,
        node_has_design: bool,
        from_metro: bool,
    ) -> None:
        """The metro design never changes the zone's size: the zone design, else the fallback."""
        gen, client = await _run(
            monkeypatch,
            cls,
            root,
            payload,
            metro,
            template,
            design="EDGE_FW_LB" if node_has_design else None,
            metro_design="M" if metro_has_design else None,
            edges=1,
        )

        assert _lengths(client, gen.fabric_name) == ((29, 28, 28) if node_has_design else (28, 28, 26))

    @_NODES
    async def test_supernet_names_are_the_metros_not_the_nodes(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        """The metro name is lower-cased from the parent, even when the node name is another one (AM-NE under AM)."""
        _, client = await _run(
            monkeypatch, cls, root, payload, metro, template, design="EDGE", metro_design="S", edges=1
        )

        assert set(_parents(client).values()) == {
            f"{metro}-loopback-pool",
            f"{metro}-management-pool",
            f"{metro}-technical-pool",
            f"{metro}-loopback-ipv6-pool",
        }


@pytest.mark.asyncio
class TestWaitForTheMetroGenerator:
    """The metro's supernets are written by add_colocation_metro, whose created-trigger can still be running: the plan
    names it as the producer of what it slices, and only when it slices from the metro.
    """

    @_NODES
    @_COMBINATIONS
    async def test_the_plan_names_the_metro_generator_only_when_it_slices_from_the_metro(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cls: Any,
        root: str,
        payload: Any,
        metro: str,
        template: Any,
        metro_has_design: bool,
        node_has_design: bool,
        from_metro: bool,
    ) -> None:
        plans: list[Any] = []

        class Stop(Exception):
            pass

        node = payload(fabric_templates=[{"role": "edge", "quantity": 1, "template": template}])
        if node_has_design:
            node["design"] = colo_zone_design("EDGE_FW")
        if metro_has_design:
            node["parent"]["design"] = colo_metro_design("S")
        gen, client = zone_env(monkeypatch, cls=cls)
        seed_nodes(client, node)

        async def capture(plan: Any) -> None:
            plans.append(plan)
            raise Stop

        gen.apply_plan = capture
        with pytest.raises(Stop):
            await gen.generate({root: [node]})

        producers = [(p.generators, p.parent_id) for p in plans[0].producers]
        assert producers == ([(("add_colocation_metro",), node["parent"]["id"])] if from_metro else [])


@pytest.mark.asyncio
class TestMissingSupernet:
    """A supernet (or bootstrap pool) that does not exist fails the run before it writes anything: slicing would
    otherwise be silent nonsense.
    """

    @_NODES
    async def test_missing_metro_supernet_fails_the_run_naming_the_pool_and_the_metro_generator(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        with pytest.raises(GeneratorError, match=rf"add_colocation_metro.*{metro}-loopback-pool") as raised:
            await _run(
                monkeypatch, cls, root, payload, metro, template, design="EDGE", metro_design="S", supernets=False
            )

        assert f"{metro}-technical-pool" in str(raised.value)

    @_NODES
    async def test_missing_metro_supernet_writes_nothing_and_waits_for_the_metro_generator(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        node = payload(fabric_templates=[{"role": "edge", "quantity": 1, "template": template}])
        node["design"], node["parent"]["design"] = colo_zone_design("EDGE"), colo_metro_design("S")
        gen, client = zone_env(monkeypatch, cls=cls)
        seed_nodes(client, node)

        with pytest.raises(GeneratorError):
            await gen.generate({root: [node]})

        assert client.saves == []
        gen.wait_for_parent_generators.assert_any_await(("add_colocation_metro",), node["parent"]["id"])

    @_NODES
    async def test_missing_bootstrap_pool_fails_the_same_way(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        node = payload(fabric_templates=[{"role": "edge", "quantity": 1, "template": template}])
        gen, client = zone_env(monkeypatch, cls=cls)
        seed_nodes(client, node)
        client.store = {i: n for i, n in client.store.items() if n.data.get("name") != "Loopback-IPv4"}

        with pytest.raises(GeneratorError, match="Loopback-IPv4"):
            await gen.generate({root: [node]})

        assert client.saves == []


@pytest.mark.asyncio
class TestDesignWarnings:
    """Findings against the envelope are warnings (an error would fail the task), once per finding."""

    @_NODES
    async def test_kit_without_a_design_warns_once_about_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        gen, client = await _run(monkeypatch, cls, root, payload, metro, template, edges=1)

        warnings = [c.args[0] for c in gen.logger.warning.call_args_list]
        assert len(warnings) == 1
        assert "no design" in warnings[0]
        assert f"/{_COLO_LOOPBACK_PREFIX_LENGTH}" in warnings[0]
        assert f"/{_COLO_TECHNICAL_PREFIX_LENGTH}" in warnings[0]
        gen.logger.error.assert_not_called()
        # The run still goes on, with the fallback sizes.
        assert _lengths(client, gen.fabric_name) == (28, 28, 26)

    @_NODES
    async def test_kit_without_a_design_under_a_metro_with_one_still_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        """The metro's envelope does not size a zone that has none: bootstrap pools, fixed lengths, one warning."""
        gen, client = await _run(monkeypatch, cls, root, payload, metro, template, metro_design="S", edges=1)

        assert len([c for c in gen.logger.warning.call_args_list if "no design" in c.args[0]]) == 1
        assert _lengths(client, gen.fabric_name) == (28, 28, 26)
        assert set(_parents(client).values()) == {"Loopback-IPv4", "Management-IPv4", "Technical-IPv4", "Loopback-IPv6"}

    @_NODES
    async def test_a_design_that_holds_the_kit_warns_about_nothing(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        gen, _ = await _run(monkeypatch, cls, root, payload, metro, template, design="EDGE", metro_design="S")

        gen.logger.warning.assert_not_called()
        gen.logger.error.assert_not_called()

    @_NODES
    async def test_no_kit_means_no_design_warning_and_nothing_written(
        self, monkeypatch: pytest.MonkeyPatch, cls: Any, root: str, payload: Any, metro: str, template: Any
    ) -> None:
        """A zone with no kit is legal and common: nothing to size, nothing to warn about, nothing written."""
        gen, client = zone_env(monkeypatch, cls=cls)
        node = payload(fabric_templates=[])

        await gen.generate({root: [node]})

        gen.logger.warning.assert_not_called()
        assert client.saves == []
