"""ColocationMetroGenerator (generators/colocation/colocation.py): the plan it writes, applied on the in-memory client: the
VNI and ASN pools of a metro and, with a design, the address supernets its zones slice, and what a re-run repeats.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.colocation.colocation import _METRO_ASN_SIZING_UNITS, ColocationMetroGenerator
from generators.ipam.pools import name_to_asn_range
from generators.model.errors import GeneratorError
from generators.model.sizing import (
    colo_metro_asn_units,
    colo_metro_loopback_bits,
    colo_metro_loopback_ipv6_bits,
    colo_metro_management_bits,
    colo_metro_technical_bits,
    colo_metro_technical_ipv6_bits,
    host_bits_to_prefix_length,
)
from tests.unit.plan_graph_helpers import HfidClient, HfidNode, make_hfid_generator
from tests.unit.simulators.fabric_designs import COLO_METRO_DESIGN_NAMES, colo_metro_design
from utils.limits import SITE_VNI_END, SITE_VNI_START

_PARENTS = ("Management-IPv4", "Technical-IPv4", "Technical-IPv6", "Loopback-IPv4", "Loopback-IPv6")


def _metro(name: str = "FR", metro_id: str = "metro-1", design: dict[str, Any] | None = None) -> dict[str, Any]:
    metro: dict[str, Any] = {"id": metro_id, "name": name}
    if design is not None:
        metro["design"] = design
    return metro


def _run(*metros: dict[str, Any], client: HfidClient | None = None) -> tuple[Any, HfidClient]:
    """A metro generator on the in-memory client (the bootstrap pools exist, the metros are there to point at) and the
    metros it has to generate; returns it with the client, not yet run.
    """
    client = client or HfidClient()
    if not any(node.kind == "CoreIPPrefixPool" for node in client.store.values()):
        for parent in _PARENTS:
            client.seed("CoreIPPrefixPool", name=parent)
    for metro in metros:
        if metro["id"] not in client.store:
            client.store[metro["id"]] = HfidNode(
                client, "TopologyColocationMetro", {"id": metro["id"], "name": metro["name"]}
            )
    return make_hfid_generator(client, cls=ColocationMetroGenerator), client


def _pools(client: HfidClient, kind: str) -> dict[str, dict[str, Any]]:
    """The stored pools of a kind by name."""
    return {node.data["name"]: node.data for node in client.store.values() if node.kind == kind}


def _metro_node(client: HfidClient, metro_id: str = "metro-1") -> dict[str, Any]:
    return client.store[metro_id].data


def _snapshot(client: HfidClient) -> dict[str, dict[str, Any]]:
    return {node_id: {k: v for k, v in node.data.items() if k != "id"} for node_id, node in client.store.items()}


# metro design -> (loopback, management, technical) IPv4 and (loopback, technical) IPv6 prefix lengths, computed by hand
# from data/bootstrap/14c_colocation_templates.yml: S holds 2 zones, M holds 4, of the widest zone design.
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


def _supernets(client: HfidClient, metro: str = "fr") -> dict[str, int]:
    """The prefix length of the supernet each of the metro's pools was cut with, by pool name."""
    pools = {name: data for name, data in _pools(client, "CoreIPPrefixPool").items() if name.startswith(f"{metro}-")}
    return {name: data["default_prefix_length"] for name, data in pools.items()}


def _slices(client: HfidClient) -> dict[str, tuple[str, str]]:
    """The slice each supernet holds: pool name -> (the parent prefix it was drawn from's length, role)."""
    return {
        node.data["identifier"]: (str(node.prefix.value).rsplit("/", 1)[-1], node.data["role"])
        for node in client.store.values()
        if node.kind == "IpamPrefix"
    }


