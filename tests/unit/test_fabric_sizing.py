"""generators/fabric_sizing.py: every fabric pool size, derived from the design envelope.

The designs are read from data/bootstrap/14b_fabric_templates.yml (tests/unit/simulators/fabric_designs.py).
The golden tables below were computed from the contract formulas, independently of the module; the formula
tests check the module against the formulas themselves (headroom, minimality, containment), and the capacity
tests check that the bootstrap parent pools hold every demo DC's pools.
"""

from __future__ import annotations

import ipaddress
from typing import Any

import pytest

from generators.fabric_sizing import (
    bits_to_hold,
    bits_with_headroom,
    dc_loopback_bits,
    dc_management_bits,
    dc_management_devices,
    dc_technical_bits,
    fabric_loopback_bits,
    host_bits_to_prefix_length,
    max_sibling_spines,
    pod_border_leafs,
    pod_leafs,
    pod_links,
    pod_loopback_bits,
    pod_technical_bits,
    pod_tors,
)
from generators.helpers.routing import underlay_pool_is_ipv6
from tests.integration.demo_data import ROOT, declared
from tests.unit.simulators.fabric_designs import (
    DC_DESIGN_NAMES,
    POD_DESIGN_NAMES,
    custom_dc_design,
    custom_pod_design,
    dc_design,
    pod_design,
)

# (DC design, pod design) -> (links, pod technical host bits, pod loopback host bits)
_GOLDEN_POD = {
    ("S", "S_MIDDLE"): (24, 6, 4),
    ("S", "S_TOR"): (144, 9, 6),
    ("S", "S_MIXED"): (160, 9, 6),
    ("S", "S_BORDER_SPINE_POD"): (24, 6, 4),
    ("S", "M_MIXED"): (228, 10, 7),
    ("S", "M_MIDDLE"): (60, 8, 5),
    ("M", "S_MIDDLE"): (42, 7, 4),
    ("M", "S_TOR"): (180, 9, 6),
    ("M", "S_MIXED"): (196, 10, 6),
    ("M", "S_BORDER_SPINE_POD"): (40, 7, 4),
    ("M", "M_MIXED"): (255, 10, 7),
    ("M", "M_MIDDLE"): (87, 8, 5),
    ("L", "S_MIDDLE"): (26, 7, 4),
    ("L", "S_TOR"): (148, 9, 6),
    ("L", "S_MIXED"): (164, 9, 6),
    ("L", "S_BORDER_SPINE_POD"): (24, 6, 4),
    ("L", "M_MIXED"): (231, 10, 7),
    ("L", "M_MIDDLE"): (63, 8, 5),
    ("XL", "S_MIDDLE"): (26, 7, 4),
    ("XL", "S_TOR"): (148, 9, 6),
    ("XL", "S_MIXED"): (164, 9, 6),
    ("XL", "S_BORDER_SPINE_POD"): (24, 6, 4),
    ("XL", "M_MIXED"): (231, 10, 7),
    ("XL", "M_MIDDLE"): (63, 8, 5),
}

# DC design -> (technical, loopback, DC-tier loopback or None, management) host bits
_GOLDEN_DC = {
    "S": (11, 8, None, 8),
    "M": (12, 10, 3, 9),
    "L": (13, 11, 4, 10),
    "XL": (15, 12, 4, 11),
}

_PAIRS = [(dc, pod) for dc in DC_DESIGN_NAMES for pod in POD_DESIGN_NAMES]


def _hosts(dc: str, pod: str) -> bool:
    return pod in {template["name"] for template in dc_design(dc)["pod_templates"]}


# ---------------------------------------------------------------------------
# Golden tables
# ---------------------------------------------------------------------------


def test_the_golden_tables_cover_every_bootstrap_design() -> None:
    """A new design must add its derived sizes here."""
    assert set(_GOLDEN_POD) == set(_PAIRS)
    assert set(_GOLDEN_DC) == set(DC_DESIGN_NAMES)


@pytest.mark.parametrize(("dc", "pod"), _PAIRS)
def test_pod_pool_sizes_match_the_golden_table(dc: str, pod: str) -> None:
    dc_row, pod_row = dc_design(dc), pod_design(pod)

    assert (pod_links(pod_row, dc_row), pod_technical_bits(pod_row, dc_row), pod_loopback_bits(pod_row)) == (
        _GOLDEN_POD[dc, pod]
    )


@pytest.mark.parametrize("dc", DC_DESIGN_NAMES)
def test_dc_pool_sizes_match_the_golden_table(dc: str) -> None:
    row = dc_design(dc)

    assert (
        dc_technical_bits(row),
        dc_loopback_bits(row),
        fabric_loopback_bits(row),
        dc_management_bits(row),
    ) == _GOLDEN_DC[dc]


