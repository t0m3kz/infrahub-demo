"""The session circuit query selects every field the generator reads."""

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
from utils.graphql_queries import load_query

_QUERIES = Path(__file__).parents[3] / "queries"
_WRAPPERS = frozenset({"edges", "node"})
# The encapsulation of a circuit end (queries/fragments/interface_encapsulation.gql): selected under this alias.
ENCAPSULATION = "capabilities.encapsulation_capabilities"


def _selected_paths(path: Path, root: str, by_alias: bool = False) -> set[str]:
    """Dotted paths of every field selected under the top-level ``root`` field of the query in ``path``."""
    document = parse(load_query(path))
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
                # An aliased selection is keyed by its alias, like the response (`encapsulation_capabilities: capabilities`).
                name = (selection.alias if by_alias and selection.alias else selection.name).value
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

    top = next(
        s for s in operation.selection_set.selections if isinstance(s, FieldNode) and (s.alias or s.name).value == root
    )
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
    return _selected_paths(
        _QUERIES / "topology" / "add" / "virtual_circuit.gql", "TopologySessionCircuit", by_alias=True
    )


# What generators/connectivity/virtual_circuit.py reads from the cleaned virtual circuit, for every kind
# (utils/circuit_underlay.py included).
_GENERATOR_READS = [
    "id",
    "__typename",
    "name",
    "status",
    "circuit_type",
    "capabilities.id",
    "capabilities.name",
    "capabilities.__typename",
    "capabilities.device.id",
    "capabilities.device.name",
    # a cloud transport device is a session end the generator leaves authored
    "capabilities.device.__typename",
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
    # the derived underlay: the carrier circuits on each port ...
    "capabilities.capabilities.__typename",
    "capabilities.capabilities.id",
    "capabilities.capabilities.name",
    "capabilities.capabilities.circuit_id",
    "capabilities.capabilities.status",
    # a handoff end is a sub-interface: its encapsulation names the parent it rides on and the tag (vlan_id)
    f"{ENCAPSULATION}.__typename",
    f"{ENCAPSULATION}.parent.id",
    f"{ENCAPSULATION}.vlan_id",
    # ... and, for a sub-interface, on the carrier circuits of its parent interface
    f"{ENCAPSULATION}.parent.capabilities.__typename",
    f"{ENCAPSULATION}.parent.capabilities.id",
    f"{ENCAPSULATION}.parent.capabilities.name",
    f"{ENCAPSULATION}.parent.capabilities.circuit_id",
    f"{ENCAPSULATION}.parent.capabilities.status",
    # a hop inside one facility is a cable, not a circuit: on a physical port, or on a sub-interface's parent
    "capabilities.cable.id",
    f"{ENCAPSULATION}.parent.cable.id",
    # a handoff
    "vrf.id",
    "vrf.deployment.id",
    "vrf.deployment.name",
    *(f"vrf.security_zone.{field}" for field in ("id", "name", "vrf_type", "l3_vni")),
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
    "ip_namespace",
    "vrf_name",
]


def _cabled_handoff() -> dict[str, Any]:
    """A handoff whose edge has no circuit, only a cable on the parent of its sub-interface."""
    return handoff_payload(ends=[handoff_edge_port(parent_circuits=[], parent_cabled=True), handoff_party_port()])


def _cabled_interconnect() -> dict[str, Any]:
    """A DCI whose ends are cabled physical ports."""
    return interconnect_payload(ends=[dc_border_port(cabled=True), colo_edge_port(cabled=True)])


def _vrf_owned_handoff() -> dict[str, Any]:
    """The handoff owns a local VRF; its loopback projects only the address SessionEnd.parse reads."""
    zone = {
        "id": "zone-prod",
        "name": {"value": "PROD"},
        "vrf_type": {"value": "prod"},
        "l3_vni": {"value": 60000},
    }
    loopback = {
        "id": "tenant-loopback",
        "__typename": "DcimVirtualInterface",
        "name": {"value": "loopback10"},
        "role": {"value": "loopback"},
        "ip_address": {"node": {"address": {"value": "10.255.0.1/32"}}},
    }
    payload = handoff_payload(ends=[handoff_edge_port(loopbacks=[loopback]), handoff_party_port()])
    node = payload["TopologySessionCircuit"]["edges"][0]["node"]
    node.pop("security_zone", None)
    node["vrf"] = {
        "node": {
            "id": "vrf-fr-prod",
            "deployment": {"node": {"id": "fr", "name": {"value": "FR"}}},
            "security_zone": {"node": zone},
        }
    }
    return payload


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
    return _payload_paths(payload["TopologySessionCircuit"]["edges"][0]["node"])


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(interconnect_payload(), id="interconnect"),
        pytest.param(handoff_payload(password="pw-1"), id="handoff"),
        pytest.param(tunnel_payload(), id="tunnel"),
        pytest.param(_cabled_handoff(), id="handoff-cabled-parent"),
        pytest.param(_cabled_interconnect(), id="interconnect-cabled-port"),
        pytest.param(_vrf_owned_handoff(), id="handoff-vrf-owned"),
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
        | _fake_paths(_vrf_owned_handoff())
    )
    assert not set(_GENERATOR_READS) - carried, f"read but never faked: {sorted(set(_GENERATOR_READS) - carried)}"


def test_the_circuit_projects_only_vrf_owned_routing(generator_paths: set[str]) -> None:
    """Global zone fields are visible through the local owner, never directly on the circuit or IPAM."""
    assert {path for path in generator_paths if path.startswith("vrf.")} == {
        "vrf.id",
        "vrf.deployment",
        "vrf.deployment.id",
        "vrf.deployment.name",
        "vrf.security_zone",
        *(f"vrf.security_zone.{field}" for field in ("id", "name", "vrf_type", "l3_vni")),
    }
    assert "security_zone" not in generator_paths
    assert not any(
        ".ip_address.security_zone" in path or ".ip_prefix.security_zone" in path for path in generator_paths
    )