@pytest.mark.asyncio
class TestGenerateEntry:
    """generate(): the cleaned input shape and the empty-response guard."""

    async def test_missing_root_key_logs_error_and_writes_nothing(self) -> None:
        gen, client = _run()

        await gen.generate({})

        gen.logger.error.assert_called_once()
        assert client.saves == []

    async def test_raw_graphql_response_is_cleaned(self) -> None:
        """generate() runs clean_data() first: without it the root key is an {"edges": [...]} dict."""
        gen, client = _run(_metro())
        raw = {"TopologyColocationMetro": {"edges": [{"node": {"id": "metro-1", "name": {"value": "FR"}}}]}}

        await gen.generate(raw)

        assert (gen.fabric_name, gen.deployment_id) == ("fr", "metro-1")
        assert set(_pools(client, "CoreNumberPool")) == {"fr-asn-pool", "fr-vni-pool"}

    async def test_every_metro_in_the_response_is_generated_and_pointed_at_its_own_pools(self) -> None:
        gen, client = _run(_metro("FR", "m-fr"), _metro("PA", "m-pa"))

        await gen.generate({"TopologyColocationMetro": [_metro("FR", "m-fr"), _metro("PA", "m-pa")]})

        assert set(_pools(client, "CoreNumberPool")) == {"fr-asn-pool", "fr-vni-pool", "pa-asn-pool", "pa-vni-pool"}
        assert client.store["m-fr"].data["fabric_asn_pool"] != client.store["m-pa"].data["fabric_asn_pool"]


@pytest.mark.asyncio
class TestMetroPools:
    """The two pools a metro owns, and what is attached to it."""

    async def test_asn_pool_is_named_per_metro_and_drawn_from_the_name_hash_grid(self) -> None:
        """The metro ASN pool uses the DC name-hash grid, so a metro cannot collide with a DC."""
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        pool = _pools(client, "CoreNumberPool")["fr-asn-pool"]
        start, end = name_to_asn_range(dc_name="fr", max_pods=_METRO_ASN_SIZING_UNITS)
        assert (pool["start_range"], pool["end_range"]) == (start, end)
        assert (pool["node"], pool["node_attribute"]) == ("RoutingAutonomousSystem", "asn")
        assert 4_200_000_000 <= start < end <= 4_294_967_295

    async def test_vni_pool_is_a_number_pool_on_the_segment_deployment_vni(self) -> None:
        """The metro hosts local segments, so it needs its own VNI pool on ManagedSegmentDeployment.vni."""
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        pool = _pools(client, "CoreNumberPool")["fr-vni-pool"]
        assert (pool["node"], pool["node_attribute"]) == ("ManagedSegmentDeployment", "vni")
        assert (pool["start_range"], pool["end_range"]) == (SITE_VNI_START, SITE_VNI_END)

    async def test_the_metro_points_at_its_two_pools_in_one_untracked_save_and_the_run_tracks_the_metro(self) -> None:
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro()]})

        assert set(_metro_node(client)) == {"id", "name", "fabric_asn_pool", "vni_pool"}
        (kwargs,) = [kwargs for _, node_id, _, kwargs in client.saves if node_id == "metro-1"]
        assert kwargs == {"allow_upsert": True, "update_group_context": False}
        assert client.related_at_first_write == ["metro-1"]

    async def test_a_metro_without_a_design_owns_no_supernet_and_takes_no_lock(self) -> None:
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [{**_metro(), "design": None}]})

        assert _pools(client, "CoreIPPrefixPool").keys() == set(_PARENTS)
        assert not [node for node in client.store.values() if node.kind == "IpamPrefix"]
        assert gen.locked_keys == []
        assert set(_metro_node(client)) == {"id", "name", "fabric_asn_pool", "vni_pool"}
        gen.logger.error.assert_not_called()


def test_golden_table_covers_every_bootstrap_metro_design() -> None:
    """A new metro design must add its expected supernet lengths here."""
    assert set(_GOLDEN_SUPERNET_LENGTHS) == set(COLO_METRO_DESIGN_NAMES)


