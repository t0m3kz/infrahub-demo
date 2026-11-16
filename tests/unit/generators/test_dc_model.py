"""Unit tests for DcFabricModel (generators/model/dc_model.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.model.dc_model import DC_VALID_FABRIC_ROLES, DcFabricModel
from tests.unit.fabric_model_helpers import cleaned
from tests.unit.simulators.fabric_designs import dc_design

_TEMPLATE = {
    "id": "tpl-1",
    "template_kind": "TemplateDcimPhysicalDevice",
    "interfaces": [{"name": "Eth1", "role": "uplink"}],
}


def _entry(role: str, quantity: int) -> dict[str, Any]:
    return {"role": role, "quantity": quantity, "template": _TEMPLATE}


def _compile(design: str = "L", **overrides: Any) -> DcFabricModel:
    """Compile a DC through the raw GraphQL shape and clean_data(), as the generator receives it."""
    node: dict[str, Any] = {
        "id": "dc-1",
        "name": "DC1",
        "index": 3,
        "design": dc_design(design),
        "fabric_templates": [],
        **overrides,
    }
    return DcFabricModel.compile(cleaned("TopologyDataCenter", node))


def test_identity_and_defaults_come_from_the_payload() -> None:
    """Names, index and the underlay/routing defaults of an unset DC."""
    model = _compile()

    assert (model.dc_id, model.dc_name, model.fabric_name, model.dc_index) == ("dc-1", "DC1", "dc1", 3)
    assert model.underlay_protocol == "ipv6"
    assert model.routing_strategy == "ebgp-ebgp"


def test_super_spine_design_sizes_every_pool_including_the_dc_tier_pool() -> None:
    """The L design has DC-level tiers: four pools, IPv6 technical/loopback, IPv4 management."""
    model = _compile("L")

    assert model.design_mode == "super-spine"
    assert model.pools_to_allocate == {"technical": 115, "loopback": 117, "management": 22, "dc-fabric-loopback": 124}
    assert list(model.pools_to_allocate) == ["technical", "loopback", "management", "dc-fabric-loopback"]


def test_back_to_back_design_without_dc_tiers_has_no_dc_tier_pool() -> None:
    """The S design has no super-spine, hyper-spine or DC-level border-leaf: no DC-fabric loopback pool."""
    model = _compile("S")

    assert model.design_mode == "back-to-back"
    assert model.pools_to_allocate == {"technical": 117, "loopback": 120, "management": 24}


@pytest.mark.parametrize(
    ("underlay", "technical", "loopback"),
    [("ipv4", 21, 24), ("ipv6", 117, 120), ("dual_stack", 117, 24)],
)
def test_each_pool_is_sliced_in_the_family_of_its_underlay(underlay: str, technical: int, loopback: int) -> None:
    """dual_stack: IPv6 technical, IPv4 loopback; management is IPv4 whatever the underlay."""
    pools = _compile("S", underlay_protocol=underlay).pools_to_allocate

    assert (pools["technical"], pools["loopback"], pools["management"]) == (technical, loopback, 24)


def test_asn_range_is_deterministic_and_sized_by_the_design() -> None:
    """The range hashes from the DC name; a larger design widens it, and the overlay AS (end + 1) stays outside."""
    small, large = _compile("S"), _compile("L")

    assert (small.asn_start, small.asn_end) == (4208504000, 4208504499)
    assert large.asn_start == small.asn_start
    assert large.asn_end > small.asn_end
    assert _compile("S", name="DC2").asn_start != small.asn_start


@pytest.mark.parametrize(
    ("strategy", "needs_pool"),
    [("ebgp-ebgp", True), ("ebgp-ibgp", True), ("ospf-ibgp", False)],
)
def test_only_ebgp_strategies_need_an_asn_pool(strategy: str, needs_pool: bool) -> None:
    """ospf-ibgp needs no per-device AS, so no pool."""
    model = _compile("S", routing_strategy=strategy)

    assert model.routing_strategy == strategy
    assert model.requires_asn_pool is needs_pool


def test_role_quantities_sum_the_entries_of_a_role() -> None:
    """Zero-quantity entries count nothing; entries of other roles are not mixed in."""
    model = _compile(
        "L",
        fabric_templates=[
            _entry("super-spine", 2),
            _entry("super-spine", 1),
            _entry("hyper-spine", 0),
            _entry("border-leaf", 4),
        ],
    )

    assert (model.super_spine_quantity, model.hyper_spine_quantity) == (3, 0)


def test_invalid_roles_are_listed_in_entry_order_not_counted() -> None:
    """A role no DC tier can place is reported for the generator to skip; the valid entries are untouched."""
    model = _compile("L", fabric_templates=[_entry("leaf", 2), _entry("super-spine", 2), _entry("edge", 1)])

    assert model.invalid_roles == ("leaf", "edge")
    assert model.super_spine_quantity == 2
    assert {"leaf", "edge"}.isdisjoint(DC_VALID_FABRIC_ROLES)


def test_super_spines_over_the_design_cap_are_an_error_message() -> None:
    """The S design hosts none: requesting two names the DC, the request and the cap."""
    model = _compile("S", fabric_templates=[_entry("super-spine", 2)])

    assert model.super_spine_error() == ("DC DC1 requests 2 super-spines but the assigned design allows at most 0")


def test_super_spines_within_the_cap_have_no_error() -> None:
    assert _compile("L", fabric_templates=[_entry("super-spine", 4)]).super_spine_error() is None


def test_hyper_spines_over_the_design_cap_are_an_error_message() -> None:
    model = _compile("L", fabric_templates=[_entry("hyper-spine", 1)])

    assert model.hyper_spine_error() == "DC DC1 requests 1 hyper-spines but the assigned design allows at most 0"


def test_hyper_spines_within_the_cap_have_no_error() -> None:
    assert _compile("XL", fabric_templates=[_entry("hyper-spine", 1)]).hyper_spine_error() is None


@pytest.mark.parametrize("missing", ["id", "name", "index", "design"])
def test_a_payload_without_a_required_field_raises_key_error(missing: str) -> None:
    """The generator turns this into a logged failure; the model never invents a default."""
    node = cleaned(
        "TopologyDataCenter",
        {"id": "dc-1", "name": "DC1", "index": 1, "design": dc_design("S"), "fabric_templates": []},
    )
    del node[missing]

    with pytest.raises(KeyError):
        DcFabricModel.compile(node)


def test_a_design_without_pod_templates_is_a_value_error() -> None:
    """Nothing to size the DC pools from."""
    design = dc_design("S")
    design["pod_templates"] = []

    node = {"id": "dc-1", "name": "DC1", "index": 1, "design": design, "fabric_templates": []}

    with pytest.raises(ValueError, match="pod_templates"):
        DcFabricModel.compile(cleaned("TopologyDataCenter", node))
