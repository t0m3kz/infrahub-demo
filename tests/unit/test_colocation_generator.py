"""Unit tests for ColocationMetroGenerator (generators/topology/colocation.py).

The metro owns no devices: its zones and virtual colocations do (see
test_colocation_zone_generator.py). What the metro keeps is what those children
share, the identity of the metro's one EVPN Multi-Site site:

- its L2 VNI pool (``{metro}-vni-pool``, read by the segment generator off
  TopologySegmentHosting);
- the ASN pool (``{metro}-asn-pool``) the site's one AS is drawn from later by a
  zone or virtual colocation.

Both are created and attached on every run, kit or no kit.

A metro WITH a design (TopologyTemplateColocationMetro, data/bootstrap/14c_colocation_templates.yml) is also the
supernet holder of its zones: five prefix pools sized by generators/fabric_sizing.py for max_zones zones of the widest
listed zone design, and an ASN block sized for max_zones. A metro without one creates none of them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.fabric_sizing import (
    colo_metro_asn_units,
    colo_metro_loopback_bits,
    colo_metro_loopback_ipv6_bits,
    colo_metro_management_bits,
    colo_metro_technical_bits,
    colo_metro_technical_ipv6_bits,
    host_bits_to_prefix_length,
)
from generators.helpers.pools import SITE_VNI_END, SITE_VNI_START, name_to_asn_range
from generators.protocols import TopologyColocationMetro
from generators.topology.colocation import _METRO_ASN_SIZING_UNITS, ColocationMetroGenerator
from tests.unit.simulators.fabric_designs import COLO_METRO_DESIGN_NAMES, colo_metro_design


def _make_generator() -> Any:
    """A metro generator whose pool upserts and attach are mocked at the SDK boundary."""
    gen = ColocationMetroGenerator.__new__(ColocationMetroGenerator)
    gen.logger = MagicMock()
    gen.client = MagicMock()
    gen.upsert_asn_pool = AsyncMock(return_value=MagicMock(id="asn-pool-id"))
    gen.upsert_number_pool = AsyncMock(return_value=MagicMock(id="vni-pool-id"))
    gen.attach_refs = AsyncMock()
    gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: MagicMock(id=f"id-{kw['pool_name']}"))
    setattr(gen, "acquire_resource_lock", AsyncMock(return_value="lock-id"))  # noqa: B010
    setattr(gen, "release_resource_lock", AsyncMock())  # noqa: B010
    return gen


def _metro(name: str = "FR", metro_id: str = "metro-1", design: dict[str, Any] | None = None) -> dict[str, Any]:
    metro: dict[str, Any] = {"id": metro_id, "name": name}
    if design is not None:
        metro["design"] = design
    return metro


class TestGenerateEntry:
    """generate(): the cleaned input shape and the empty-response guard."""

    @pytest.mark.asyncio
    async def test_missing_root_key_logs_error(self) -> None:
        """No TopologyColocationMetro in the response is an error and writes nothing."""
        gen = _make_generator()

        await gen.generate({})

        gen.logger.error.assert_called_once()
        gen.upsert_asn_pool.assert_not_awaited()
        gen.upsert_number_pool.assert_not_awaited()
        gen.attach_refs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raw_graphql_response_is_cleaned(self) -> None:
        """generate() runs clean_data() first: without it the root key is an {"edges": [...]} dict."""
        gen = _make_generator()
        raw = {"TopologyColocationMetro": {"edges": [{"node": {"id": "metro-1", "name": {"value": "FR"}}}]}}

        await gen.generate(raw)

        assert gen.fabric_name == "fr"
        assert gen.deployment_id == "metro-1"
        gen.attach_refs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_every_metro_in_the_response_is_generated(self) -> None:
        """Two metros in one response each get their own pools and attach."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro("FR", "m-fr"), _metro("PA", "m-pa")]})

        assert [c.kwargs["pool_name"] for c in gen.upsert_asn_pool.await_args_list] == ["fr-asn-pool", "pa-asn-pool"]
        assert [c.kwargs["node_id"] for c in gen.attach_refs.await_args_list] == ["m-fr", "m-pa"]


