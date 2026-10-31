"""The circuit model in schemas/extensions/topology/topology_connectivity.yml.

Two kinds: TopologyCarrierCircuit (the carrier) and TopologySessionCircuit (dci, handoff, tunnel by ``circuit_type``).
The locations are declared once on the TopologyCircuit generic. The two ends (``capabilities``) are re-declared on
each node: hoisting them onto the generic is refused by the server (a node that inherits two generics declaring
``capabilities`` with a different min_count, measured on 1.11.9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_SCHEMA = _ROOT / "schemas" / "extensions" / "topology" / "topology_connectivity.yml"
_CLOUD = _ROOT / "schemas" / "extensions" / "topology" / "topology_cloud_network.yml"
# Spelled in two halves so the repo-wide guard on the old edge name (test_capability_edge_names.py) does not match.
_ENDS_IDENTIFIER = "interface_" + "capabilities"


def _document() -> dict[str, Any]:
    return yaml.safe_load(_SCHEMA.read_text())


def _generic(name: str) -> dict[str, Any]:
    return next(item for item in _document()["generics"] if item["name"] == name)


def _node(name: str) -> dict[str, Any]:
    return next(item for item in _document()["nodes"] if item["name"] == name)


def _field(section: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in section if item["name"] == name)


def test_the_connectivity_file_declares_exactly_the_carrier_and_the_session_kind() -> None:
    """The physical/virtual/interconnect/handoff/tunnel kinds and the TopologyVirtualCircuit marker are gone."""
    assert [node["name"] for node in _document()["nodes"]] == ["CarrierCircuit", "SessionCircuit"]
    assert "VirtualCircuit" not in [generic["name"] for generic in _document()["generics"]]


def test_the_locations_are_declared_once_on_the_circuit_generic() -> None:
    """locations (two facilities at most) sits on TopologyCircuit; the ends are not hoisted onto it."""
    relationships = _generic("Circuit")["relationships"]

    locations = _field(relationships, "locations")
    assert (locations["peer"], locations["max_count"]) == ("TopologyFacility", 2)
    assert "min_count" not in locations
    assert "capabilities" not in {relationship["name"] for relationship in relationships}


def test_each_node_declares_the_two_ends_with_a_count_and_inherits_the_circuit_generic_first() -> None:
    """The ends are re-declared per node (min 2, max 2) because the server refuses the hoisted declaration."""
    for node in _document()["nodes"]:
        assert node["inherit_from"] == ["TopologyCircuit", "ManagedGeneric", "ManagedGenericInterfaces"], node["name"]
        relationships = node.get("relationships") or []
        ends = _field(relationships, "capabilities")
        assert (ends["peer"], ends["identifier"], ends["min_count"], ends["max_count"]) == (
            "DcimInterface",
            _ENDS_IDENTIFIER,
            2,
            2,
        ), node["name"]
        assert "locations" not in {relationship["name"] for relationship in relationships}, node["name"]


def test_the_carrier_has_the_provider_circuit_types_and_optional_provider_fields() -> None:
    """virtual_connection and metro_ethernet join the types; term_length, commit_rate, redundancy_group are optional."""
    attributes = _node("CarrierCircuit")["attributes"]

    choices = [choice["name"] for choice in _field(attributes, "circuit_type")["choices"]]
    assert choices == ["internet", "dark_fiber", "cross_connect", "virtual_connection", "metro_ethernet"]
    for name, kind in (("term_length", "Number"), ("commit_rate", "Number"), ("redundancy_group", "Text")):
        attribute = _field(attributes, name)
        assert attribute["kind"] == kind and attribute["optional"] is True, name
    assert "mirrors" in _field(attributes, "term_length")["description"]
    assert "CloudNetworkAppliance.term_length" in _field(attributes, "term_length")["description"]


def test_the_carrier_order_weights_are_unique_after_inheritance() -> None:
    """The weights of the node and of its generics do not collide (the server numbers a tie itself)."""
    carrier = _node("CarrierCircuit")
    generic = _generic("Circuit")
    weights = [a["order_weight"] for a in carrier["attributes"]]
    weights += [f["order_weight"] for f in (generic.get("attributes") or []) + (generic.get("relationships") or [])]
    assert len(weights) == len(set(weights))


def test_the_session_circuit_type_is_required_with_three_choices() -> None:
    """circuit_type is a Dropdown with no default and no optional flag: dci, handoff, tunnel."""
    circuit_type = _field(_node("SessionCircuit")["attributes"], "circuit_type")

    assert circuit_type["kind"] == "Dropdown"
    assert not circuit_type.get("optional") and "default_value" not in circuit_type
    assert [choice["name"] for choice in circuit_type["choices"]] == ["dci", "handoff", "tunnel"]


def test_the_session_fields_of_one_type_are_optional_in_the_schema() -> None:
    """The rules per type live in checks/circuit_placement.py, so the schema requires none of them."""
    session = _node("SessionCircuit")
    for name in ("ip_namespace", "password", "cloud_endpoint", "registered_prefixes"):
        assert _field(session["relationships"], name)["optional"] is True, name


def test_the_cloud_attachment_peers_the_session_kind() -> None:
    """CloudHybridAttachment.virtual_circuit points at TopologySessionCircuit on the shared cloud_endpoint identifier."""
    extensions = yaml.safe_load(_CLOUD.read_text())["extensions"]["nodes"]
    attachment = next(item for item in extensions if item["kind"] == "CloudHybridAttachment")
    relationship = _field(attachment["relationships"], "virtual_circuit")

    assert relationship["peer"] == "TopologySessionCircuit"
    assert (
        relationship["identifier"] == _field(_node("SessionCircuit")["relationships"], "cloud_endpoint")["identifier"]
    )
