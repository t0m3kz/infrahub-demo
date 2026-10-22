"""The virtual-circuit queries select every field the code and the fakes read.

Unit tests feed canned payloads, so a field the code reads but the query does not select (or a fake that invents a
field no query returns) passes here and fails only on a real stack. These tests parse the two ``.gql`` files with
graphql-core, flatten them to the field paths the cleaned data carries (``edges``/``node``/``value`` wrappers
dropped, inline fragments merged) and compare them with what the generator, the check and the fakes use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from graphql import (
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    parse,
)

from tests.unit.simulators.circuit_fakes import virtual_payload

_QUERIES = Path(__file__).parents[2] / "queries"
_WRAPPERS = frozenset({"edges", "node"})


def _selected_paths(path: Path, root: str) -> set[str]:
    """Dotted paths of every field selected under the top-level ``root`` field of the query in ``path``.

    A field selecting only ``value`` is a leaf (``status { value }`` is ``status``); ``edges``/``node`` are dropped.
    """
    document = parse(path.read_text())
    fragments = {d.name.value: d for d in document.definitions if isinstance(d, FragmentDefinitionNode)}
    operation = next(d for d in document.definitions if isinstance(d, OperationDefinitionNode))
    paths: set[str] = set()

    def walk(selections: Any, prefix: tuple[str, ...]) -> None:
        for selection in selections:
            if isinstance(selection, InlineFragmentNode):
                walk(selection.selection_set.selections, prefix)
            elif isinstance(selection, FragmentSpreadNode):
                walk(fragments[selection.name.value].selection_set.selections, prefix)
            elif isinstance(selection, FieldNode):
                name = selection.name.value
                children = selection.selection_set.selections if selection.selection_set else []
                if name in _WRAPPERS:
                    walk(children, prefix)
                    continue
                here = (*prefix, name)
                only_value = bool(children) and all(
                    isinstance(c, FieldNode) and c.name.value == "value" for c in children
                )
                if not children or only_value:
                    paths.add(".".join(here))
                else:
                    paths.add(".".join(here))
                    walk(children, here)

    top = next(s for s in operation.selection_set.selections if isinstance(s, FieldNode) and s.name.value == root)
    assert top.selection_set is not None
    walk(top.selection_set.selections, ())
    return paths


def _payload_paths(value: Any, prefix: tuple[str, ...] = ()) -> set[str]:
    """Dotted paths of the leaves of a raw (uncleaned) GraphQL payload, wrappers dropped like the query paths."""
    paths: set[str] = set()
    if isinstance(value, dict):
        if set(value) == {"value"}:
            return {".".join(prefix)}
        for key, child in value.items():
            if key in _WRAPPERS:
                paths |= _payload_paths(child, prefix)
            else:
                paths |= _payload_paths(child, (*prefix, key))
    elif isinstance(value, list):
        for child in value:
            paths |= _payload_paths(child, prefix)
    elif prefix:
        paths.add(".".join(prefix))
    return paths


@pytest.fixture(scope="module")
def generator_paths() -> set[str]:
    """Paths selected by queries/topology/add/virtual_circuit.gql."""
    return _selected_paths(_QUERIES / "topology" / "add" / "virtual_circuit.gql", "TopologyVirtualCircuit")


@pytest.fixture(scope="module")
def check_paths() -> set[str]:
    """Paths selected by queries/validation/circuit_placement.gql for the virtual circuits."""
    return _selected_paths(_QUERIES / "validation" / "circuit_placement.gql", "TopologyVirtualCircuit")


# What generators/topology/circuit.py reads from the cleaned virtual circuit (utils/circuit_underlay.py included).
_GENERATOR_READS = [
    "id",
    "name",
    "status",
    "link_type",
    "peering_role",
    "capabilities.id",
    "capabilities.name",
    "capabilities.__typename",
    "capabilities.ip_address.id",
    "capabilities.ip_address.address",
    "capabilities.device.id",
    "capabilities.device.name",
    "capabilities.device.deployment.id",
    "capabilities.device.deployment.__typename",
    "capabilities.device.capabilities.id",
    "capabilities.device.capabilities.__typename",
    "capabilities.device.capabilities.name",
    "capabilities.device.capabilities.process_role",
    # the derived underlay: the physical circuits on each port
    "capabilities.capabilities.__typename",
    "capabilities.capabilities.id",
    "capabilities.capabilities.name",
    "capabilities.capabilities.circuit_id",
    "capabilities.capabilities.status",
]

# What checks/circuit_placement.py reads from the cleaned virtual circuit.
_CHECK_READS = [
    "id",
    "name",
    "link_type",
    "transport_mode",
    "owner.id",
    "owner.org_id",
    "locations.id",
    "locations.__typename",
    "locations.name",
    "locations.owner.id",
    "locations.environment",
    "locations.parent.id",
    "capabilities.id",
    "capabilities.name",
    "capabilities.device.id",
    "capabilities.device.name",
    "capabilities.device.deployment.id",
    "capabilities.device.deployment.__typename",
    "capabilities.device.deployment.name",
    "capabilities.device.deployment.parent.id",
    # the derived underlay: the physical circuits on each port, with their ends
    "capabilities.capabilities.__typename",
    "capabilities.capabilities.id",
    "capabilities.capabilities.name",
    "capabilities.capabilities.status",
    "capabilities.capabilities.locations.id",
    "capabilities.capabilities.locations.__typename",
    "capabilities.capabilities.locations.name",
    "capabilities.capabilities.locations.parent.id",
]


@pytest.mark.parametrize("path", _GENERATOR_READS)
def test_the_generator_query_selects_what_the_generator_reads(path: str, generator_paths: set[str]) -> None:
    """Every path in `_GENERATOR_READS` is selected by virtual_circuit.gql."""
    assert path in generator_paths


@pytest.mark.parametrize("path", _CHECK_READS)
def test_the_check_query_selects_what_the_check_reads(path: str, check_paths: set[str]) -> None:
    """Every path in `_CHECK_READS` is selected by circuit_placement.gql."""
    assert path in check_paths


def test_the_fakes_return_only_selected_fields(generator_paths: set[str]) -> None:
    """A fake payload path the query does not select is a field no real response carries."""
    node = virtual_payload()["TopologyVirtualCircuit"]["edges"][0]["node"]
    fake_paths = {path.replace(".typename", ".__typename") for path in _payload_paths(node)}
    unselected = {p for p in fake_paths if p not in generator_paths and not p.startswith(("vni", "tunnel_id"))}
    assert not unselected, f"fake payload fields the query does not select: {sorted(unselected)}"


def test_the_decommissioned_filter_has_a_status_to_read(generator_paths: set[str], check_paths: set[str]) -> None:
    """Both consumers drop decommissioned circuits, so both queries select the derived circuit's status."""
    assert "capabilities.capabilities.status" in generator_paths
    assert "capabilities.capabilities.status" in check_paths
