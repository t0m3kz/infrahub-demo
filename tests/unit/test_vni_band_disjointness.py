"""Unit tests asserting the VNI bands in shipped data and generator constants stay disjoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from utils.limits import MAX_ENCODABLE_VNI, SITE_VNI_END, SITE_VNI_START

# Name of the synthetic pool standing in for the per-site L2 pool the generators create.
SITE_POOL_NAME = "<site>-vni-pool"


def _load_objects(path: Path, kind: str) -> list[dict[str, Any]]:
    """Return the ``spec.data`` entries of every document in path matching kind."""
    entries: list[dict[str, Any]] = []
    for doc in yaml.safe_load_all(path.read_text()):
        if not doc:
            continue
        spec = doc.get("spec") or {}
        if spec.get("kind") == kind:
            entries.extend(spec.get("data") or [])
    return entries


@pytest.fixture(scope="module")
def fixed_namespace_l3_vnis(root_dir: Path) -> dict[str, int]:
    """The hand-assigned L3 VNI of each of the 4 fixed global VRF namespaces."""
    namespaces = _load_objects(root_dir / "data" / "bootstrap" / "24_security_zones.yml", "SecurityZone")
    return {ns["name"]: ns["l3_vni"] for ns in namespaces if ns.get("l3_vni") is not None}


@pytest.fixture(scope="module")
def bootstrap_number_pools(root_dir: Path) -> list[dict[str, Any]]:
    """Every CoreNumberPool declared anywhere in data/bootstrap."""
    pools: list[dict[str, Any]] = []
    for path in sorted((root_dir / "data" / "bootstrap").glob("*.yml")):
        pools.extend(_load_objects(path, "CoreNumberPool"))
    return pools


@pytest.fixture(scope="module")
def l2_vni_pools(root_dir: Path) -> list[dict[str, Any]]:
    """The L2 VNI pools in play: GLOBAL-L2VNI from bootstrap plus the per-site pool."""
    pools = _pools_for_attribute(
        _load_objects(root_dir / "data" / "bootstrap" / "18_vni_pools.yml", "CoreNumberPool"), "vni"
    )
    pools.append(
        {
            "name": SITE_POOL_NAME,
            "node_attribute": "vni",
            "start_range": SITE_VNI_START,
            "end_range": SITE_VNI_END,
        }
    )
    return pools


def _pools_for_attribute(pools: list[dict[str, Any]], attribute: str) -> list[dict[str, Any]]:
    """Return the pools whose ``node_attribute`` is attribute."""
    return [p for p in pools if p.get("node_attribute") == attribute]


class TestFixedNamespaceL3Vnis:
    """The four hand-set L3 VNIs are the only L3 VNIs in the data model."""

    def test_all_four_namespaces_have_an_l3_vni(self, fixed_namespace_l3_vnis: dict[str, int]) -> None:
        """PROD, NONPROD, INTERNET and MANAGEMENT each carry an l3_vni."""
        assert set(fixed_namespace_l3_vnis) == {"PROD", "NONPROD", "INTERNET", "MANAGEMENT"}

    def test_they_are_unique(self, fixed_namespace_l3_vnis: dict[str, int]) -> None:
        """Two VRFs sharing an L3 VNI route into each other."""
        values = list(fixed_namespace_l3_vnis.values())
        assert len(set(values)) == len(values), f"duplicate L3 VNI among fixed namespaces: {fixed_namespace_l3_vnis}"

    def test_they_are_encodable(self, fixed_namespace_l3_vnis: dict[str, int]) -> None:
        """Each L3 VNI fits the 16-bit assigned-number field of a type-1 RD / type-2 RT."""
        for name, vni in fixed_namespace_l3_vnis.items():
            assert vni <= MAX_ENCODABLE_VNI, f"{name}: L3 VNI {vni} cannot be encoded in a type-1 RD / type-2 RT"

    def test_no_bootstrap_pool_allocates_l3_vnis(self, bootstrap_number_pools: list[dict[str, Any]]) -> None:
        """No CoreNumberPool targets l3_vni, so nothing can collide with the hand-set values."""
        assert bootstrap_number_pools, "no CoreNumberPool found in data/bootstrap -- fixture is not reading the files"
        l3_pools = _pools_for_attribute(bootstrap_number_pools, "l3_vni")
        assert not l3_pools, f"an l3_vni pool came back: {[p['name'] for p in l3_pools]}"


class TestFixedNamespacesAvoidEveryPool:
    """The fixed namespaces are allocated by hand, deliberately pool-free."""

    def test_no_fixed_namespace_sits_inside_an_l2_vni_pool(
        self, fixed_namespace_l3_vnis: dict[str, int], l2_vni_pools: list[dict[str, Any]]
    ) -> None:
        """The regression: a customer segment drawing this VNI from an L2 pool
        would bridge tenant traffic into the routed VRF using the same VNI."""
        assert len(l2_vni_pools) >= 2, "expected the site pool and GLOBAL-L2VNI"

        for name, vni in fixed_namespace_l3_vnis.items():
            for pool in l2_vni_pools:
                assert not (pool["start_range"] <= vni <= pool["end_range"]), (
                    f"{name} L3 VNI {vni} is inside L2 pool '{pool['name']}' "
                    f"({pool['start_range']}-{pool['end_range']})"
                )


class TestTransitVniBand:
    """Exchange transit VNIs are derived (utils/exchange_transit.py), not pooled."""

    def test_no_pool_or_fixed_namespace_overlaps_the_transit_band(
        self, fixed_namespace_l3_vnis: dict[str, int], l2_vni_pools: list[dict[str, Any]]
    ) -> None:
        """Neither an L2 pool nor a fixed namespace L3 VNI may reach the derived transit VNIs."""
        from utils.exchange_transit import TRANSIT_VNI_MAX, TRANSIT_VNI_MIN

        assert TRANSIT_VNI_MAX <= MAX_ENCODABLE_VNI
        for pool in l2_vni_pools:
            assert pool["end_range"] < TRANSIT_VNI_MIN or pool["start_range"] > TRANSIT_VNI_MAX, (
                f"pool '{pool['name']}' overlaps the transit VNI band {TRANSIT_VNI_MIN}-{TRANSIT_VNI_MAX}"
            )
        for name, vni in fixed_namespace_l3_vnis.items():
            assert not TRANSIT_VNI_MIN <= vni <= TRANSIT_VNI_MAX, f"{name} L3 VNI {vni} is in the transit band"


class TestPoolBandsAreDisjoint:
    """The site L2 pool, the stretched pool and every VNI pool stay apart and encodable."""

    def test_site_l2_pool_never_overlaps_the_stretched_pool(self, l2_vni_pools: list[dict[str, Any]]) -> None:
        """Stretched segments share one VNI from GLOBAL-L2VNI across every site."""
        stretched = [p for p in l2_vni_pools if p["name"] == "GLOBAL-L2VNI"]
        assert len(stretched) == 1, "GLOBAL-L2VNI is missing from data/bootstrap/18_vni_pools.yml"
        band = stretched[0]
        site = [p for p in l2_vni_pools if p is not band]
        assert [p["name"] for p in site] == [SITE_POOL_NAME]

        for pool in site:
            overlaps = pool["start_range"] <= band["end_range"] and band["start_range"] <= pool["end_range"]
            assert not overlaps, (
                f"site L2 pool '{pool['name']}' ({pool['start_range']}-{pool['end_range']}) overlaps "
                f"GLOBAL-L2VNI ({band['start_range']}-{band['end_range']})"
            )

    def test_every_vni_pool_stays_encodable(self, l2_vni_pools: list[dict[str, Any]]) -> None:
        """Every VNI pool is EVPN-carried and so RD/RT-encoded."""
        for pool in l2_vni_pools:
            assert pool["end_range"] <= MAX_ENCODABLE_VNI, (
                f"pool '{pool['name']}' can hand out {pool['end_range']}, which cannot be "
                f"encoded in a type-1 RD / type-2 RT (max {MAX_ENCODABLE_VNI})"
            )
