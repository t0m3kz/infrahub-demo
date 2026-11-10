"""The customer service tier (TopologyCustomerTemplateDC, ...Colo, ...Cloud) is one contract between schema, data and check.

A footprint states `design: "M"` (required, a template of its own kind); the numbers behind the name live once, in the
template rows of data/bootstrap/21_customer_templates.yml, or an earlier file in the same demo for a custom service
mix. checks/customer_quota.py holds the footprint to them. These tests
keep the three in line: the schema requires the numbers, every row sets them, the tiers are ordered and have the
agreed values, every design a demo footprint names exists, and the demo data itself stays within its tier's segment
quota.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.integration.demo_data import (
    ROOT,
    Declared,
    attribute,
    declared,
    human_friendly_id,
    name,
    relationship,
    schema,
)
from utils.exchange_transit import is_exchange_zone

_BOOTSTRAP = ROOT / "data" / "bootstrap"
_DEMOS = ROOT / "data" / "demos"
_KIND = "TopologyCustomerTemplateDC"
_NUMBERS = ("max_segments", "address_prefix_length", "ports_25g", "ports_100g")
_SHARED = ("S", "M")
_DEDICATED = ("L", "XL")
_TIER_ORDER = (*_SHARED, *_DEDICATED)
_TEMPLATE_KINDS = {
    "TopologyCustomerDC": "TopologyCustomerTemplateDC",
    "TopologyCustomerColocation": "TopologyCustomerTemplateColo",
    "TopologyCustomerCloud": "TopologyCustomerTemplateCloud",
}
# tier -> (max_segments, address_prefix_length) shared by the data center and colocation kinds.
_BLOCK_TIERS = {"S": (2, 23), "M": (6, 21), "L": (12, 20), "XL": (24, 19)}
# tier -> (max_segments, max_virtual_networks, address_prefix_length) of the cloud kind: the length is the routed range of
# the footprint, not a block for its segments.
_CLOUD_TIERS = {"S": (2, 1, 28), "M": (6, 3, 27), "L": (12, 6, 26), "XL": (24, 12, 25)}


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


@pytest.mark.parametrize("kind", [*_TEMPLATE_KINDS.values()])
def test_the_generic_carries_the_quota_and_the_address_block_once(kind: str) -> None:
    """max_segments and address_prefix_length are the generic's: required, no default, the block between /16 and /28,
    and no template node redefines them (a node must not redefine a generic attribute)."""
    quota = attribute(kind, "max_segments") or {}
    block = attribute(kind, "address_prefix_length") or {}
    assert quota["optional"] is False and "default_value" not in quota
    assert quota["parameters"] == {"min_value": 1}
    assert block["optional"] is False and "default_value" not in block
    assert block["parameters"] == {"min_value": 16, "max_value": 28}
    assert "max_segments" in schema()["TopologyCustomerTemplate"].attributes
    assert "address_prefix_length" in schema()["TopologyCustomerTemplate"].attributes
    assert {"max_segments", "address_prefix_length"}.isdisjoint(schema()[kind].attributes)


def test_the_cloud_template_states_its_virtual_network_quota() -> None:
    """max_virtual_networks is cloud only: required, at least one."""
    definition = attribute("TopologyCustomerTemplateCloud", "max_virtual_networks") or {}
    assert definition["optional"] is False and definition["parameters"] == {"min_value": 1}
    assert attribute("TopologyCustomerTemplateDC", "max_virtual_networks") is None
    assert attribute("TopologyCustomerTemplateColo", "max_virtual_networks") is None


@pytest.mark.parametrize(("footprint", "template"), [*_TEMPLATE_KINDS.items()])
def test_a_footprint_names_a_design_of_its_own_kind_and_must(footprint: str, template: str) -> None:
    """`design` is required on the data center, colocation and cloud footprints and peers their own template kind."""
    design = relationship(footprint, "design") or {}
    assert design["peer"] == template
    assert design["optional"] is False
    assert design["cardinality"] == "one"


@pytest.mark.parametrize("kind", [*_TEMPLATE_KINDS.values()])
def test_every_template_kind_has_the_same_hfid_and_a_menu_entry(kind: str) -> None:
    """Names clash between the kinds only in appearance: each kind is its own node with a name HFID."""
    assert human_friendly_id(kind) == ("name__value",)
    assert f"kind: {kind}" in (ROOT / "menu" / "menu.yml").read_text()


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


@pytest.mark.parametrize("kind", [*_TEMPLATE_KINDS.values()])
def test_the_four_tiers_are_declared_for_every_kind(kind: str) -> None:
    """Bootstrap keeps exactly the four standard tiers S, M, L and XL in each kind; custom mixes belong to their own
    demo."""
    assert sorted(str(row.item["name"]) for row in declared(kind, _BOOTSTRAP)) == sorted(_TIER_ORDER)


@pytest.mark.parametrize("tier", _TIER_ORDER)
def test_the_data_center_and_colocation_tiers_have_the_agreed_values(tier: str) -> None:
    """S 2/23, M 6/21, L 12/20, XL 24/19 (max_segments / address_prefix_length) in both kinds."""
    wanted = _BLOCK_TIERS[tier]
    for kind in ("TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"):
        row = next(r for r in declared(kind, _BOOTSTRAP) if r.item["name"] == tier).item
        assert (row["max_segments"], row["address_prefix_length"]) == wanted, f"{kind} {tier}"


@pytest.mark.parametrize("tier", _TIER_ORDER)
def test_the_cloud_tiers_have_the_agreed_values(tier: str) -> None:
    """Cloud: max_segments, max_virtual_networks and the routed range of the footprint (/28 to /25)."""
    row = next(r for r in declared("TopologyCustomerTemplateCloud", _BOOTSTRAP) if r.item["name"] == tier).item
    assert (row["max_segments"], row["max_virtual_networks"], row["address_prefix_length"]) == _CLOUD_TIERS[tier]


@pytest.mark.parametrize("kind", ["TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo"])
def test_the_address_block_holds_the_tier(kind: str) -> None:
    """Rule 9(a) of checks/customer_quota.py on the data: a /<address_prefix_length> holds max_segments /24s."""
    for row in declared(kind, _BOOTSTRAP):
        assert 2 ** (24 - row.item["address_prefix_length"]) >= row.item["max_segments"], f"{kind} {row.item['name']}"


@pytest.mark.parametrize("tier", _TIER_ORDER)
def test_every_row_sets_the_numbers_and_no_size(tier: str) -> None:
    """Whole numbers, no negative quota, at least one segment, a block of /16 to /28, and no `size` label."""
    row = _ROWS[tier].item
    assert "size" not in row
    for column in _NUMBERS:
        value = row.get(column)
        assert isinstance(value, int) and not isinstance(value, bool), f"{tier}.{column} = {value!r}"
        assert value >= 0, f"{tier}.{column}"
    assert row["max_segments"] >= 1
    assert 16 <= row["address_prefix_length"] <= 28


@pytest.mark.parametrize("column", ("max_segments", "ports_25g", "ports_100g"))
def test_shared_tiers_are_below_dedicated_tiers_in_every_column(column: str) -> None:
    """The biggest shared tier is entitled to less than the smallest dedicated one."""
    assert max(_number(tier, column) for tier in _SHARED) < min(_number(tier, column) for tier in _DEDICATED)


@pytest.mark.parametrize("column", ("max_segments", "ports_25g", "ports_100g"))
def test_tiers_never_shrink_from_one_to_the_next(column: str) -> None:
    """S <= M <= L <= XL: a larger tier is never entitled to less."""
    values = [_number(tier, column) for tier in _TIER_ORDER]
    assert values == sorted(values), f"{column}: {values}"


def test_a_larger_tier_never_gets_a_smaller_block() -> None:
    """address_prefix_length falls (the block grows) from S to XL."""
    values = [_number(tier, "address_prefix_length") for tier in _TIER_ORDER]
    assert values == sorted(values, reverse=True), values


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


def _design_row(footprint: Declared) -> Declared:
    """Resolve a standard tier or a valid, earlier custom definition in this footprint's own demo."""
    design = footprint.item["design"]
    if design in _ROWS:
        return _ROWS[design]
    demo = _DEMOS / footprint.path.relative_to(_DEMOS).parts[0]
    matches = [row for row in declared(_KIND, demo) if row.item.get("name") == design]
    assert matches, f"{design}: no template in {demo.name}"
    assert all(row.path < footprint.path for row in matches), f"{design}: defined after its consumer"
    for row in matches:
        for column in _NUMBERS:
            value = row.item.get(column)
            assert isinstance(value, int) and not isinstance(value, bool) and value >= 0
        assert row.item["max_segments"] >= 1
        for flag in ("dedicated_firewall", "dedicated_loadbalancer"):
            assert isinstance(row.item.get(flag), bool)
    return matches[0]


