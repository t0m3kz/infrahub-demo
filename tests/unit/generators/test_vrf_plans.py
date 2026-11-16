"""The pure builders of the VRF capability (generators/capabilities/vrf.py, pbr.py) and of the SVIs
(generators/dcim/svi.py): the plans they return, as data."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from generators.capabilities.pbr import pbr_name, plan_pbr
from generators.capabilities.vrf import vrf_instance_spec
from generators.dcim.svi import plan_l3vni_svis, plan_svis
from generators.plan import EdgesSpec, Hfid, NodeRef, NodeSpec, PruneSpec
from generators.protocols import DcimVirtualInterface, ManagedPBR, ManagedVRF
from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_interface, raw_row, raw_svi


def _steering(*, steers: bool, pbr_ids: tuple[str, ...] = ()) -> Any:
    """The facts plan_pbr reads: the device, whether it steers, the PBRs it has."""
    return SimpleNamespace(name="leaf-1", steers=lambda: steers, pbr_ids=pbr_ids)


def test_a_device_that_steers_and_has_no_pbr_gets_a_shared_one_named_after_it() -> None:
    plan = plan_pbr(_steering(steers=True))

    (spec,) = plan.of(NodeSpec)
    assert spec == NodeSpec(
        "pbr",
        ManagedPBR,
        {"name": pbr_name("leaf-1"), "status": "active", "capabilities": [Hfid("DcimPhysicalDevice", ("leaf-1",))]},
        hfid=Hfid("ManagedPBR", ("leaf-1-pbr",)),
        ownership="shared",
    )
    assert not spec.tracked


def test_a_device_that_steers_nothing_loses_every_pbr_it_has() -> None:
    plan = plan_pbr(_steering(steers=False, pbr_ids=("pbr-1", "pbr-2")))

    assert plan.specs == [PruneSpec(ManagedPBR, scope={"ids": ["pbr-1", "pbr-2"]}, keep=frozenset(), key="id")]


def test_a_settled_device_has_an_empty_pbr_plan() -> None:
    assert not plan_pbr(_steering(steers=True, pbr_ids=("pbr-1",))).specs
    assert not plan_pbr(_steering(steers=False)).specs


def test_a_new_instance_is_created_with_its_first_member_in_one_shared_spec() -> None:
    facts = SimpleNamespace(name="leaf-1")

    spec = vrf_instance_spec(facts, "dc-1", "DC10", {"id": "ns-PROD", "name": "PROD"})  # ty: ignore[invalid-argument-type]

    assert spec.kind is ManagedVRF
    assert spec.hfid == Hfid("ManagedVRF", ("DC10", "PROD"))
    assert spec.data["deployment"] == {"id": "dc-1"} and spec.data["security_zone"] == {"id": "ns-PROD"}
    assert spec.data["shared_devices"] == [Hfid("DcimPhysicalDevice", ("leaf-1",))]
    assert spec.ownership == "shared" and not spec.tracked


def _leaf(**kwargs: Any) -> Any:
    return facts_of(raw_device("d1", "leaf-1", rows=[raw_row("r1", 100, "PROD")], **kwargs))


def test_a_routed_row_plans_its_svi_in_the_instance_and_its_row_edge_after_the_save() -> None:
    planned = plan_svis(_leaf(), {"PROD": "vrf-1"})

    node, edge = planned.plan.specs
    assert isinstance(node, NodeSpec) and node.kind is DcimVirtualInterface
    assert node.hfid == Hfid("DcimVirtualInterface", ("leaf-1", "Vlan100"))
    assert node.data["capabilities"] == [{"id": "vrf-1"}] and node.data["role"] == "svi"
    assert not node.tracked
    assert edge == EdgesSpec(NodeRef("svi-Vlan100"), "capabilities", ("r1",), retry_not_found=True)
    assert planned.taken == {}


def test_a_settled_device_has_an_empty_svi_plan() -> None:
    settled = _leaf(interfaces=[raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", row_id="r1")])

    planned = plan_svis(settled, {"PROD": "vrf-1"})

    assert not planned.plan.specs and not planned.changes


def test_an_svi_that_loses_its_vrf_is_planned_for_recreation_by_the_caller() -> None:
    """The delete before the write is the convergence's (the SVI is untracked, a ``replaces`` never deletes it)."""
    moved = _leaf(interfaces=[raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", row_id="r1")])

    planned = plan_svis(moved, {"PROD": "vrf-1"})
    default = facts_of(
        raw_device(
            "d1",
            "leaf-1",
            rows=[raw_row("r1", 100, "default")],
            interfaces=[raw_svi("svi-1", 100, "seg", "vrf-1", "PROD", row_id="r1")],
        )
    )
    recreated = plan_svis(default, {})

    assert not planned.changes.recreate_ids
    assert recreated.changes.recreate_ids == ("svi-1",)
    assert [s.hfid for s in recreated.plan.specs if isinstance(s, NodeSpec)] == [
        Hfid("DcimVirtualInterface", ("leaf-1", "Vlan100"))
    ]


def test_a_name_another_role_holds_is_skipped_and_reported() -> None:
    held = _leaf(interfaces=[raw_interface("i9", "Vlan100", "management")])

    planned = plan_svis(held, {"PROD": "vrf-1"})

    assert planned.taken == {"Vlan100": "management"}
    assert not planned.plan.specs


def test_an_l3vni_svi_is_planned_without_address_or_row_and_skips_a_taken_name() -> None:
    namespaces = {"PROD": {"name": "PROD", "l3_vni": 50001, "vrf_type": "prod"}}

    planned = plan_l3vni_svis(_leaf(), namespaces, {"PROD": "vrf-prod"})
    (node,) = planned.plan.specs
    assert isinstance(node, NodeSpec)
    assert node.data == {
        "name": "Vlan3900",
        "device": Hfid("DcimPhysicalDevice", ("leaf-1",)),
        "role": "l3vni",
        "status": "active",
        "capabilities": [{"id": "vrf-prod"}],
    }

    held = plan_l3vni_svis(
        _leaf(interfaces=[raw_interface("i9", "Vlan3900", "management")]), namespaces, {"PROD": "vrf-prod"}
    )
    assert held.taken == {"Vlan3900": "management"} and not held.plan.specs
