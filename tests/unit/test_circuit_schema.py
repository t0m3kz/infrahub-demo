"""The circuit model in schemas/extensions/topology/topology_connectivity.yml.

Two kinds: TopologyCarrierCircuit (the carrier) and TopologySessionCircuit (dci, handoff, tunnel by ``circuit_type``).
The two ends (``capabilities``) and ``locations`` are declared once on the TopologyCircuit generic, which the nodes
inherit FIRST so that its ``capabilities`` wins over ManagedGenericInterfaces'.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_SCHEMA = _ROOT / "schemas" / "extensions" / "topology" / "topology_connectivity.yml"
_CLOUD = _ROOT / "schemas" / "extensions" / "topology" / "topology_cloud_network.yml"


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


def test_the_ends_and_the_locations_are_declared_once_on_the_circuit_generic() -> None:
    """capabilities (the two ends) and locations (two facilities at most) sit on TopologyCircuit."""
    relationships = _generic("Circuit")["relationships"]

    ends = _field(relationships, "capabilities")
    assert (ends["peer"], ends["identifier"], ends["min_count"], ends["max_count"]) == (
        "DcimInterface",
        "interface_capabilities",
        2,
        2,
    )
    locations = _field(relationships, "locations")
    assert (locations["peer"], locations["max_count"]) == ("TopologyFacility", 2)
    assert "min_count" not in locations


def test_the_nodes_inherit_the_circuit_generic_first_and_redeclare_nothing() -> None:
    """TopologyCircuit comes first so its capabilities wins; the nodes declare neither relationship themselves."""
    for node in _document()["nodes"]:
        assert node["inherit_from"] == ["TopologyCircuit", "ManagedGeneric", "ManagedGenericInterfaces"], node["name"]
        names = {relationship["name"] for relationship in node.get("relationships") or []}
        assert names.isdisjoint({"capabilities", "locations"}), node["name"]


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
    assert _field(session["attributes"], "maximum_routes")["optional"] is True


def test_the_cloud_attachment_peers_the_session_kind() -> None:
    """CloudHybridAttachment.virtual_circuit points at TopologySessionCircuit on the shared cloud_endpoint identifier."""
    extensions = yaml.safe_load(_CLOUD.read_text())["extensions"]["nodes"]
    attachment = next(item for item in extensions if item["kind"] == "CloudHybridAttachment")
    relationship = _field(attachment["relationships"], "virtual_circuit")

    assert relationship["peer"] == "TopologySessionCircuit"
    assert (
        relationship["identifier"] == _field(_node("SessionCircuit")["relationships"], "cloud_endpoint")["identifier"]
    )