@pytest.mark.parametrize("footprint", _metered_footprints(), ids=name)
def test_every_footprint_design_is_a_declared_template(footprint: Declared) -> None:
    """A custom design must be valid and defined earlier in this demo, never borrowed from another demo."""
    assert _design_row(footprint).item["name"] == footprint.item["design"]


def test_custom_template_from_another_demo_does_not_resolve() -> None:
    """Moving only the footprint to another demo must not expose 00_full_topology's custom template."""
    footprint = next(row for row in _metered_footprints() if name(row) == "C900-P-DC-WAW1")
    foreign = replace(footprint, path=_DEMOS / "07_customers" / "99_custom_waw.yml")
    with pytest.raises(AssertionError, match="no template in 07_customers"):
        test_every_footprint_design_is_a_declared_template(foreign)


@pytest.mark.parametrize("fault", ["later", "negative_quota", "boolean_quota", "invalid_flag"])
def test_custom_template_must_be_earlier_and_valid(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Local scope alone does not permit a late definition or a malformed custom entitlement."""
    footprint = next(row for row in _metered_footprints() if name(row) == "C900-P-DC-WAW1")
    template = next(row for row in declared(_KIND, _DEMOS / "00_full_topology") if name(row) == "M_INTERNAL")
    item = dict(template.item)
    path = template.path
    if fault == "later":
        path = _DEMOS / "00_full_topology" / "99_late.yml"
    elif fault == "negative_quota":
        item["ports_25g"] = -1
    elif fault == "boolean_quota":
        item["max_segments"] = True
    else:
        item["dedicated_firewall"] = "true"
    monkeypatch.setitem(globals(), "declared", lambda *_: [replace(template, path=path, item=item)])
    with pytest.raises(AssertionError):
        test_every_footprint_design_is_a_declared_template(footprint)


def _segments_per_footprint() -> dict[str, set[str]]:
    """30_all DC footprint name -> the (non exchange-zone) segments naming it, as the quota check counts them."""
    counted: dict[str, set[str]] = {}
    for segment in declared("ManagedVxlanSegment", _DEMOS / "30_all"):
        if is_exchange_zone(segment.item.get("security_zone")):
            continue
        for footprint in segment.item.get("customer_deployments") or []:
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
        allowed = _design_row(footprint).item["max_segments"]
        if used > allowed:
            over.append(f"{name(footprint)} ({footprint.item['design']}): {used} segments, max {allowed}")
    assert not over, "\n".join(over)


def test_the_segment_counter_sees_the_demo_segments() -> None:
    """Guard: the counter finds segments on a known footprint, so the quota test above is not vacuous."""
    counted: dict[str, Any] = _segments_per_footprint()
    assert any(len(segments) >= 2 for segments in counted.values())


def test_tier_names_are_unique_per_kind_not_across_the_generic() -> None:
    """The three template kinds all name their tiers S, M, L and XL, so the name is constrained on each node and NOT on
    the generic: a constraint or an HFID on the generic makes the server resolve an upsert by name across kinds (a
    colocation "S" found the DC "S" and the bootstrap load failed with NODE_NOT_FOUND)."""
    generic = schema()["TopologyCustomerTemplate"]
    assert not generic.attributes["name"].get("unique"), "name must not be unique on the generic"
    assert not generic.uniqueness_constraints, "the generic must not constrain its kinds' names together"
    assert not generic.human_friendly_id, "the generic must not have an HFID: it is enforced across its kinds"
    for kind in ("TopologyCustomerTemplateDC", "TopologyCustomerTemplateColo", "TopologyCustomerTemplateCloud"):
        assert schema()[kind].uniqueness_constraints == (("name__value",),), kind