class TestMetroPools:
    """The two pools a metro owns, and what is attached to it."""

    @pytest.mark.asyncio
    async def test_asn_pool_is_named_per_metro_and_drawn_from_the_name_hash_grid(self) -> None:
        """The metro ASN pool uses the DC name-hash grid, so a metro cannot collide with a DC."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        kwargs = gen.upsert_asn_pool.await_args_list[-1].kwargs
        start, end = name_to_asn_range(dc_name="fr", max_pods=_METRO_ASN_SIZING_UNITS)
        assert kwargs["pool_name"] == "fr-asn-pool"
        assert (kwargs["start_range"], kwargs["end_range"]) == (start, end)
        assert 4_200_000_000 <= start < end <= 4_294_967_295

    @pytest.mark.asyncio
    async def test_vni_pool_is_a_number_pool_on_the_segment_deployment_vni(self) -> None:
        """The metro hosts local segments, so it needs its own VNI pool on ManagedSegmentDeployment.vni."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        kwargs = gen.upsert_number_pool.await_args_list[-1].kwargs
        assert kwargs["pool_name"] == "fr-vni-pool"
        assert kwargs["node"] == "ManagedSegmentDeployment"
        assert kwargs["node_attribute"] == "vni"
        assert (kwargs["start_range"], kwargs["end_range"]) == (SITE_VNI_START, SITE_VNI_END)

    @pytest.mark.asyncio
    async def test_both_pools_are_attached_to_the_metro_in_one_call(self) -> None:
        """fabric_asn_pool and vni_pool ride one attach_refs on the metro; no other pool is attached."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        gen.attach_refs.assert_awaited_once_with(
            kind=TopologyColocationMetro,
            node_id="metro-1",
            refs={"fabric_asn_pool": "asn-pool-id", "vni_pool": "vni-pool-id"},
        )

    @pytest.mark.asyncio
    async def test_metro_names_are_lower_cased_in_pool_names(self) -> None:
        """Pool names follow the lower-cased metro name, the same space as device names."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro("AM", "m-am")]})

        assert gen.upsert_asn_pool.await_args_list[-1].kwargs["pool_name"] == "am-asn-pool"
        assert gen.upsert_number_pool.await_args_list[-1].kwargs["pool_name"] == "am-vni-pool"