@pytest.mark.asyncio
class TestMetroWithDesignSupernets:
    """A metro with a design is the supernet holder of its zones, the way a DC is of its pods."""

    @pytest.mark.parametrize("design_name", COLO_METRO_DESIGN_NAMES)
    async def test_five_prefix_pools_are_sliced_with_the_sizing_functions_lengths(self, design_name: str) -> None:
        """Names, role and the length from colo_metro_*_bits (and from the hand-made table)."""
        design = colo_metro_design(design_name)
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro(design=design)]})

        lengths = _supernets(client)
        assert lengths == _GOLDEN_SUPERNET_LENGTHS[design_name]
        assert lengths["fr-loopback-pool"] == host_bits_to_prefix_length(colo_metro_loopback_bits(design), ipv6=False)
        assert lengths["fr-management-pool"] == host_bits_to_prefix_length(
            colo_metro_management_bits(design), ipv6=False
        )
        assert lengths["fr-technical-pool"] == host_bits_to_prefix_length(colo_metro_technical_bits(design), ipv6=False)
        assert lengths["fr-loopback-ipv6-pool"] == host_bits_to_prefix_length(
            colo_metro_loopback_ipv6_bits(design), ipv6=True
        )
        assert lengths["fr-technical-ipv6-pool"] == host_bits_to_prefix_length(
            colo_metro_technical_ipv6_bits(design), ipv6=True
        )
        slices = _slices(client)
        assert {name: role for name, (_, role) in slices.items()} == _SUPERNET_ROLES
        assert {name: int(length) for name, (length, _) in slices.items()} == _GOLDEN_SUPERNET_LENGTHS[design_name]
        # Every supernet is a prefix pool holding its slice: the zones cut their own address and prefix pools out of it
        assert all(node.data["default_prefix_type"] == "IpamPrefix" for node in _supernet_nodes(client))

    async def test_each_supernet_is_cut_from_its_bootstrap_pool(self) -> None:
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        parents = {identifier: pool_id for pool_id, identifier in client.allocations}
        assert {name: client.store[pool_id].data["name"] for name, pool_id in parents.items()} == _SUPERNET_PARENTS

    async def test_supernet_names_follow_the_lower_cased_metro_name(self) -> None:
        gen, client = _run(_metro("AM", "m-am"))

        await gen.generate({"TopologyColocationMetro": [_metro("AM", "m-am", colo_metro_design("S"))]})

        assert set(_supernets(client, "am")) == {
            "am-loopback-pool",
            "am-management-pool",
            "am-technical-pool",
            "am-loopback-ipv6-pool",
            "am-technical-ipv6-pool",
        }

    async def test_the_metro_points_at_the_three_ipv4_supernets_but_not_the_ipv6_ones(self) -> None:
        """The IPv6 supernets have no relationship: the zones find them by name."""
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        assert set(_metro_node(client)) == {
            "id",
            "name",
            "fabric_asn_pool",
            "vni_pool",
            "loopback_pool",
            "management_pool",
            "technical_pool",
        }
        by_id = {node.id: node.data["name"] for node in client.store.values() if "name" in node.data}
        assert {key: by_id[_metro_node(client)[key]] for key in ("loopback_pool", "management_pool")} == {
            "loopback_pool": "fr-loopback-pool",
            "management_pool": "fr-management-pool",
        }

    async def test_the_supernets_are_cut_under_the_metro_keyed_lock(self) -> None:
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        assert gen.locked_keys == ["colocation-metro-pools-fr"]

    async def test_asn_block_is_sized_from_the_design(self) -> None:
        """colo_metro_asn_units(design) replaces the single unit of a metro without a design."""
        design = colo_metro_design("S")
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro(design=design)]})

        pool = _pools(client, "CoreNumberPool")["fr-asn-pool"]
        start, end = name_to_asn_range(dc_name="fr", max_pods=colo_metro_asn_units(design))
        assert colo_metro_asn_units(design) == design["max_zones"] == 2
        assert (pool["start_range"], pool["end_range"]) == (start, end)

    async def test_a_wider_design_gets_a_wider_asn_block_than_no_design(self) -> None:
        """The block follows max_zones: a metro without a design keeps the one-unit block, a big one gets more."""
        big = {**colo_metro_design("S"), "max_zones": 8}
        sizes = {}
        for label, metro in (
            ("none", _metro()),
            ("S", _metro(design=colo_metro_design("S"))),
            ("big", _metro(design=big)),
        ):
            gen, client = _run(_metro())
            await gen.generate({"TopologyColocationMetro": [metro]})
            pool = _pools(client, "CoreNumberPool")["fr-asn-pool"]
            sizes[label] = pool["end_range"] - pool["start_range"]

        assert sizes["none"] < sizes["S"] < sizes["big"]
        start, end = name_to_asn_range(dc_name="fr", max_pods=_METRO_ASN_SIZING_UNITS)
        assert sizes["none"] == end - start

    async def test_design_arrives_as_the_cleaned_graphql_shape(self) -> None:
        """colocation_metro.gql returns design { node { ... zone_templates { edges { node } } } }."""
        gen, client = _run(_metro())

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

        assert _supernets(client) == _GOLDEN_SUPERNET_LENGTHS["S"]
        gen.logger.error.assert_not_called()


