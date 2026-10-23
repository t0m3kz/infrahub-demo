"""The virtual-circuit session query selects every field the generator reads.

Unit tests feed canned payloads, so a field the code reads but the query does not select passes there and fails only
on a real stack. These tests parse ``queries/topology/add/virtual_circuit.gql`` with graphql-core, flatten it to the
field paths the cleaned data carries (``edges``/``node``/``value`` wrappers dropped, inline fragments and fragment
spreads merged) and compare them with what the session generator uses. The helpers are shared with
``test_circuit_check_query_shapes.py``, which does the same for the placement check's query.
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

from tests.unit.simulators.circuit_fakes import (
    colo_edge_port,
    dc_border_port,
    handoff_edge_port,
    handoff_party_port,
    handoff_payload,
    interconnect_payload,
    tunnel_payload,
)

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


# What generators/topology/virtual_circuit.py reads from the cleaned virtual circuit, for every kind
# (utils/circuit_underlay.py included). `__typename` is `typename` once cleaned.
_GENERATOR_READS = [
    "id",
    "__typename",
    "name",
    "status",
    "capabilities.id",
    "capabilities.name",
    "capabilities.__typename",
    "capabilities.device.id",
    "capabilities.device.name",
    "capabilities.device.deployment.__typename",
    "capabilities.device.deployment.name",
    "capabilities.device.deployment.underlay_protocol",
    "capabilities.device.capabilities.id",
    "capabilities.device.capabilities.__typename",
    "capabilities.device.capabilities.name",
    "capabilities.device.capabilities.process_role",
    # the DCI EVPN session sources its loopbacks
    "capabilities.device.interfaces.id",
    "capabilities.device.interfaces.__typename",
    "capabilities.device.interfaces.name",
    "capabilities.device.interfaces.role",
    "capabilities.device.interfaces.ip_address.address",
    # the derived underlay: the physical circuits on each port ...
    "capabilities.capabilities.__typename",
    "capabilities.capabilities.id",
    "capabilities.capabilities.name",
    "capabilities.capabilities.circuit_id",
    "capabilities.capabilities.status",
    # ... and, for a sub-interface, on its parent interface
    "capabilities.parent_interface.capabilities.__typename",
    "capabilities.parent_interface.capabilities.id",
    "capabilities.parent_interface.capabilities.name",
    "capabilities.parent_interface.capabilities.circuit_id",
    "capabilities.parent_interface.capabilities.status",
    # a hop inside one facility is a cable, not a circuit: on a physical port, or on a sub-interface's parent
    "capabilities.cable.id",
    "capabilities.parent_interface.cable.id",
    # TopologyHandoffCircuit
    "ip_namespace.name",
    "maximum_routes",
    "password.id",
]

# Fields the redesign removed from the virtual circuit (and the physical one): selecting one fails on a real stack.
_REMOVED_FIELDS = [
    "link_type",
    "transport_mode",
    "peering_role",
    "vni",
    "tunnel_id",
    "encryption",
    "bandwidth",
    "cloud_endpoints",
    # read by no session generator: the ends' addresses are allocated, the cloud side is data
    "cloud_endpoint",
    "owner",
]


def _cabled_handoff() -> dict[str, Any]:
    """A handoff whose edge has no circuit, only a cable on the parent of its sub-interface."""
    return handoff_payload(ends=[handoff_edge_port(parent_circuits=[], parent_cabled=True), handoff_party_port()])


def _cabled_interconnect() -> dict[str, Any]:
    """A DCI whose ends are cabled physical ports."""
    return interconnect_payload(ends=[dc_border_port(cabled=True), colo_edge_port(cabled=True)])


@pytest.mark.parametrize("path", _GENERATOR_READS)
def test_the_generator_query_selects_what_the_generator_reads(path: str, generator_paths: set[str]) -> None:
    """Every path in `_GENERATOR_READS` is selected by virtual_circuit.gql."""
    assert path in generator_paths


@pytest.mark.parametrize("field", _REMOVED_FIELDS)
def test_the_generator_query_selects_no_removed_field(field: str, generator_paths: set[str]) -> None:
    """No path of virtual_circuit.gql ends in a field of the old virtual circuit."""
    assert not {p for p in generator_paths if p.split(".")[-1] == field}


def test_every_selected_field_is_read(generator_paths: set[str]) -> None:
    """A leaf the query selects that the generator does not read is dead weight (parents of a read path are fine)."""
    parents = {".".join(path.split(".")[:i]) for path in _GENERATOR_READS for i in range(1, len(path.split(".")))}
    unread = generator_paths - set(_GENERATOR_READS) - parents
    assert not unread, f"selected but not read: {sorted(unread)}"


def _fake_paths(payload: dict[str, Any]) -> set[str]:
    """Paths of one fake circuit payload (raw GraphQL), in the query's spelling."""
    return _payload_paths(payload["TopologyVirtualCircuit"]["edges"][0]["node"])


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(interconnect_payload(), id="interconnect"),
        pytest.param(handoff_payload(password="pw-1"), id="handoff"),
        pytest.param(tunnel_payload(), id="tunnel"),
        pytest.param(_cabled_handoff(), id="handoff-cabled-parent"),
        pytest.param(_cabled_interconnect(), id="interconnect-cabled-port"),
    ],
)
def test_the_fakes_return_only_selected_fields(payload: dict[str, Any], generator_paths: set[str]) -> None:
    """A fake payload path the query does not select is a field no real response carries."""
    unselected = _fake_paths(payload) - generator_paths
    assert not unselected, f"fake payload fields the query does not select: {sorted(unselected)}"


def test_the_fakes_carry_every_field_the_generator_reads() -> None:
    """A field the generator reads that no fake carries is read nowhere in the unit tests."""
    carried = (
        _fake_paths(interconnect_payload())
        | _fake_paths(handoff_payload(password="pw-1"))
        | _fake_paths(_cabled_handoff())
        | _fake_paths(_cabled_interconnect())
    )
    assert not set(_GENERATOR_READS) - carried, f"read but never faked: {sorted(set(_GENERATOR_READS) - carried)}"


def test_the_cable_is_selected_on_the_port_and_on_its_parent(generator_paths: set[str]) -> None:
    """A cable-only handoff has no circuit: the carrier is the cable on the physical port or on the parent."""
    assert {"capabilities.cable.id", "capabilities.parent_interface.cable.id"} <= generator_paths


def test_the_decommissioned_filter_has_a_status_to_read(generator_paths: set[str]) -> None:
    """The generator drops decommissioned carriers, so the query selects the carrier's status, on a port and its parent."""
    assert "capabilities.capabilities.status" in generator_paths
    assert "capabilities.parent_interface.capabilities.status" in generator_paths
