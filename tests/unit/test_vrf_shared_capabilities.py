"""ManagedVRF membership rides the shared device edge (``DcimCapabilities.shared_capabilities``).

``device_memberships`` is the one typename filter; the queries select the edge only inside ``... on DcimCapabilities``
(the field is not on ``DcimDevice``) and no query selects a device-level ``vrfs`` any more.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    SelectionSetNode,
    parse,
)

from tests.unit.vrf_instances_helpers import facts_of, raw_device, raw_instance
from transforms.helpers.vrf import device_vrfs
from utils.vrf_demand import device_memberships, device_vrf_facts

QUERIES = Path(__file__).resolve().parents[2] / "queries"
EDGE = "shared_capabilities"
CAPABILITIES = "DcimCapabilities"
#: parents that may still select a field named ``vrfs`` (a deployment's or a namespace's own instances)
VRFS_ALLOWED_PARENTS = {"TopologySegmentHosting", "IpamNamespace"}

PROD = {"name": "PROD", "l3_vni": 50000, "namespace_type": "prod"}
VRF = {"typename": "ManagedVRF", "namespace": PROD}
FOREIGN = {"typename": "ManagedSNMP"}


def _parse(path: Path) -> DocumentNode:
    return parse(path.read_text())


def _fragments(document: DocumentNode) -> dict[str, FragmentDefinitionNode]:
    return {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}


def _walk(
    selection_set: SelectionSetNode | None,
    fragments: dict[str, FragmentDefinitionNode],
    types: tuple[str, ...],
    seen: tuple[str, ...] = (),
):
    """Yield ``(field name, enclosing type conditions)`` for every field, spreads of in-file fragments expanded."""
    for selection in selection_set.selections if selection_set else ():
        if isinstance(selection, FieldNode):
            yield selection.name.value, types
            # a field's own type is unknown offline: nested conditions restart from the field
            yield from _walk(selection.selection_set, fragments, (), seen)
        elif isinstance(selection, InlineFragmentNode):
            condition = selection.type_condition.name.value if selection.type_condition else None
            yield from _walk(selection.selection_set, fragments, (*types, *([condition] if condition else [])), seen)
        elif isinstance(selection, FragmentSpreadNode) and selection.name.value in fragments:
            name = selection.name.value
            if name not in seen:
                fragment = fragments[name]
                yield from _walk(
                    fragment.selection_set, fragments, (*types, fragment.type_condition.name.value), (*seen, name)
                )


def _fields(path: Path) -> list[tuple[str, tuple[str, ...]]]:
    """Every field of the file's operations and fragment definitions, with its enclosing type conditions."""
    document = _parse(path)
    fragments = _fragments(document)
    found: list[tuple[str, tuple[str, ...]]] = []
    for definition in document.definitions:
        if isinstance(definition, FragmentDefinitionNode):
            types = (definition.type_condition.name.value,)
        else:
            types = ()
        found.extend(_walk(getattr(definition, "selection_set", None), fragments, types, ()))
    return found


def _edge_subtree_fields(path: Path) -> list[set[str]]:
    """For each ``shared_capabilities`` field of the file: every field name under it (spreads expanded)."""
    document = _parse(path)
    fragments = _fragments(document)
    found: list[set[str]] = []

    def visit(selection_set: SelectionSetNode | None, seen: tuple[str, ...]) -> None:
        for selection in selection_set.selections if selection_set else ():
            if isinstance(selection, FieldNode):
                if selection.name.value == EDGE:
                    found.append({name for name, _ in _walk(selection.selection_set, fragments, ())})
                visit(selection.selection_set, seen)
            elif isinstance(selection, InlineFragmentNode):
                visit(selection.selection_set, seen)
            elif (
                isinstance(selection, FragmentSpreadNode)
                and selection.name.value in fragments
                and selection.name.value not in seen
            ):
                visit(fragments[selection.name.value].selection_set, (*seen, selection.name.value))

    for definition in document.definitions:
        visit(getattr(definition, "selection_set", None), ())
    return found


def _all_queries() -> list[Path]:
    return sorted(QUERIES.rglob("*.gql"))


def _rel(path: Path) -> str:
    return str(path.relative_to(QUERIES))


def _spreaders(fragment: str) -> list[Path]:
    """The query files that spread ``fragment`` by name."""
    return [path for path in _all_queries() if f"...{fragment}" in path.read_text()]


# --- device_memberships / device_vrfs / device_vrf_facts ---------------------------------------------------------


def test_device_memberships_keeps_only_the_vrf_kind() -> None:
    """A foreign shared kind on the same edge is never a VRF."""
    assert device_memberships({EDGE: [FOREIGN, VRF, FOREIGN]}) == [VRF]


@pytest.mark.parametrize("node", [{}, {EDGE: None}, {EDGE: []}, {EDGE: [FOREIGN]}, {"vrfs": [VRF]}])
def test_device_memberships_is_empty_without_a_vrf_on_the_shared_edge(node: dict[str, Any]) -> None:
    """No edge, a null edge, an empty one, only foreign kinds, or the old device-level ``vrfs``: no membership."""
    assert device_memberships(node) == []


