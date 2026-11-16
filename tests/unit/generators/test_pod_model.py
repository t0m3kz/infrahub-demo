"""Unit tests for PodFabricModel (generators/model/pod_model.py)."""

from __future__ import annotations

from typing import Any

import pytest

from generators.model.errors import FabricModelError
from generators.model.pod_model import PodFabricModel
from tests.unit.fabric_model_helpers import cleaned
from tests.unit.simulators.fabric_designs import dc_design, pod_design

_SPINE_TEMPLATE = {
    "id": "tpl-spine",
    "template_kind": "TemplateDcimPhysicalDevice",
    "interfaces": [
        {"name": "Eth1/1", "role": "uplink"},
        {"name": "Eth1/2", "role": "uplink"},
        {"name": "Eth1/10", "role": "downlink"},
        {"name": "Eth1/11", "role": "downlink"},
    ],
}


def _compile(pod: str = "S_MIXED", dc: str = "S", **overrides: Any) -> PodFabricModel:
    """Compile a pod through the raw GraphQL shape and clean_data(), as the generator receives it."""
    parent: dict[str, Any] = {
        "id": "dc-1",
        "name": "DC1",
        "index": 2,
        "design": dc_design(dc),
        "children": [{"id": "pod-1", "name": "POD1", "index": 1, "design": {"id": "x"}}],
    }
    parent.update(overrides.pop("parent", {}))
    node: dict[str, Any] = {
        "id": "pod-1",
        "name": "POD1",
        "index": 1,
        "design": pod_design(pod),
        "fabric_templates": [{"role": "spine", "quantity": 2, "template": _SPINE_TEMPLATE}],
        "parent": parent,
        **overrides,
    }
    return PodFabricModel.compile(cleaned("TopologyPod", node))


def test_identity_and_deployment_type_come_from_the_payload() -> None:
    model = _compile("S_MIXED")

    assert (model.pod_id, model.pod_name, model.pod_index) == ("pod-1", "POD1", 1)
    assert (model.dc_id, model.dc_name, model.dc_index) == ("dc-1", "DC1", 2)
    assert model.deployment_type == "mixed"
    assert model.naming_convention == "standard"
    assert model.underlay_protocol == "ipv6"


@pytest.mark.parametrize(("pod", "deployment_type"), [("S_MIDDLE", "middle_rack"), ("S_TOR", "tor")])
def test_deployment_type_follows_the_pod_design(pod: str, deployment_type: str) -> None:
    assert _compile(pod).deployment_type == deployment_type


def test_spine_slot_is_the_spine_role_with_its_interfaces() -> None:
    model = _compile()

    assert model.spine_role == "spine"
    assert model.spine_uplink_interfaces == ("Eth1/1", "Eth1/2")
    assert model.spine_downlink_interfaces == ("Eth1/10", "Eth1/11")
    assert [entry["quantity"] for entry in model.spine_entries] == [2]


def test_border_spine_entries_fill_the_spine_slot() -> None:
    """A micro-fabric pod declares border-spines instead of spines; role and count follow."""
    model = _compile(fabric_templates=[{"role": "border-spine", "quantity": 3, "template": _SPINE_TEMPLATE}])

    assert model.spine_role == "border-spine"
    assert [entry["quantity"] for entry in model.spine_entries] == [3]


@pytest.mark.parametrize(
    ("underlay", "expected"),
    [
        ("ipv6", {"technical": 119, "loopback": 122}),
        ("dual_stack", {"technical": 119, "loopback": 26}),
        ("ipv4", {"technical": 23, "loopback": 26}),
    ],
)
def test_pool_sizes_follow_the_pod_envelope_in_the_family_of_the_underlay(
    underlay: str, expected: dict[str, int]
) -> None:
    """dual_stack: IPv6 technical, IPv4 loopback."""
    model = _compile(parent={"underlay_protocol": underlay})

    assert model.pools_to_allocate == expected


def test_a_bigger_pod_design_takes_a_bigger_pool() -> None:
    """Fewer host bits means a shorter prefix."""
    assert (
        _compile("S_MIXED", "M").pools_to_allocate["technical"]
        < _compile("S_MIDDLE", "M").pools_to_allocate["technical"]
    )


