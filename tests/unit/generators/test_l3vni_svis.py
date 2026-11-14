"""The L3 VNI SVI of a VRF: which the device terminates and the writes that make its role-l3vni interfaces match."""

from __future__ import annotations

from typing import Any

from generators.helpers.svi import L3vniPlan, L3vniWrite, plan_l3vni_svis
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_interface, raw_row, raw_svi

_NAMESPACES: dict[str, dict[str, Any]] = {
    "PROD": {"name": "PROD", "l3_vni": 50001, "vrf_type": "prod"},
    "INTERNET": {"name": "INTERNET", "l3_vni": 50003, "vrf_type": "internet"},
    "NOVNI": {"name": "NOVNI", "l3_vni": None, "vrf_type": "prod"},
}


def _l3vni(svi_id: str, name: str, vrf_id: str | None = None, vrf_namespace: str | None = None) -> dict[str, Any]:
    node = raw_svi(svi_id, 0, None, vrf_id, vrf_namespace, name=name)
    node["role"] = {"value": "l3vni"}
    return node


def test_a_vrf_the_device_routes_a_segment_in_is_terminated_with_the_slot_of_its_type() -> None:
    """Vlan3900 for PROD, whatever else the device carries; the slot follows the namespace type."""
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "PROD")]))
    assert facts.desired_l3vni_svis(_NAMESPACES) == {"Vlan3900": "PROD"}


def test_a_namespace_without_an_l3_vni_or_a_device_that_carries_no_vrf_terminates_nothing() -> None:
    """No L3 VNI on the namespace: nothing; a spine carries no VRF."""
    leaf = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "NOVNI")]))
    spine = facts_of(raw_device("d2", "spine-1", role="spine", rows=[raw_row("r1", 100, "PROD")]))
    assert leaf.desired_l3vni_svis(_NAMESPACES) == {} and spine.desired_l3vni_svis(_NAMESPACES) == {}


def test_the_existing_l3vni_interfaces_are_read_and_are_not_other_interfaces() -> None:
    """A role-l3vni interface is the writer's: it does not block a name as 'taken'."""
    facts = facts_of(raw_device("d1", "leaf-1", interfaces=[_l3vni("i1", "Vlan3900", "vrf-1", "PROD")]))
    assert [svi.name for svi in facts.l3vni_svis] == ["Vlan3900"] and "Vlan3900" not in facts.other_interfaces


def test_a_missing_l3vni_svi_is_created_on_the_instance_of_its_vrf() -> None:
    """Nothing there yet: one write for PROD, on its instance."""
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "PROD")]))
    assert plan_l3vni_svis(facts, _NAMESPACES, {"PROD": "vrf-prod"}) == L3vniPlan(
        writes=(L3vniWrite("Vlan3900", "vrf-prod"),)
    )


def test_everything_in_place_writes_nothing_and_an_instance_that_moved_updates_in_place() -> None:
    """Idempotent; a different instance is a move, not a recreate."""
    rows = [raw_row("r1", 100, "PROD")]
    same = facts_of(raw_device("d1", "leaf-1", rows=rows, interfaces=[_l3vni("i1", "Vlan3900", "vrf-prod", "PROD")]))
    assert not plan_l3vni_svis(same, _NAMESPACES, {"PROD": "vrf-prod"})
    moved = plan_l3vni_svis(same, _NAMESPACES, {"PROD": "vrf-new"})
    assert moved == L3vniPlan(writes=(L3vniWrite("Vlan3900", "vrf-new", existing_id="i1"),))


def test_the_l3vni_svi_of_a_vrf_the_device_stopped_terminating_is_deleted() -> None:
    """No segment or leg in INTERNET any more: its SVI goes, the PROD one stays."""
    interfaces = [_l3vni("i1", "Vlan3900", "vrf-prod", "PROD"), _l3vni("i2", "Vlan3902", "vrf-inet", "INTERNET")]
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "PROD")], interfaces=interfaces))
    assert plan_l3vni_svis(facts, _NAMESPACES, {"PROD": "vrf-prod"}) == L3vniPlan(stale_ids=("i2",))


def test_an_instance_the_device_is_not_in_yet_is_skipped_not_guessed() -> None:
    """No instance for the namespace in this pass: no write (the next pass has it)."""
    facts = facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "PROD")]))
    assert not plan_l3vni_svis(facts, _NAMESPACES, {})


def test_a_hand_made_interface_with_the_name_is_other_not_l3vni() -> None:
    """An interface of another role named like the SVI stays in other_interfaces (the writer skips it with a warning)."""
    facts = facts_of(raw_device("d1", "leaf-1", interfaces=[raw_interface("i9", "Vlan3900", "management")]))
    assert facts.other_interfaces.get("Vlan3900") == "management"


def test_the_transform_renders_no_interface_for_an_l3vni_svi() -> None:
    """The L3 VNI SVI is rendered by the overlay (symmetric IRB, ip forward), never as an interface of the device."""
    from transforms.common import _NON_TEMPLATE_INTERFACE_ROLES

    assert "l3vni" in _NON_TEMPLATE_INTERFACE_ROLES
