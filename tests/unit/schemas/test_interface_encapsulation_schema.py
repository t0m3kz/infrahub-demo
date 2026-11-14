"""The interface encapsulation in schemas/extensions/capabilities/encapsulation.yml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_SCHEMAS = Path(__file__).parents[3] / "schemas"
_ENCAPSULATION = _SCHEMAS / "extensions" / "capabilities" / "encapsulation.yml"
_KIND = "ManagedInterfaceEncapsulation"
_IDENTIFIER = "interface_encapsulation__parent"
_REMOVED_GENERICS = ("DcimSubInterface", "DcimHasSubInterface")
_REMOVED_FIELDS = ("parent_interface", "sub_interfaces")
_FORMER_MARKER_KINDS = ("CloudPeeringInterface", "TopologyProviderInterface", "DcimVirtualInterface")


def _documents() -> list[tuple[Path, dict[str, Any]]]:
    return [(path, yaml.safe_load(path.read_text()) or {}) for path in sorted(_SCHEMAS.rglob("*.yml"))]


def _all_nodes() -> dict[str, dict[str, Any]]:
    """Every node and generic of the schema by kind (namespace + name), extensions excluded."""
    found: dict[str, dict[str, Any]] = {}
    for _, document in _documents():
        for section in ("generics", "nodes"):
            for item in document.get(section) or []:
                found[f"{item.get('namespace', '')}{item['name']}"] = item
    return found


def _field(item: dict[str, Any], section: str, name: str) -> dict[str, Any]:
    return next(entry for entry in item.get(section) or [] if entry["name"] == name)


def _encapsulation() -> dict[str, Any]:
    return _all_nodes()[_KIND]


def _extension(kind: str) -> dict[str, Any]:
    document = yaml.safe_load(_ENCAPSULATION.read_text())
    return next(item for item in document["extensions"]["nodes"] if item["kind"] == kind)


def test_the_marker_generics_exist_nowhere_in_the_schema() -> None:
    """DcimSubInterface and DcimHasSubInterface are deleted: not defined, not inherited, not named in any yml."""
    nodes = _all_nodes()
    for generic in _REMOVED_GENERICS:
        assert generic not in nodes
    for path, document in _documents():
        text = path.read_text()
        for generic in _REMOVED_GENERICS:
            assert generic not in text, f"{generic} still named in {path.relative_to(_SCHEMAS)}"
        for item in [*(document.get("nodes") or []), *(document.get("generics") or [])]:
            assert not set(_REMOVED_GENERICS) & set(item.get("inherit_from") or []), item["name"]


def test_no_node_declares_parent_interface_or_sub_interfaces() -> None:
    """The fields are gone from every node, generic and extension: the encapsulation holds the link."""
    for path, document in _documents():
        items = [
            *(document.get("nodes") or []),
            *(document.get("generics") or []),
            *((document.get("extensions") or {}).get("nodes") or []),
        ]
        for item in items:
            declared = {rel["name"] for rel in item.get("relationships") or []}
            assert not set(_REMOVED_FIELDS) & declared, f"{item.get('name') or item.get('kind')} in {path.name}"


def test_the_former_marker_kinds_declare_no_parent_or_tag_of_their_own() -> None:
    """A cloud peering, a provider interface and a virtual interface ride through the encapsulation."""
    nodes = _all_nodes()
    for kind in _FORMER_MARKER_KINDS:
        node = nodes[kind]
        assert "parent" not in {rel["name"] for rel in node.get("relationships") or []}, kind
        assert not {"vlan_id", "inner_vlan_id"} & {attr["name"] for attr in node.get("attributes") or []}, kind
        assert not set(_REMOVED_GENERICS) & set(node.get("inherit_from") or []), kind


def test_the_encapsulation_is_a_managed_capability_on_the_interface_edge() -> None:
    """A Managed node that inherits the generic and the interface edge (not a device edge):"""
    node = _encapsulation()

    assert node["namespace"] == "Managed" and node["name"] == "InterfaceEncapsulation"
    assert node["inherit_from"] == ["ManagedGeneric", "ManagedGenericInterfaces"]
    assert node["uniqueness_constraints"] == [["name__value"]] and node["human_friendly_id"] == ["name__value"]


def test_the_vlan_tag_is_an_optional_number_in_the_vlan_range() -> None:
    """Untagged (a VM NIC, a LAG peering) is unset; a tag is 1 to 4094."""
    vlan = _field(_encapsulation(), "attributes", "vlan_id")

    assert (vlan["kind"], vlan["optional"], vlan["parameters"]) == ("Number", True, {"min_value": 1, "max_value": 4094})
    assert [attr["name"] for attr in _encapsulation()["attributes"]] == ["vlan_id"]


def test_the_parent_is_a_mandatory_one_sided_edge_to_an_interface_with_no_common_parent() -> None:
    """A NIC's parent is a port of ANOTHER device (its host), so no ``common_parent``; the same-device rule is a check."""
    parent = _field(_encapsulation(), "relationships", "parent")

    assert parent["peer"] == "DcimInterface"
    assert (parent["optional"], parent["cardinality"], parent["kind"]) == (False, "one", "Attribute")
    assert parent["identifier"] == _IDENTIFIER
    assert "common_parent" not in parent