def test_super_spine_offset_strides_by_the_widest_pod_design() -> None:
    """Every pod owns the widest pod design's spine count of super-spine ports, so pod N starts at (N - 1) * width."""
    first = _compile()
    second = _compile(
        index=2,
        parent={"children": [{"id": "pod-1", "name": "POD1", "index": 1, "design": {"id": "x"}}]},
    )
    widest = max(template["max_spines_per_pod"] for template in dc_design("S")["pod_templates"])

    assert first.super_spine_offset == 0
    assert second.super_spine_offset == widest


def test_siblings_are_the_dcs_children() -> None:
    model = _compile(
        parent={
            "children": [
                {"id": "pod-1", "name": "POD1", "index": 1, "design": {"id": "x"}},
                {"id": "pod-2", "name": "POD2", "index": 2, "design": {"id": "y"}},
            ]
        }
    )

    assert [sibling["id"] for sibling in model.siblings] == ["pod-1", "pod-2"]


def test_a_pod_without_spine_entries_is_refused() -> None:
    with pytest.raises(FabricModelError, match="no spine/border-spine fabric_templates entries"):
        _compile(fabric_templates=[])


def test_zero_quantity_spine_entries_do_not_count_as_spines() -> None:
    with pytest.raises(FabricModelError, match="no spine/border-spine"):
        _compile(fabric_templates=[{"role": "spine", "quantity": 0, "template": _SPINE_TEMPLATE}])


def test_more_spines_than_the_pod_design_allows_is_refused() -> None:
    entries = [{"role": "spine", "quantity": 99, "template": _SPINE_TEMPLATE}]

    with pytest.raises(FabricModelError, match=r"requests 99 spines but pod design 'S_MIXED' allows at most 4"):
        _compile(fabric_templates=entries)


def test_a_pod_design_the_dc_design_does_not_list_is_refused() -> None:
    """S hosts S_* and M_* designs only: a pod with an unlisted design is outside the envelope."""
    design = pod_design("S_MIXED")
    design["name"] = "ODD"

    with pytest.raises(FabricModelError, match=r"design 'ODD' is not one of DC DC1's design 'S' pod_templates"):
        _compile(design=design)


def test_more_pods_than_the_dc_design_allows_is_refused() -> None:
    children = [{"id": f"pod-{n}", "name": f"POD{n}", "index": n, "design": {"id": "x"}} for n in (1, 2, 3)]

    with pytest.raises(FabricModelError, match=r"DC DC1 holds 3 pods but its design 'S' allows at most 2"):
        _compile(parent={"children": children})


def test_the_pod_itself_is_counted_once_among_the_children() -> None:
    """A re-run sees itself among the children: two pods in a two-pod DC pass."""
    children = [{"id": f"pod-{n}", "name": f"POD{n}", "index": n, "design": {"id": "x"}} for n in (1, 2)]

    assert _compile(parent={"children": children}).pod_id == "pod-1"


def test_a_spine_template_without_uplinks_is_refused() -> None:
    template = {**_SPINE_TEMPLATE, "interfaces": [{"name": "Eth1/10", "role": "downlink"}]}

    with pytest.raises(FabricModelError, match="No uplink interfaces found in spine template"):
        _compile(fabric_templates=[{"role": "spine", "quantity": 2, "template": template}])


def test_the_model_error_is_a_value_error() -> None:
    """A caller that catches ValueError around the payload read catches it too."""
    assert issubclass(FabricModelError, ValueError)


@pytest.mark.parametrize("missing", ["id", "name", "index", "design", "parent"])
def test_a_payload_without_a_required_field_raises_key_error(missing: str) -> None:
    node = cleaned(
        "TopologyPod",
        {
            "id": "pod-1",
            "name": "POD1",
            "index": 1,
            "design": pod_design("S_MIXED"),
            "fabric_templates": [{"role": "spine", "quantity": 2, "template": _SPINE_TEMPLATE}],
            "parent": {"id": "dc-1", "name": "DC1", "index": 1, "design": dc_design("S"), "children": []},
        },
    )
    del node[missing]

    with pytest.raises(KeyError):
        PodFabricModel.compile(node)
