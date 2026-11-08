"""The footprint of a routed SVI and the pbr policies on it, as the device facts read them, and rule S4."""

from __future__ import annotations

from typing import Any

from checks.vrf_instances import svi_content_errors
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_footprint, raw_row, raw_svi


def _policy(policy_id: str = "p1", footprint: str = "fp-a", *, origin: str = "generated") -> dict[str, Any]:
    return {
        "__typename": "RoutingPolicy",
        "id": policy_id,
        "kind": {"value": "pbr"},
        "origin": {"value": origin},
        "customer_deployment": {"node": {"id": footprint}},
    }


def _device(rows: list[dict[str, Any]], svis: list[dict[str, Any]]) -> Any:
    return facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=svis))


def test_a_routed_svi_is_steered_for_the_footprint_of_its_dc_that_has_a_context() -> None:
    """The footprint in the device's hosting DC with a serving context decides."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    facts = _device(rows, [raw_svi("svi-1", 100, "r1")])
    assert facts.pbr_footprints() == {"svi-1": "fp-a"}


def test_a_footprint_in_another_dc_or_without_a_context_does_not_steer() -> None:
    """A stretched segment lists the footprint of each DC; only the local one with a context counts."""
    rows = [
        raw_row("r1", 100, "PROD", "a", footprints=[raw_footprint("fp-far", "dc-other")]),
        raw_row("r2", 101, "PROD", "b", footprints=[raw_footprint("fp-nocontext", context=False)]),
        raw_row("r3", 102, "PROD", "c"),
    ]
    svis = [raw_svi("svi-1", 100, "r1"), raw_svi("svi-2", 101, "r2"), raw_svi("svi-3", 102, "r3")]
    assert _device(rows, svis).pbr_footprints() == {"svi-1": None, "svi-2": None, "svi-3": None}


def test_the_lowest_footprint_id_decides_when_a_segment_has_two_in_the_dc() -> None:
    """The same answer every run."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-b"), raw_footprint("fp-a")])]
    assert _device(rows, [raw_svi("svi-1", 100, "r1")]).pbr_footprints() == {"svi-1": "fp-a"}


def test_the_policies_on_an_svi_are_read_with_their_footprint_and_owner() -> None:
    """Policy id, kind, origin and customer deployment."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    (svi,) = _device(rows, [raw_svi("svi-1", 100, "r1", capabilities=[_policy("p1", "fp-a", origin="manual")])]).svis
    assert [(p.id, p.kind, p.origin, p.customer_id) for p in svi.policies] == [("p1", "pbr", "manual", "fp-a")]


def test_rule_s4_accepts_a_pbr_policy_but_not_another_capability() -> None:
    """A pbr policy is the one capability an SVI may have."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    with_policy = _device(rows, [raw_svi("svi-1", 100, "r1", capabilities=[_policy()])])
    assert svi_content_errors(with_policy) == []
    other = {"__typename": "ManagedBGPPeering", "id": "x"}
    with_other = _device(rows, [raw_svi("svi-1", 100, "r1", capabilities=[_policy(), other])])
    assert "a capability other than a pbr policy" in svi_content_errors(with_other)[0]


def test_a_border_leaf_steers_nothing_until_its_templates_render_pbr() -> None:
    """The SVI of a stretched segment on a border leaf has a footprint with a context, but the border templates render
    no PBR: it is not steered, so no policy claims a steering the config does not have."""
    rows = [raw_row("r1", 100, "PROD", footprints=[raw_footprint("fp-a")])]
    leaf = facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=[raw_svi("svi-1", 100, "r1")]))
    border = facts_of(raw_device("d1", "bl-1", role="border-leaf", rows=rows, interfaces=[raw_svi("svi-1", 100, "r1")]))
    assert leaf.pbr_footprints_by_name() == {"Vlan100": "fp-a"}
    assert border.pbr_footprints_by_name() == {"Vlan100": None}


def test_a_terminated_segment_is_no_svi_and_so_not_steered() -> None:
    """A segment terminated on a firewall stays L2 on the fabric: no routed row, no SVI, no steering."""
    rows = [raw_row("r1", 100, "PROD", terminated=True, footprints=[raw_footprint("fp-a")])]
    facts = facts_of(raw_device("d1", "leaf-1", rows=rows))
    assert facts.pbr_footprints_by_name() == {}
