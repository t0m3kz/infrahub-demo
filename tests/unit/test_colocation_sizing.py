"""generators/fabric_sizing.py, colocation part: pool sizes derived from the colocation design envelope.

The designs are the rows of data/bootstrap/14c_colocation_templates.yml (tests/unit/simulators/fabric_designs.py).
The golden tables were computed from the contract formulas (25% headroom on a pool that addresses devices or links,
a metro pool exactly as wide as its zones), independently of the module.
"""

from __future__ import annotations

import pytest

from generators.fabric_sizing import (
    COLO_ZONE_LOOPBACK_IPV6_BITS,
    COLO_ZONE_TECHNICAL_IPV6_BITS,
    bits_to_hold,
    colo_metro_asn_units,
    colo_metro_loopback_bits,
    colo_metro_loopback_ipv6_bits,
    colo_metro_management_bits,
    colo_metro_technical_bits,
    colo_metro_technical_ipv6_bits,
    colo_zone_devices,
    colo_zone_links,
    colo_zone_loopback_bits,
    colo_zone_management_bits,
    colo_zone_technical_bits,
)
from tests.unit.simulators.fabric_designs import (
    COLO_METRO_DESIGN_NAMES,
    COLO_ZONE_DESIGN_NAMES,
    colo_metro_design,
    colo_zone_design,
)

# zone design -> (devices, links, loopback, management, technical) host bits
_GOLDEN_ZONE = {
    "EDGE": (2, 1, 3, 3, 2),
    "EDGE_FW": (4, 3, 3, 3, 3),
    "EDGE_FW_LB": (6, 5, 3, 4, 4),
}

# metro design -> (loopback, management, technical, loopback ipv6, technical ipv6, asn units) host bits
_GOLDEN_METRO = {
    "S": (4, 5, 5, 9, 5, 2),
    "M": (5, 6, 6, 10, 6, 4),
}


def test_the_golden_tables_cover_every_bootstrap_colocation_design() -> None:
    """A new design must add its derived sizes here."""
    assert set(_GOLDEN_ZONE) == set(COLO_ZONE_DESIGN_NAMES)
    assert set(_GOLDEN_METRO) == set(COLO_METRO_DESIGN_NAMES)


@pytest.mark.parametrize("name", COLO_ZONE_DESIGN_NAMES)
def test_zone_sizes_match_the_golden_table(name: str) -> None:
    """Each zone design yields the tabulated device, link and pool widths."""
    zone = colo_zone_design(name)

    assert (
        colo_zone_devices(zone),
        colo_zone_links(zone),
        colo_zone_loopback_bits(zone),
        colo_zone_management_bits(zone),
        colo_zone_technical_bits(zone),
    ) == _GOLDEN_ZONE[name]


@pytest.mark.parametrize("name", COLO_METRO_DESIGN_NAMES)
def test_metro_sizes_match_the_golden_table(name: str) -> None:
    """Each metro design yields the tabulated supernet widths and ASN units."""
    metro = colo_metro_design(name)

    assert (
        colo_metro_loopback_bits(metro),
        colo_metro_management_bits(metro),
        colo_metro_technical_bits(metro),
        colo_metro_loopback_ipv6_bits(metro),
        colo_metro_technical_ipv6_bits(metro),
        colo_metro_asn_units(metro),
    ) == _GOLDEN_METRO[name]


@pytest.mark.parametrize("name", COLO_ZONE_DESIGN_NAMES)
def test_zone_pools_keep_a_quarter_headroom(name: str) -> None:
    """Loopbacks (edges plus two spare), management (every device plus two spare), technical (two per link) fit in 75%."""
    zone = colo_zone_design(name)

    assert (zone["max_edges"] + 2) * 4 <= 3 * 2 ** colo_zone_loopback_bits(zone)
    assert (colo_zone_devices(zone) + 2) * 4 <= 3 * 2 ** colo_zone_management_bits(zone)
    assert 2 * max(1, colo_zone_links(zone)) * 4 <= 3 * 2 ** colo_zone_technical_bits(zone)


@pytest.mark.parametrize("name", COLO_METRO_DESIGN_NAMES)
def test_metro_supernets_hold_max_zones_of_the_widest_zone_pool(name: str) -> None:
    """A metro pool holds max_zones of the widest listed zone pool, and not one bit less."""
    metro = colo_metro_design(name)
    zones = metro["zone_templates"]
    cases = (
        (colo_metro_loopback_bits(metro), colo_zone_loopback_bits),
        (colo_metro_management_bits(metro), colo_zone_management_bits),
        (colo_metro_technical_bits(metro), colo_zone_technical_bits),
    )

    for bits, zone_bits in cases:
        needed = metro["max_zones"] * 2 ** max(zone_bits(zone) for zone in zones)
        assert bits == bits_to_hold(needed)
        assert 2**bits >= needed > 2 ** (bits - 1)


@pytest.mark.parametrize("name", COLO_METRO_DESIGN_NAMES)
def test_metro_ipv6_supernets_hold_one_fixed_slice_per_zone(name: str) -> None:
    """The VTEP /120 and interlink /124 a zone takes are fixed, so the metro blocks scale with max_zones only."""
    metro = colo_metro_design(name)

    assert colo_metro_loopback_ipv6_bits(metro) == bits_to_hold(metro["max_zones"] * 2**COLO_ZONE_LOOPBACK_IPV6_BITS)
    assert colo_metro_technical_ipv6_bits(metro) == bits_to_hold(metro["max_zones"] * 2**COLO_ZONE_TECHNICAL_IPV6_BITS)


def test_a_single_edge_zone_has_no_interlink() -> None:
    """Only an edge pair has an interlink; a lone edge still gets a technical pool of one link."""
    zone = {"max_edges": 1, "max_firewalls": 0, "max_load_balancers": 0}

    assert colo_zone_links(zone) == 0
    assert colo_zone_technical_bits(zone) == 2


def test_a_metro_design_without_zone_templates_cannot_be_sized() -> None:
    """There is nothing to size a metro from when it lists no zone designs."""
    metro = {"name": "EMPTY", "max_zones": 2, "zone_templates": []}

    with pytest.raises(ValueError, match="lists no zone_templates"):
        colo_metro_loopback_bits(metro)