@pytest.mark.asyncio
class TestRerun:
    async def test_a_rerun_writes_the_same_objects_and_holds_the_same_slices(self) -> None:
        """Idempotent: the same names and sizes are asked for again, so the name-keyed slices converge."""
        metros = {"TopologyColocationMetro": [_metro(design=colo_metro_design("M"))]}
        gen, client = _run(_metro())
        await gen.generate(metros)
        snapshot = _snapshot(client)

        again, _ = _run(_metro(), client=client)
        await again.generate(metros)

        assert _snapshot(client) == snapshot
        assert len(_slices(client)) == 5

    async def test_a_metro_without_a_design_converges_too(self) -> None:
        gen, client = _run(_metro())
        await gen.generate({"TopologyColocationMetro": [_metro()]})
        snapshot = _snapshot(client)

        again, _ = _run(_metro(), client=client)
        await again.generate({"TopologyColocationMetro": [_metro()]})

        assert _snapshot(client) == snapshot


@pytest.mark.asyncio
class TestMetroDesignWithoutZoneTemplates:
    """A design that lists no zone designs gives nothing to size the supernets from."""

    async def test_logs_an_error_and_writes_nothing(self) -> None:
        """Every size is derived before the first write: a partial tracked set would be cleaned up as stale."""
        design = {**colo_metro_design("S"), "zone_templates": []}
        gen, client = _run(_metro())

        await gen.generate({"TopologyColocationMetro": [_metro(design=design)]})

        gen.logger.error.assert_called_once()
        message = gen.logger.error.call_args.args[0]
        assert "Metro fr" in message and "zone_templates" in message
        assert client.saves == []

    async def test_the_other_metros_of_the_response_are_still_generated(self) -> None:
        """One undersized design stops that metro only."""
        broken = {**colo_metro_design("S"), "zone_templates": []}
        gen, client = _run(_metro("FR", "m-fr"), _metro("PA", "m-pa"))

        await gen.generate(
            {"TopologyColocationMetro": [_metro("FR", "m-fr", broken), _metro("PA", "m-pa", colo_metro_design("S"))]}
        )

        assert "fabric_asn_pool" in client.store["m-pa"].data and "fabric_asn_pool" not in client.store["m-fr"].data
        gen.logger.error.assert_called_once()


@pytest.mark.asyncio
class TestMissingBootstrapPool:
    async def test_a_bootstrap_pool_nobody_loaded_fails_before_any_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from unittest.mock import AsyncMock

        monkeypatch.setattr("generators.reconciler.asyncio.sleep", AsyncMock())
        client = HfidClient()
        client.seed("CoreIPPrefixPool", name="Management-IPv4")
        gen, _ = _run(_metro(), client=client)

        with pytest.raises(GeneratorError, match="Not found after"):
            await gen.generate({"TopologyColocationMetro": [_metro(design=colo_metro_design("S"))]})

        assert client.saves == []


def _supernet_nodes(client: HfidClient) -> list[Any]:
    return [
        node
        for node in client.store.values()
        if node.kind == "CoreIPPrefixPool" and node.data["name"].startswith("fr-")
    ]