def test_device_vrfs_ignores_a_foreign_shared_kind() -> None:
    """The renderers get only the namespaces of the VRF memberships."""
    assert device_vrfs({EDGE: [FOREIGN, VRF]}) == [PROD]
    assert device_vrfs({EDGE: [FOREIGN]}) == []


def test_device_vrfs_skips_a_vrf_without_a_namespace() -> None:
    """A membership with no namespace has nothing to render."""
    assert device_vrfs({EDGE: [{"typename": "ManagedVRF", "namespace": None}]}) == []


def test_device_vrf_facts_ignores_a_foreign_shared_kind() -> None:
    """Only the ManagedVRF entries become instances; the foreign kind adds none."""
    node = {"id": "d1", "name": "leaf-1", EDGE: [FOREIGN, {**VRF, "id": "v1", "name": "dc1-PROD"}]}
    assert [instance.id for instance in device_vrf_facts(node).instances] == ["v1"]
    assert device_vrf_facts({"id": "d1", "name": "x", EDGE: [FOREIGN]}).instances == ()


def test_raw_device_with_a_foreign_shared_kind_has_the_same_facts() -> None:
    """The raw GraphQL shape run through clean_data: a foreign ``__typename`` on the edge changes nothing."""
    plain = facts_of(raw_device("d1", "leaf-1", vrfs=[raw_instance("v1", "x", "PROD")]))
    mixed = facts_of(raw_device("d1", "leaf-1", vrfs=[raw_instance("v1", "x", "PROD")], shared_others=["ManagedSNMP"]))
    assert mixed.instances == plain.instances
    assert len(mixed.instances) == 1


# --- query shape ---------------------------------------------------------------------------------------------------


def test_every_shared_capabilities_selection_is_inside_dcim_capabilities() -> None:
    """The edge is on ``DcimCapabilities``, not on ``DcimDevice``: it is selected under ``... on DcimCapabilities``."""
    offenders = []
    selecting = []
    for path in _all_queries():
        for name, types in _fields(path):
            if name == EDGE:
                selecting.append(_rel(path))
                if CAPABILITIES not in types:
                    offenders.append((_rel(path), types))
    assert offenders == []
    assert {"fragments/device_capabilities.gql", "validation/evpn_fabric.gql", "validation/vrf_instances.gql"} <= set(
        selecting
    )


@pytest.mark.parametrize(
    "relative", ["fragments/device_capabilities.gql", "validation/evpn_fabric.gql", "validation/vrf_instances.gql"]
)
def test_the_three_membership_queries_select_the_edge_with_a_typename(relative: str) -> None:
    """Each reads the edge and the node's ``__typename`` (the filter of ``device_memberships``)."""
    names = _edge_subtree_fields(QUERIES / relative)
    assert names, "the edge must be selected"
    assert all("__typename" in fields for fields in names)


def test_no_query_selects_a_device_level_vrfs_field() -> None:
    """``DcimDevice.vrfs`` is gone; the remaining ``vrfs`` live on a deployment or a namespace."""
    offenders = []
    for path in _all_queries():
        for name, types in _fields(path):
            if name == "vrfs" and not (set(types) & VRFS_ALLOWED_PARENTS):
                offenders.append((_rel(path), types))
    assert offenders == []


def test_vrf_instances_query_selects_shared_devices_on_the_root() -> None:
    """The ManagedVRF root lists its members as ``shared_devices``; the old ``devices`` is not selected on it."""
    text = (QUERIES / "validation" / "vrf_instances.gql").read_text()
    assert "shared_devices" in text
    assert "devices {" not in text.replace("shared_devices {", "")


# --- audit: every query spreading DeviceCapabilitiesOnDeviceFields -----------------------------------------------

SPREADERS = _spreaders("DeviceCapabilitiesOnDeviceFields")


def test_the_audit_finds_the_known_spreaders() -> None:
    """Eight config queries spread the device fragment (the ninth file is the fragment's own definition)."""
    names = {_rel(path) for path in SPREADERS}
    assert len(names) == 8
    assert all(name.startswith("config/") for name in names)


@pytest.mark.parametrize("path", SPREADERS, ids=_rel)
def test_a_query_spreading_the_device_fragment_selects_the_edge_only_inside_dcim_capabilities(path: Path) -> None:
    """Resolving the spread (in-file fragments, plus the shared fragment files) never exposes the edge at device level."""
    fragments: dict[str, FragmentDefinitionNode] = {}
    for other in _all_queries():
        fragments.update(_fragments(_parse(other)))
    document = _parse(path)
    found: list[tuple[str, ...]] = []
    for definition in document.definitions:
        for name, types in _walk(getattr(definition, "selection_set", None), fragments, (), ()):
            if name == EDGE:
                found.append(types)
    assert found, "the spread must reach the shared_capabilities selection"
    assert all(CAPABILITIES in types for types in found)