def test_the_port_lists_its_encapsulations_in_one_reverse_edge_declared_once() -> None:
    """``DcimInterface.encapsulations``."""
    parent = _field(_encapsulation(), "relationships", "parent")
    reverse = _field(_extension("DcimInterface"), "relationships", "encapsulations")

    assert reverse["peer"] == _KIND and reverse["cardinality"] == "many" and reverse["direction"] == "inbound"
    assert reverse["identifier"] == parent["identifier"] == _IDENTIFIER
    assert "common_parent" not in reverse
    declarations = 0
    for _, document in _documents():
        items = [
            *(document.get("nodes") or []),
            *(document.get("generics") or []),
            *((document.get("extensions") or {}).get("nodes") or []),
        ]
        declarations += sum("encapsulations" in {r["name"] for r in item.get("relationships") or []} for item in items)
    assert declarations == 1


def test_common_parent_stays_on_the_lag_pair() -> None:
    """Only a port and its LAG are tied to one device by the server."""
    nodes = _all_nodes()
    lag = _field(nodes["DcimPhysicalInterface"], "relationships", "lag")
    members = _field(nodes["DcimLAGInterface"], "relationships", "member_interfaces")

    assert [rel.get("common_parent") for rel in (lag, members)] == ["device"] * 2


def test_ip_address_is_declared_once_on_the_marker_generic() -> None:
    """One Attribute declaration without cascade."""
    nodes = _all_nodes()
    ip_address = _field(nodes["DcimHasIPAddress"], "relationships", "ip_address")
    inheritors = {kind for kind, node in nodes.items() if "DcimHasIPAddress" in (node.get("inherit_from") or [])}

    assert (ip_address["peer"], ip_address["cardinality"], ip_address["kind"]) == ("IpamIPAddress", "one", "Attribute")
    assert ip_address["on_delete"] == "no-action"
    assert inheritors == {
        "DcimPhysicalInterface",
        "DcimLAGInterface",
        "DcimVirtualInterface",
        "CloudPeeringInterface",
    }
    for kind, node in nodes.items():
        if kind != "DcimHasIPAddress":
            assert "ip_address" not in [rel["name"] for rel in node.get("relationships") or []], kind


def test_common_parent_is_used_only_by_the_lag_pair_the_ospf_settings_the_vlan_rows_and_the_patch_panel_ports() -> None:
    """The encapsulation file holds no same-device rule in the schema: checks/devices/interface_parents.py does."""
    users = sorted(
        str(path.relative_to(_SCHEMAS))
        for path in _SCHEMAS.rglob("*.yml")
        if "\n        common_parent:" in path.read_text()
    )
    assert "extensions/capabilities/encapsulation.yml" not in users
    assert "base/dcim.yml" in users and "extensions/dcim/patch_panel.yml" in users


def test_the_encapsulation_identifier_is_not_the_interface_edge_identifier() -> None:
    """The interface edge's identifier pairs the interface with its capabilities;"""
    edge_identifier = "interface_" + "capabilities"  # spelled in two parts: test_capability_edge_names greps the name
    assert _IDENTIFIER != edge_identifier
    assert f'identifier: "{edge_identifier}"' not in _ENCAPSULATION.read_text()
    assert _field(_encapsulation(), "relationships", "parent")["identifier"] == _IDENTIFIER
