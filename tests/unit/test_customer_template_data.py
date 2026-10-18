"""The customer service tier (TopologyCustomerTemplateDC) is one contract between schema, data and check.

A DC footprint states `design: "M_DC"`; the numbers behind the name live once, in the template rows of
data/bootstrap/21_customer_templates.yml. checks/customer_quota.py holds the footprint to them. These tests
keep the three in line: the schema requires the numbers, every row sets them, the tiers are ordered, every
design a demo footprint names exists, and the demo data itself stays within its tier's segment quota.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.integration.demo_data import ROOT, Declared, attribute, declared, name, schema
from utils.exchange_transit import is_exchange_zone

_BOOTSTRAP = ROOT / "data" / "bootstrap"
_DEMOS = ROOT / "data" / "demos"
_KIND = "TopologyCustomerTemplateDC"
_NUMBERS = ("max_segments", "ports_25g", "ports_100g")
_SHARED = ("S_DC", "M_DC")
_DEDICATED = ("L_DC", "XL_DC")
_TIER_ORDER = (*_SHARED, *_DEDICATED)


def _rows() -> dict[str, Declared]:
    """Template name -> its row in the bootstrap data."""
    return {str(row.item["name"]): row for row in declared(_KIND, _BOOTSTRAP)}


_ROWS = _rows()


def _number(tier: str, column: str) -> int:
    return _ROWS[tier].item[column]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", _NUMBERS)
def test_schema_requires_each_number_with_no_default(column: str) -> None:
    """A tier must state its quota: a silent default would mean 'unlimited' or 'none' by accident."""
    definition = attribute(_KIND, column) or {}
    assert definition.get("kind") == "Number"
    assert definition.get("optional") is False
    assert "default_value" not in definition


def test_schema_template_has_no_size_label() -> None:
    """`size` was a label nothing read; the numbers replaced it."""
    assert attribute(_KIND, "size") is None
    assert attribute("TopologyCustomerTemplate", "size") is None


@pytest.mark.parametrize("kind", ["TopologyCustomerSaas", "TopologyOfficeCustomer"])
def test_removed_customer_kinds_are_gone_from_the_schema(kind: str) -> None:
    """The SaaS footprint and the office wrapper were deleted; nothing may inherit or reference them."""
    assert kind not in schema()
    assert not [k for k, definition in schema().items() if kind in definition.inherit_from]
    assert not [
        (k, rel_name)
        for k, definition in schema().items()
        for rel_name, rel in definition.relationships.items()
        if rel.get("peer") == kind
    ]


# ---------------------------------------------------------------------------
# Template rows
# ---------------------------------------------------------------------------


def test_the_four_tiers_are_declared() -> None:
    """S, M, L, XL, nothing else: every demo design names one of them."""
    assert sorted(_ROWS) == sorted(_TIER_ORDER)


@pytest.mark.parametrize("tier", _TIER_ORDER)
def test_every_row_sets_the_numbers_and_no_size(tier: str) -> None:
    """Whole numbers, no negative quota, at least one segment, and no `size` label."""
    row = _ROWS[tier].item
    assert "size" not in row
    for column in _NUMBERS:
        value = row.get(column)
        assert isinstance(value, int) and not isinstance(value, bool), f"{tier}.{column} = {value!r}"
        assert value >= 0, f"{tier}.{column}"
    assert row["max_segments"] >= 1


@pytest.mark.parametrize("column", _NUMBERS)
def test_shared_tiers_are_below_dedicated_tiers_in_every_column(column: str) -> None:
    """The biggest shared tier is entitled to less than the smallest dedicated one."""
    assert max(_number(tier, column) for tier in _SHARED) < min(_number(tier, column) for tier in _DEDICATED)


@pytest.mark.parametrize("column", _NUMBERS)
def test_tiers_never_shrink_from_one_to_the_next(column: str) -> None:
    """S <= M <= L <= XL: a larger tier is never entitled to less."""
    values = [_number(tier, column) for tier in _TIER_ORDER]
    assert values == sorted(values), f"{column}: {values}"


def test_only_the_large_tiers_are_dedicated() -> None:
    """S/M share the firewall and load balancer, L/XL get their own (the file header's naming rule)."""
    for tier in _TIER_ORDER:
        dedicated = tier in _DEDICATED
        assert _ROWS[tier].item["dedicated_firewall"] is dedicated, tier
        assert _ROWS[tier].item["dedicated_loadbalancer"] is dedicated, tier


def test_the_shared_tiers_allow_no_100g_host_ports() -> None:
    """No shared-tier footprint is entitled to 100G host ports (the server NICs are 25G)."""
    assert [tier for tier in _SHARED if _number(tier, "ports_100g") != 0] == []


# ---------------------------------------------------------------------------
# Footprints name existing designs and stay within their tier
# ---------------------------------------------------------------------------


def _metered_footprints() -> list[Declared]:
    """Every DC footprint under data/demos that states a design (the 30_all, 07_customers and 32_arista_mssg sets)."""
    return [footprint for footprint in declared("TopologyCustomerDC", _DEMOS) if footprint.item.get("design")]


def test_the_scan_finds_designed_footprints_in_every_demo_set() -> None:
    """Guard against the walker silently finding nothing in a set."""
    sets = {footprint.path.relative_to(_DEMOS).parts[0] for footprint in _metered_footprints()}
    assert {"07_customers", "30_all", "32_arista_mssg"} <= sets


@pytest.mark.parametrize("footprint", _metered_footprints(), ids=name)
def test_every_footprint_design_is_a_declared_template(footprint: Declared) -> None:
    """`design` is a relationship by name: a typo would fail the load, or meter nothing."""
    assert footprint.item["design"] in _ROWS


def _segments_per_footprint() -> dict[str, set[str]]:
    """30_all DC footprint name -> the (non exchange-zone) segments naming it, as the quota check counts them."""
    counted: dict[str, set[str]] = {}
    for kind, key in (("ManagedVxlanSegment", "customer_deployments"), ("ManagedVlanSegment", "customer_deployment")):
        for segment in declared(kind, _DEMOS / "30_all"):
            if is_exchange_zone(segment.item.get("security_zone")):
                continue
            named = segment.item.get(key) or []
            for footprint in [named] if isinstance(named, str) else named:
                counted.setdefault(str(footprint), set()).add(name(segment))
    return counted


def test_the_demo_segments_fit_the_segment_quota_of_their_footprint_design() -> None:
    """The numbers are sized from the demo's own use: no 30_all DC footprint declares more segments than its
    design allows (the check would put an ERROR on the demo itself)."""
    counted = _segments_per_footprint()
    over: list[str] = []
    for footprint in _metered_footprints():
        if "30_all" not in footprint.path.parts:
            continue
        used = len(counted.get(name(footprint), ()))
        allowed = _number(footprint.item["design"], "max_segments")
        if used > allowed:
            over.append(f"{name(footprint)} ({footprint.item['design']}): {used} segments, max {allowed}")
    assert not over, "\n".join(over)


def test_the_segment_counter_sees_the_demo_segments() -> None:
    """Guard: the counter finds segments on a known footprint, so the quota test above is not vacuous."""
    counted: dict[str, Any] = _segments_per_footprint()
    assert any(len(segments) >= 2 for segments in counted.values())
