"""The parent/child relationships of interfaces in schemas/base/dcim.yml.

DcimVirtualInterface.parent_interface (outbound, one) is the child side of a sub-interface; DcimHasSubInterface.
sub_interfaces (inbound, many) is the other end, declared once on a marker generic that every kind able to carry
sub-interfaces inherits (a NIC is the parent of several sub-interfaces, which the server accepts only with the many
side declared: ``has 2 peers for sub__interface, maximum of 1 allowed`` otherwise). Only the LAG pair carries
``common_parent: device`` (a port and its LAG are on one device). The NIC of a virtual machine rides a port of ANOTHER
device (its host) through the same parent_interface, so the sub-interface pair has no common_parent and
checks/interface_parents.py holds the cross-device rule. ``ip_address`` is declared once on DcimHasIPAddress.
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


def test_common_parent_stays_only_on_the_lag_pair() -> None:
    """Only a port and its LAG are tied to one device by the server; sub-interface links are held by a check."""
    schema = _schema()
    child = _item(_item(schema["nodes"], "VirtualInterface")["relationships"], "parent_interface")
    parent = _item(_item(schema["generics"], "HasSubInterface")["relationships"], "sub_interfaces")
    lag = _item(_item(schema["nodes"], "PhysicalInterface")["relationships"], "lag")
    members = _item(_item(schema["nodes"], "LAGInterface")["relationships"], "member_interfaces")

    assert [rel.get("common_parent") for rel in (lag, members)] == ["device"] * 2
    assert "common_parent" not in child
    assert "common_parent" not in parent


def test_host_interface_is_gone_the_nic_rides_its_host_port_through_parent_interface() -> None:
    """A VM NIC's parent_interface is its host port (another device); there is no relationship of its own."""
    nodes = _schema()["nodes"]
    names = [rel["name"] for rel in _item(nodes, "VirtualInterface").get("relationships") or []]

    assert "host_interface" not in names
    assert "parent_interface" in names


def test_ip_address_is_declared_once_on_the_marker_generic() -> None:
    """One Attribute declaration without cascade (a multisite VIP is one address on two loopbacks), inherited by the
    three address-holding kinds and not by the console interface."""
    schema = _schema()
    ip_address = _item(_item(schema["generics"], "HasIPAddress")["relationships"], "ip_address")
    nodes = {(node["namespace"] + node["name"]): node for node in schema["nodes"]}
    inheritors = {kind for kind, node in nodes.items() if "DcimHasIPAddress" in (node.get("inherit_from") or [])}

    assert (ip_address["peer"], ip_address["cardinality"], ip_address["kind"]) == ("IpamIPAddress", "one", "Attribute")
    assert ip_address["on_delete"] == "no-action"
    assert inheritors == {"DcimPhysicalInterface", "DcimLAGInterface", "DcimVirtualInterface"}
    for node in schema["nodes"]:
        assert "ip_address" not in [rel["name"] for rel in node.get("relationships") or []], node["name"]
