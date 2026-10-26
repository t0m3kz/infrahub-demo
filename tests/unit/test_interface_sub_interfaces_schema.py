"""The parent/child relationships of interfaces in schemas/base/dcim.yml.

DcimVirtualInterface.parent_interface (outbound, one) is the child side of a sub-interface; DcimHasSubInterface.
sub_interfaces (inbound, many) is the other end, declared once on a marker generic that every kind able to carry
sub-interfaces inherits (a NIC is the parent of several sub-interfaces, which the server accepts only with the many
side declared: ``has 2 peers for sub__interface, maximum of 1 allowed`` otherwise). Both, and the LAG pair, carry
``common_parent: device``: a sub-interface and its port, a port and its LAG, are on one device. The NIC of a virtual
machine is pinned to a port of ANOTHER device (its host), so that link is a relationship of its own, host_interface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DCIM = Path(__file__).parents[2] / "schemas" / "base" / "dcim.yml"


def _schema() -> dict[str, Any]:
    return yaml.safe_load(_DCIM.read_text())


def _item(section: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in section if item["name"] == name)


def test_the_two_ends_share_one_identifier_with_opposite_directions() -> None:
    """The shared identifier is what pairs the two relationships; the directions must differ."""
    schema = _schema()
    child = _item(_item(schema["nodes"], "VirtualInterface")["relationships"], "parent_interface")
    parent = _item(_item(schema["generics"], "HasSubInterface")["relationships"], "sub_interfaces")

    assert child["identifier"] == parent["identifier"] == "sub__interface"
    assert (child["direction"], parent["direction"]) == ("outbound", "inbound")
    assert (child["cardinality"], parent["cardinality"]) == ("one", "many")
    assert child["peer"] == "DcimInterface"
    assert parent["peer"] == "DcimVirtualInterface"


def test_kinds_that_carry_sub_interfaces_inherit_the_marker() -> None:
    """A port, a LAG and a virtual interface can be a parent; a console port cannot."""
    nodes = {(node["namespace"] + node["name"]): node for node in _schema()["nodes"]}
    inheritors = {kind for kind, node in nodes.items() if "DcimHasSubInterface" in (node.get("inherit_from") or [])}

    assert inheritors == {"DcimPhysicalInterface", "DcimLAGInterface", "DcimVirtualInterface"}


def test_the_marker_declares_the_relationship_only_once() -> None:
    """sub_interfaces is not redeclared on a node: there is no allow_override, a node must not repeat a generic's field."""
    for node in _schema()["nodes"]:
        assert "sub_interfaces" not in [rel["name"] for rel in node.get("relationships") or []], node["name"]


def test_a_sub_interface_and_its_port_share_a_device() -> None:
    """common_parent: device on both ends of the pair and on the LAG membership: the server refuses a cross-device link."""
    schema = _schema()
    child = _item(_item(schema["nodes"], "VirtualInterface")["relationships"], "parent_interface")
    parent = _item(_item(schema["generics"], "HasSubInterface")["relationships"], "sub_interfaces")
    lag = _item(_item(schema["nodes"], "PhysicalInterface")["relationships"], "lag")
    members = _item(_item(schema["nodes"], "LAGInterface")["relationships"], "member_interfaces")

    assert [rel.get("common_parent") for rel in (child, parent, lag, members)] == ["device"] * 4


def test_the_nic_of_a_virtual_machine_is_pinned_by_a_relationship_of_its_own() -> None:
    """host_interface crosses devices (VM -> host), so it is not parent_interface and carries no common_parent."""
    schema = _schema()
    nic = _item(_item(schema["nodes"], "VirtualInterface")["relationships"], "host_interface")
    child = _item(_item(schema["nodes"], "VirtualInterface")["relationships"], "parent_interface")

    assert nic["peer"] == "DcimPhysicalInterface"
    assert nic["cardinality"] == "one"
    assert "common_parent" not in nic
    assert nic["identifier"] != child["identifier"]
