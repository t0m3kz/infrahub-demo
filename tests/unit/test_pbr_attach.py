"""The plan of the PBR attachments of a device's SVIs (generators/helpers/pbr_attach.py)."""

from __future__ import annotations

from generators.helpers.pbr_attach import PbrPlan, PbrPolicyFact, plan_pbr_attachments, policy_name


def _policy(
    policy_id: str,
    footprint: str | None,
    attached: tuple[str, ...] = (),
    *,
    origin: str = "generated",
    kind: str = "pbr",
) -> PbrPolicyFact:
    return PbrPolicyFact(policy_id, kind, origin, footprint, frozenset(attached))


def test_the_name_of_a_policy_follows_its_footprint() -> None:
    """One generated policy per footprint, named after it."""
    assert policy_name("C005-P-DC10") == "pbr-c005-p-dc10"


def test_a_new_svi_is_added_and_a_missing_policy_is_created() -> None:
    """The first SVI of a footprint creates its policy and is attached to it."""
    plan = plan_pbr_attachments({"svi-1": "fp-a"}, [], ["svi-1"])
    assert plan == PbrPlan(create=("fp-a",), add={"fp-a": ("svi-1",)})


def test_an_existing_svi_is_attached_too_not_only_a_fresh_one() -> None:
    """An SVI that has been there for a while but is on no policy is attached by the next run."""
    plan = plan_pbr_attachments({"svi-1": "fp-a"}, [_policy("p1", "fp-a")], ["svi-1"])
    assert plan == PbrPlan(add={"fp-a": ("svi-1",)})


def test_everything_in_place_writes_nothing() -> None:
    """Idempotent: attached SVIs of the right footprint give an empty plan."""
    plan = plan_pbr_attachments(
        {"svi-1": "fp-a", "svi-2": "fp-a"}, [_policy("p1", "fp-a", ("svi-1", "svi-2"))], ["svi-1", "svi-2"]
    )
    assert not plan


def test_an_svi_recreated_by_another_flow_is_attached_again() -> None:
    """A recreated SVI has a new id; the old edge is gone with the old SVI, so the new id is not attached."""
    plan = plan_pbr_attachments({"svi-new": "fp-a"}, [_policy("p1", "fp-a", ())], ["svi-new"])
    assert plan == PbrPlan(add={"fp-a": ("svi-new",)})


def test_an_svi_that_moved_to_another_footprint_leaves_the_old_policy() -> None:
    """The row moved: detached from the old footprint's policy, attached to the new one."""
    old, new = _policy("p-old", "fp-a", ("svi-1",)), _policy("p-new", "fp-b")
    plan = plan_pbr_attachments({"svi-1": "fp-b"}, [old, new], ["svi-1"])
    assert plan == PbrPlan(add={"fp-b": ("svi-1",)}, remove={"p-old": ("svi-1",)})


def test_an_svi_that_is_no_longer_steered_is_detached() -> None:
    """The segment left the footprint (or the footprint lost its context): nothing to steer."""
    plan = plan_pbr_attachments({"svi-1": None}, [_policy("p1", "fp-a", ("svi-1",))], ["svi-1"])
    assert plan == PbrPlan(remove={"p1": ("svi-1",)})


def test_an_svi_that_is_gone_is_detached_when_it_is_still_owned() -> None:
    """An SVI the device no longer needs but still holds is detached; one of another device is never touched."""
    policy = _policy("p1", "fp-a", ("svi-1", "svi-other-device"))
    plan = plan_pbr_attachments({}, [policy], ["svi-1"])
    assert plan == PbrPlan(remove={"p1": ("svi-1",)})


def test_a_manual_policy_is_never_changed() -> None:
    """A person's pbr policy keeps its attachments, whatever the footprint says; the generator adds its own."""
    manual = _policy("p-manual", "fp-a", ("svi-1",), origin="manual")
    plan = plan_pbr_attachments({"svi-1": "fp-b"}, [manual], ["svi-1"])
    assert plan == PbrPlan(create=("fp-b",), add={"fp-b": ("svi-1",)})
    assert not plan_pbr_attachments({"svi-1": None}, [manual], ["svi-1"])


def test_a_bgp_policy_is_not_a_pbr_policy() -> None:
    """Only kind pbr counts; a bgp policy never holds an SVI of ours."""
    assert not plan_pbr_attachments({}, [_policy("p1", "fp-a", ("svi-1",), kind="bgp")], ["svi-1"])


def test_a_pbr_policy_without_a_footprint_is_left_alone() -> None:
    """No customer deployment: not ours to maintain."""
    assert not plan_pbr_attachments({"svi-1": None}, [_policy("p1", None, ("svi-1",))], ["svi-1"])


def test_two_svis_of_one_footprint_create_the_policy_once() -> None:
    """The create list holds a footprint once."""
    plan = plan_pbr_attachments({"svi-1": "fp-a", "svi-2": "fp-a"}, [], ["svi-1", "svi-2"])
    assert plan.create == ("fp-a",) and plan.add == {"fp-a": ("svi-1", "svi-2")}


def test_an_svi_the_caller_does_not_own_is_ignored() -> None:
    """The plan only covers the interfaces the caller reconciles."""
    assert not plan_pbr_attachments({"svi-x": "fp-a"}, [], ["svi-1"])


def test_one_interface_can_be_steered_for_several_footprints() -> None:
    """The L3 VNI SVI of a VRF is shared: each footprint's policy is attached to it."""
    plan = plan_pbr_attachments({"l3": {"fp-a", "fp-b"}}, [_policy("p-a", "fp-a")], ["l3"])
    assert plan == PbrPlan(create=("fp-b",), add={"fp-a": ("l3",), "fp-b": ("l3",)})


def test_a_footprint_that_left_the_shared_interface_is_detached_and_the_others_stay() -> None:
    """Only the policy of the footprint that is no longer served by the device loses the interface."""
    both = [_policy("p-a", "fp-a", ("l3",)), _policy("p-b", "fp-b", ("l3",))]
    assert plan_pbr_attachments({"l3": {"fp-a"}}, both, ["l3"]) == PbrPlan(remove={"p-b": ("l3",)})