class TestMetroOwnsNoKit:
    """The metro runs with no kit at all and never creates devices, IP pools or locks."""

    @pytest.mark.asyncio
    async def test_runs_with_only_id_and_name_and_creates_no_devices_or_ip_pools(self) -> None:
        """A metro whose zones carry nothing still gets its pools; the client is never written to."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        gen.upsert_asn_pool.assert_awaited_once()
        gen.upsert_number_pool.assert_awaited_once()
        gen.client.create.assert_not_called()
        gen.client.allocate_next_ip_prefix.assert_not_called()
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerun_converges_on_the_same_keys(self) -> None:
        """Idempotent: a second run issues the same pool names and the same attach."""
        gen = _make_generator()

        for _ in range(2):
            await gen.generate({"TopologyColocationMetro": [_metro()]})

        first, second = gen.attach_refs.await_args_list
        assert first == second
        assert {c.kwargs["pool_name"] for c in gen.upsert_asn_pool.await_args_list} == {"fr-asn-pool"}
        assert {c.kwargs["pool_name"] for c in gen.upsert_number_pool.await_args_list} == {"fr-vni-pool"}


# metro design -> (loopback, management, technical) IPv4 and (loopback, technical) IPv6 prefix lengths, computed by
# hand from data/bootstrap/14c_colocation_templates.yml: S holds 2 zones, M holds 4, of the widest zone design
# (EDGE_FW_LB: 8 loopback, 16 management, 16 technical addresses; management holds the devices plus two spare).
_GOLDEN_SUPERNET_LENGTHS = {
    "S": {
        "fr-loopback-pool": 28,
        "fr-management-pool": 27,
        "fr-technical-pool": 27,
        "fr-loopback-ipv6-pool": 119,
        "fr-technical-ipv6-pool": 123,
    },
    "M": {
        "fr-loopback-pool": 27,
        "fr-management-pool": 26,
        "fr-technical-pool": 26,
        "fr-loopback-ipv6-pool": 118,
        "fr-technical-ipv6-pool": 122,
    },
}
_SUPERNET_PARENTS = {
    "fr-loopback-pool": "Loopback-IPv4",
    "fr-management-pool": "Management-IPv4",
    "fr-technical-pool": "Technical-IPv4",
    "fr-loopback-ipv6-pool": "Loopback-IPv6",
    "fr-technical-ipv6-pool": "Technical-IPv6",
}
_SUPERNET_ROLES = {
    "fr-loopback-pool": "loopback",
    "fr-management-pool": "management",
    "fr-technical-pool": "technical",
    "fr-loopback-ipv6-pool": "loopback",
    "fr-technical-ipv6-pool": "technical",
}


def _supernet_calls(gen: Any) -> dict[str, dict[str, Any]]:
    """The ensure_sliced_pool kwargs of a run, keyed by pool name."""
    return {c.kwargs["pool_name"]: c.kwargs for c in gen.ensure_sliced_pool.await_args_list}


class TestMetroWithDesignSupernets:
    """A metro with a design is the supernet holder of its zones, the way a DC is of its pods."""

    def test_golden_table_covers_every_bootstrap_metro_design(self) -> None:
        """A new metro design must add its expected supernet lengths here."""
        assert set(_GOLDEN_SUPERNET_LENGTHS) == set(COLO_METRO_DESIGN_NAMES)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("design_name", COLO_METRO_DESIGN_NAMES)
    async def test_five_prefix_pools_are_sliced_with_the_sizing_functions_lengths(self, design_name: str) -> None:
        """Names, kind, role, bootstrap parent and the length from colo_metro_*_bits (and from the hand-made table)."""
        design = colo_metro_design(design_name)
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro(design=design)]})

        calls = _supernet_calls(gen)
        assert set(calls) == set(_SUPERNET_PARENTS)
        assert {name: call["prefix_length"] for name, call in calls.items()} == _GOLDEN_SUPERNET_LENGTHS[design_name]
        assert {name: call["parent_pool_name"] for name, call in calls.items()} == _SUPERNET_PARENTS
        assert {name: call["role"] for name, call in calls.items()} == _SUPERNET_ROLES
        # A prefix pool for every supernet: the zones slice their own address and prefix pools out of it.
        assert {call["kind"] for call in calls.values()} == {"prefix"}
        assert calls["fr-loopback-pool"]["prefix_length"] == host_bits_to_prefix_length(
            colo_metro_loopback_bits(design), ipv6=False
        )
        assert calls["fr-management-pool"]["prefix_length"] == host_bits_to_prefix_length(
            colo_metro_management_bits(design), ipv6=False
        )
        assert calls["fr-technical-pool"]["prefix_length"] == host_bits_to_prefix_length(
            colo_metro_technical_bits(design), ipv6=False
        )
        assert calls["fr-loopback-ipv6-pool"]["prefix_length"] == host_bits_to_prefix_length(
            colo_metro_loopback_ipv6_bits(design), ipv6=True
        )
        assert calls["fr-technical-ipv6-pool"]["prefix_length"] == host_bits_to_prefix_length(
            colo_metro_technical_ipv6_bits(design), ipv6=True
        )

    @pytest.mark.asyncio
    async def test_supernet_names_follow_the_lower_cased_metro_name(self) -> None:
        """Supernet pools live in the same name space as the metro's devices and pools."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro("AM", "m-am", colo_metro_design("S"))]})

        assert set(_supernet_calls(gen)) == {
            "am-loopback-pool",
            "am-management-pool",
            "am-technical-pool",
            "am-loopback-ipv6-pool",
            "am-technical-ipv6-pool",
        }

    @pytest.mark.asyncio
    async def test_attach_refs_carries_asn_vni_and_the_three_ipv4_supernets_but_not_the_ipv6_ones(self) -> None:
        """The IPv6 supernets have no relationship: the zones find them by name."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        gen.attach_refs.assert_awaited_once_with(
            kind=TopologyColocationMetro,
            node_id="metro-1",
            refs={
                "fabric_asn_pool": "asn-pool-id",
                "vni_pool": "vni-pool-id",
                "loopback_pool": "id-fr-loopback-pool",
                "management_pool": "id-fr-management-pool",
                "technical_pool": "id-fr-technical-pool",
            },
        )

    @pytest.mark.asyncio
    async def test_supernets_are_sliced_under_the_metro_keyed_lock_and_it_is_released(self) -> None:
        """Each supernet allocates a prefix from a shared bootstrap pool: two overlapping runs must not both create it."""
        gen = _make_generator()
        order: list[str] = []
        gen.acquire_resource_lock = AsyncMock(side_effect=lambda key: order.append(f"acquire {key}") or "lock-id")
        gen.release_resource_lock = AsyncMock(side_effect=lambda lock_id: order.append(f"release {lock_id}"))
        gen.ensure_sliced_pool = AsyncMock(side_effect=lambda **kw: order.append(kw["pool_name"]) or MagicMock(id="x"))

        await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        assert order[0] == "acquire colocation-metro-pools-fr"
        assert order[-1] == "release lock-id"
        assert sorted(order[1:-1]) == sorted(_SUPERNET_PARENTS)

    @pytest.mark.asyncio
    async def test_lock_is_released_when_a_supernet_fails(self) -> None:
        """A failing slice must not leave the metro locked for the next run."""
        gen = _make_generator()
        gen.ensure_sliced_pool = AsyncMock(side_effect=RuntimeError("no more resources"))

        with pytest.raises(RuntimeError, match="no more resources"):
            await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        gen.release_resource_lock.assert_awaited_once_with("lock-id")
        gen.attach_refs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_asn_block_is_sized_from_the_design(self) -> None:
        """colo_metro_asn_units(design) replaces the single unit of a metro without a design."""
        design = colo_metro_design("S")
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro(design=design)]})

        kwargs = gen.upsert_asn_pool.await_args_list[-1].kwargs
        start, end = name_to_asn_range(dc_name="fr", max_pods=colo_metro_asn_units(design))
        assert colo_metro_asn_units(design) == design["max_zones"] == 2
        assert (kwargs["start_range"], kwargs["end_range"]) == (start, end)
        assert kwargs["pool_name"] == "fr-asn-pool"

    @pytest.mark.asyncio
    async def test_a_wider_design_gets_a_wider_asn_block_than_no_design(self) -> None:
        """The block follows max_zones: a metro without a design keeps the one-unit block, a big one gets more."""
        big = {**colo_metro_design("S"), "max_zones": 8}
        sizes = {}
        for label, metro in (
            ("none", _metro()),
            ("S", _metro(design=colo_metro_design("S"))),
            ("big", _metro(design=big)),
        ):
            gen = _make_generator()
            await gen.generate({"TopologyColocationMetro": [metro]})
            kwargs = gen.upsert_asn_pool.await_args_list[-1].kwargs
            sizes[label] = kwargs["end_range"] - kwargs["start_range"]

        assert sizes["none"] < sizes["S"] < sizes["big"]
        start, end = name_to_asn_range(dc_name="fr", max_pods=_METRO_ASN_SIZING_UNITS)
        assert sizes["none"] == end - start

    @pytest.mark.asyncio
    async def test_design_arrives_as_the_cleaned_graphql_shape(self) -> None:
        """colocation_metro.gql returns design { node { ... zone_templates { edges { node } } } }; clean_data() flattens it."""
        gen = _make_generator()

        def zone(name: str, edges: int, firewalls: int, load_balancers: int) -> dict[str, Any]:
            return {
                "node": {
                    "id": f"zd-{name}",
                    "name": {"value": name},
                    "max_edges": {"value": edges},
                    "max_firewalls": {"value": firewalls},
                    "max_load_balancers": {"value": load_balancers},
                }
            }

        raw = {
            "TopologyColocationMetro": {
                "edges": [
                    {
                        "node": {
                            "id": "metro-1",
                            "name": {"value": "FR"},
                            "design": {
                                "node": {
                                    "id": "md-S",
                                    "name": {"value": "S"},
                                    "max_zones": {"value": 2},
                                    "zone_templates": {
                                        "edges": [
                                            zone("EDGE", 2, 0, 0),
                                            zone("EDGE_FW", 2, 2, 0),
                                            zone("EDGE_FW_LB", 2, 2, 2),
                                        ]
                                    },
                                }
                            },
                        }
                    }
                ]
            }
        }

        await gen.generate(raw)

        lengths = {name: call["prefix_length"] for name, call in _supernet_calls(gen).items()}
        assert lengths == _GOLDEN_SUPERNET_LENGTHS["S"]
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerun_converges_on_the_same_supernets(self) -> None:
        """Idempotent: the same names and sizes are asked for again, so the name-keyed slices converge."""
        gen = _make_generator()

        for _ in range(2):
            await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("M"))]})

        names = [c.kwargs["pool_name"] for c in gen.ensure_sliced_pool.await_args_list]
        assert len(names) == 10
        assert names[:5] == names[5:]
        first, second = gen.attach_refs.await_args_list
        assert first == second


class TestMetroWithoutDesign:
    """A metro with no design declares no envelope: no supernets, its zones slice the bootstrap pools."""

    @pytest.mark.asyncio
    async def test_creates_no_supernets_takes_no_lock_and_attaches_only_asn_and_vni(self) -> None:
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        gen.ensure_sliced_pool.assert_not_awaited()
        gen.acquire_resource_lock.assert_not_awaited()
        gen.attach_refs.assert_awaited_once_with(
            kind=TopologyColocationMetro,
            node_id="metro-1",
            refs={"fabric_asn_pool": "asn-pool-id", "vni_pool": "vni-pool-id"},
        )

    @pytest.mark.asyncio
    async def test_design_none_is_the_same_as_no_design(self) -> None:
        """The query returns design: null for a metro without one."""
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [{**_metro(), "design": None}]})

        gen.ensure_sliced_pool.assert_not_awaited()
        gen.logger.error.assert_not_called()


class TestMetroDesignWithoutZoneTemplates:
    """A design that lists no zone designs gives nothing to size the supernets from."""

    @pytest.mark.asyncio
    async def test_logs_an_error_and_saves_nothing(self) -> None:
        """Every size is derived before the first save: a partial tracked set would be cleaned up as stale."""
        design = {**colo_metro_design("S"), "zone_templates": []}
        gen = _make_generator()

        await gen.generate({"TopologyColocationMetro": [_metro(design=design)]})

        gen.logger.error.assert_called_once()
        message = gen.logger.error.call_args.args[0]
        assert "Metro fr" in message
        assert "zone_templates" in message
        gen.upsert_asn_pool.assert_not_awaited()
        gen.upsert_number_pool.assert_not_awaited()
        gen.ensure_sliced_pool.assert_not_awaited()
        gen.attach_refs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_other_metros_of_the_response_are_still_generated(self) -> None:
        """One undersized design stops that metro only."""
        broken = {**colo_metro_design("S"), "zone_templates": []}
        gen = _make_generator()

        await gen.generate(
            {"TopologyColocationMetro": [_metro("FR", "m-fr", broken), _metro("PA", "m-pa", colo_metro_design("S"))]}
        )

        assert [c.kwargs["node_id"] for c in gen.attach_refs.await_args_list] == ["m-pa"]
        gen.logger.error.assert_called_once()