@pytest.mark.parametrize("dc", DC_DESIGN_NAMES)
def test_every_dc_design_hosts_every_pod_design(dc: str) -> None:
    """The bootstrap rule: a DC design lists all six pod designs."""
    assert all(_hosts(dc, pod) for pod in POD_DESIGN_NAMES)


# ---------------------------------------------------------------------------
# The formulas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("dc", "pod"), _PAIRS)
def test_pod_technical_pool_is_the_smallest_with_a_quarter_headroom(dc: str, pod: str) -> None:
    """2 addresses per link fit in 75% of the pool, and not in 75% of one bit less."""
    dc_row, pod_row = dc_design(dc), pod_design(pod)
    addresses, bits = 2 * pod_links(pod_row, dc_row), pod_technical_bits(pod_row, dc_row)

    assert addresses * 4 <= 3 * 2**bits
    assert addresses * 4 > 3 * 2 ** (bits - 1)


@pytest.mark.parametrize(("dc", "pod"), _PAIRS)
def test_pod_links_count_downlinks_and_uplinks(dc: str, pod: str) -> None:
    """S x (L + T + B), plus S x SS to the super-spines or (P - 1) x S x S_sib back-to-back."""
    dc_row, pod_row = dc_design(dc), pod_design(pod)
    spines = pod_row["max_spines_per_pod"]
    downlinks = spines * (pod_leafs(pod_row) + pod_tors(pod_row) + pod_border_leafs(pod_row, dc_row))
    if dc_row["max_super_spines_per_fabric"]:
        uplinks = spines * dc_row["max_super_spines_per_fabric"]
    else:
        uplinks = (dc_row["max_pods"] - 1) * spines * max_sibling_spines(dc_row)

    assert pod_links(pod_row, dc_row) == downlinks + uplinks


@pytest.mark.parametrize("pod", POD_DESIGN_NAMES)
def test_pod_loopback_pool_holds_every_device_plus_two(pod: str) -> None:
    row = pod_design(pod)
    devices = row["max_spines_per_pod"] + pod_leafs(row) + pod_tors(row) + 2
    bits = pod_loopback_bits(row)

    assert 2 ** (bits - 1) < devices <= 2**bits


@pytest.mark.parametrize("dc", DC_DESIGN_NAMES)
def test_dc_technical_pool_holds_max_pods_widest_pod_pools_and_the_hyper_spine_links(dc: str) -> None:
    row = dc_design(dc)
    widest = max(pod_technical_bits(pod, row) for pod in row["pod_templates"])
    needed = row["max_pods"] * 2**widest + 2 * row["max_super_spines_per_fabric"] * row["max_hyper_spines_per_fabric"]
    bits = dc_technical_bits(row)

    assert 2 ** (bits - 1) < needed <= 2**bits


@pytest.mark.parametrize("dc", DC_DESIGN_NAMES)
def test_dc_loopback_pool_holds_max_pods_widest_pod_pools_and_the_tier_pool(dc: str) -> None:
    row = dc_design(dc)
    widest = max(pod_loopback_bits(pod) for pod in row["pod_templates"])
    tier_bits = fabric_loopback_bits(row)
    needed = row["max_pods"] * 2**widest + (2**tier_bits if tier_bits is not None else 0)
    bits = dc_loopback_bits(row)

    assert 2 ** (bits - 1) < needed <= 2**bits


@pytest.mark.parametrize("dc", DC_DESIGN_NAMES)
def test_dc_management_pool_holds_every_switch_with_a_quarter_headroom(dc: str) -> None:
    row = dc_design(dc)
    devices, bits = dc_management_devices(row), dc_management_bits(row)

    assert devices * 4 <= 3 * 2**bits < devices * 8


def test_max_sibling_spines_is_the_widest_pod_design() -> None:
    """The super-spine port stride: the widest spine tier of any pod design the DC may host."""
    design = custom_dc_design(
        pod_templates=[custom_pod_design("A", max_spines_per_pod=2), custom_pod_design("B", max_spines_per_pod=3)]
    )

    assert max_sibling_spines(design) == 3


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("addresses", "headroom_bits", "exact_bits"),
    [(0, 0, 0), (1, 1, 0), (3, 2, 2), (4, 3, 2), (12, 4, 4), (13, 5, 4), (384, 9, 9), (385, 10, 9)],
)
def test_bit_widths(addresses: int, headroom_bits: int, exact_bits: int) -> None:
    assert bits_with_headroom(addresses) == headroom_bits
    assert bits_to_hold(addresses) == exact_bits


@pytest.mark.parametrize(("bits", "ipv6", "prefix"), [(8, False, 24), (8, True, 120), (0, False, 32), (0, True, 128)])
def test_host_bits_to_prefix_length(bits: int, ipv6: bool, prefix: int) -> None:
    assert host_bits_to_prefix_length(bits, ipv6=ipv6) == prefix


