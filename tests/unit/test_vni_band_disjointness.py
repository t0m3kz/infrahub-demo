"""Unit tests asserting the VNI bands in shipped data stay disjoint.

A VNI is a single flat 24-bit field on the wire with no L2/L3 discriminator, so
an L2 VNI and an L3 VNI that collide put bridged tenant traffic and a routed VRF
into the same segment. Nothing in the rendered config looks wrong when that
happens — it is a property of the numbering plan, not of any one device.

``checks/evpn_fabric.py`` catches a collision once objects exist in Infrahub.
These tests catch it earlier and for free, in the YAML that seeds those objects:
the four fixed namespaces in ``22_namespaces.yml`` carry hand-assigned L3 VNIs,
and nothing at load time stops someone re-picking a value that the per-DC L2 pool
in ``01_pools.yml`` will later hand out. They did in fact overlap (the fixed
namespaces sat at 30000-40001, inside the 10001-49999 L2 pool) while every pool
file's own comment asserted they were disjoint.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# transforms/helpers/vxlan.py's ceiling for a VNI encodable in a type-1 RD or a
# type-2 RT once a 4-byte private ASN has taken the other 4 bytes.
MAX_ENCODABLE_VNI = 65535


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
    namespaces = _load_objects(root_dir / "data" / "bootstrap" / "22_namespaces.yml", "IpamNamespace")
    return {ns["name"]: ns["l3_vni"] for ns in namespaces if ns.get("l3_vni") is not None}


@pytest.fixture(scope="module")
def number_pools(root_dir: Path) -> list[dict[str, Any]]:
    """Every CoreNumberPool in bootstrap + the DC-fabric demo, which is where the
    per-DC L2 ``vni`` pools that could collide with the fixed namespaces live."""
    paths = [
        root_dir / "data" / "bootstrap" / "18_vni_pools.yml",
        root_dir / "data" / "bootstrap" / "24_interconnect_pools.yml",
        root_dir / "data" / "demos" / "15_customer_deployments" / "00_dc_fabric" / "01_pools.yml",
    ]
    pools: list[dict[str, Any]] = []
    for path in paths:
        assert path.exists(), f"expected pool file is missing: {path}"
        pools.extend(_load_objects(path, "CoreNumberPool"))
    return pools


def _pools_for_attribute(pools: list[dict[str, Any]], attribute: str) -> list[dict[str, Any]]:
    return [p for p in pools if p.get("node_attribute") == attribute]


class TestFixedNamespaceL3Vnis:
    def test_all_four_namespaces_have_an_l3_vni(self, fixed_namespace_l3_vnis: dict[str, int]) -> None:
        assert set(fixed_namespace_l3_vnis) == {"PROD", "NON-PROD", "INTERNET", "MANAGEMENT"}

    def test_they_are_unique(self, fixed_namespace_l3_vnis: dict[str, int]) -> None:
        """Two VRFs sharing an L3 VNI route into each other."""
        values = list(fixed_namespace_l3_vnis.values())
        assert len(set(values)) == len(values), f"duplicate L3 VNI among fixed namespaces: {fixed_namespace_l3_vnis}"

    def test_they_are_encodable(self, fixed_namespace_l3_vnis: dict[str, int]) -> None:
        for name, vni in fixed_namespace_l3_vnis.items():
            assert vni <= MAX_ENCODABLE_VNI, f"{name}: L3 VNI {vni} cannot be encoded in a type-1 RD / type-2 RT"


class TestFixedNamespacesAvoidEveryPool:
    """The fixed namespaces are allocated by hand, deliberately pool-free. That
    only stays safe while their values sit outside every pool's range — a pool
    has no idea these four exist and will hand out anything in its band.
    """

    def test_no_fixed_namespace_sits_inside_an_l2_vni_pool(
        self, fixed_namespace_l3_vnis: dict[str, int], number_pools: list[dict[str, Any]]
    ) -> None:
        """The regression: a customer segment drawing this VNI from a dcN-vni-pool
        would bridge tenant traffic into the routed VRF using the same VNI."""
        l2_pools = _pools_for_attribute(number_pools, "vni")
        assert l2_pools, "no L2 vni pools found — fixture is not reading the pool files"

        for name, vni in fixed_namespace_l3_vnis.items():
            for pool in l2_pools:
                assert not (pool["start_range"] <= vni <= pool["end_range"]), (
                    f"{name} L3 VNI {vni} is inside L2 pool '{pool['name']}' "
                    f"({pool['start_range']}-{pool['end_range']})"
                )

    def test_no_fixed_namespace_sits_inside_an_l3_vni_pool(
        self, fixed_namespace_l3_vnis: dict[str, int], number_pools: list[dict[str, Any]]
    ) -> None:
        """A pool-allocated per-DC VRF must not be handed one of these four."""
        l3_pools = _pools_for_attribute(number_pools, "l3_vni")
        assert l3_pools, "no l3_vni pools found — fixture is not reading the pool files"

        for name, vni in fixed_namespace_l3_vnis.items():
            for pool in l3_pools:
                assert not (pool["start_range"] <= vni <= pool["end_range"]), (
                    f"{name} L3 VNI {vni} is inside L3 pool '{pool['name']}' "
                    f"({pool['start_range']}-{pool['end_range']})"
                )


class TestPoolBandsAreDisjoint:
    def test_l2_and_l3_pool_ranges_never_overlap(self, number_pools: list[dict[str, Any]]) -> None:
        """One flat VNI namespace per device: an L2 pool overlapping an L3 pool
        makes a collision a matter of allocation order, i.e. eventually certain."""
        l2_pools = _pools_for_attribute(number_pools, "vni")
        l3_pools = _pools_for_attribute(number_pools, "l3_vni")

        for l2 in l2_pools:
            for l3 in l3_pools:
                overlaps = l2["start_range"] <= l3["end_range"] and l3["start_range"] <= l2["end_range"]
                assert not overlaps, (
                    f"L2 pool '{l2['name']}' ({l2['start_range']}-{l2['end_range']}) overlaps "
                    f"L3 pool '{l3['name']}' ({l3['start_range']}-{l3['end_range']})"
                )

    def test_every_evpn_vni_pool_stays_encodable(self, number_pools: list[dict[str, Any]]) -> None:
        """Only the EVPN-carried attributes are RD/RT-encoded. TopologyVirtualCircuit
        VNIs are WAN-scoped and never appear in a route-target, so they are exempt.
        """
        for pool in _pools_for_attribute(number_pools, "vni") + _pools_for_attribute(number_pools, "l3_vni"):
            if pool.get("node") == "TopologyVirtualCircuit":
                continue
            assert pool["end_range"] <= MAX_ENCODABLE_VNI, (
                f"pool '{pool['name']}' can hand out {pool['end_range']}, which cannot be "
                f"encoded in a type-1 RD / type-2 RT (max {MAX_ENCODABLE_VNI})"
            )