def test_a_dc_without_dc_tiers_has_no_tier_loopback_pool() -> None:
    """No super-spine, hyper-spine or border-leaf: no DC-tier pool, the DC loopback pool holds only pod pools."""
    design = custom_dc_design()

    assert fabric_loopback_bits(design) is None
    assert dc_loopback_bits(design) == bits_to_hold(2 * 2 ** pod_loopback_bits(custom_pod_design()))


@pytest.mark.parametrize(("super_spines", "hyper_spines", "border_leafs"), [(1, 0, 0), (0, 1, 0), (0, 0, 1)])
def test_any_dc_tier_alone_gets_a_tier_loopback_pool(super_spines: int, hyper_spines: int, border_leafs: int) -> None:
    design = custom_dc_design(
        max_super_spines_per_fabric=super_spines,
        max_hyper_spines_per_fabric=hyper_spines,
        max_border_leafs_per_fabric=border_leafs,
    )

    assert fabric_loopback_bits(design) == bits_with_headroom(1 + 2)


def test_a_pod_never_hosts_more_border_leafs_than_the_fabric() -> None:
    pod = custom_pod_design(max_border_leafs_per_pod=4)

    assert pod_border_leafs(pod, custom_dc_design(max_border_leafs_per_fabric=2)) == 2
    assert pod_border_leafs(pod, custom_dc_design(max_border_leafs_per_fabric=0)) == 0


def test_a_single_pod_dc_back_to_back_has_no_uplinks() -> None:
    pod = custom_pod_design()
    design = custom_dc_design(max_pods=1, pod_templates=[pod])

    assert pod_links(pod, design) == pod["max_spines_per_pod"] * pod_leafs(pod)


def test_hyper_spine_links_draw_from_the_dc_technical_pool() -> None:
    """Four super-spines x two hyper-spines: 8 links, 16 addresses on top of the pods' blocks."""
    pod = custom_pod_design()
    design = custom_dc_design(max_super_spines_per_fabric=4, pod_templates=[pod])
    with_hyper = custom_dc_design(max_super_spines_per_fabric=4, max_hyper_spines_per_fabric=2, pod_templates=[pod])

    pods_block = 2 * 2 ** pod_technical_bits(pod, design)
    assert dc_technical_bits(design) == bits_to_hold(pods_block)
    assert dc_technical_bits(with_hyper) == bits_to_hold(pods_block + 16)


def test_a_dc_design_without_pod_templates_cannot_be_sized() -> None:
    with pytest.raises(ValueError, match="lists no pod_templates"):
        dc_technical_bits(custom_dc_design(pod_templates=[]))


# ---------------------------------------------------------------------------
# The bootstrap parent pools hold every demo DC
# ---------------------------------------------------------------------------


def _parent_host_bits(pool_name: str) -> int:
    """Host bits of the bootstrap prefix behind CoreIPPrefixPool ``pool_name``."""
    (pool,) = [
        row for row in declared("CoreIPPrefixPool", ROOT / "data" / "bootstrap") if row.item["name"] == pool_name
    ]
    (prefix,) = pool.item["resources"]["data"]
    network = ipaddress.ip_network(prefix["prefix"])
    return network.max_prefixlen - network.prefixlen


def _demo_dcs() -> list[dict[str, Any]]:
    return [row.item for row in declared("TopologyDataCenter", ROOT / "data" / "demos")]


def test_the_scan_finds_the_demo_dcs() -> None:
    """Guard: the capacity tests below are not vacuous."""
    assert len(_demo_dcs()) >= 10
    assert {dc["design"] for dc in _demo_dcs()} <= set(DC_DESIGN_NAMES)


@pytest.mark.parametrize(
    ("parent", "pool"),
    [("Management-IPv4", "management"), ("Technical-IPv4", "technical"), ("Loopback-IPv4", "loopback")],
)
def test_the_ipv4_parent_pools_hold_every_demo_dc_at_once(parent: str, pool: str) -> None:
    """Every demo DC loaded together (the worst case) still fits its IPv4 parent pool."""
    size_of = {"management": dc_management_bits, "technical": dc_technical_bits, "loopback": dc_loopback_bits}[pool]
    ipv4_dcs = [
        dc
        for dc in _demo_dcs()
        if pool == "management" or not underlay_pool_is_ipv6(dc.get("underlay_protocol", "ipv6"), pool)
    ]
    used = sum(2 ** size_of(dc_design(dc["design"])) for dc in ipv4_dcs)

    assert used <= 2 ** _parent_host_bits(parent), f"{parent}: {used} addresses for {len(ipv4_dcs)} DC(s)"
