"""The OSPF objects and the interface capabilities edge (schemas/extensions/capabilities/ospf.yml).

``RoutingOSPFInterface`` holds settings of interfaces of ONE device: the device is its ``Parent`` (the settings go with
it) and its re-declared interface edge carries ``common_parent: device``, so the server refuses an interface of another
device. ``ManagedOSPFPeering`` is not on the interface edge: its interfaces are those of its ``ospf_interface``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_OSPF = _ROOT / "schemas" / "extensions" / "capabilities" / "ospf.yml"
_CAPABILITIES = _ROOT / "schemas" / "extensions" / "capabilities" / "capabilities.yml"


def _node(name: str) -> dict[str, Any]:
    return next(node for node in yaml.safe_load(_OSPF.read_text())["nodes"] if node["name"] == name)


def _rel(node: dict[str, Any], name: str) -> dict[str, Any]:
    return next(rel for rel in node["relationships"] if rel["name"] == name)


def test_the_settings_are_a_child_of_their_physical_device() -> None:
    """``device`` is a mandatory Parent; the device lists the settings as a cascading Component on the same identifier."""
    device = _rel(_node("OSPFInterface"), "device")
    assert (device["kind"], device["optional"], device["cardinality"]) == ("Parent", False, "one")
    assert device["peer"] == "DcimPhysicalDevice"
    (extension,) = [
        n for n in yaml.safe_load(_OSPF.read_text())["extensions"]["nodes"] if n["kind"] == "DcimPhysicalDevice"
    ]
    reverse = _rel(extension, "ospf_interfaces")
    assert (reverse["kind"], reverse["on_delete"], reverse["peer"]) == ("Component", "cascade", "RoutingOSPFInterface")
    assert reverse["identifier"] == device["identifier"]


def test_the_interface_edge_is_redeclared_with_a_common_parent() -> None:
    """The node re-declares the generic's edge as it is (peer, kind, identifier) and adds ``common_parent: device``."""
    generic = next(g for g in yaml.safe_load(_CAPABILITIES.read_text())["generics"] if g["name"] == "GenericInterfaces")
    inherited = _rel(generic, "capabilities")
    edge = _rel(_node("OSPFInterface"), "capabilities")
    assert edge["common_parent"] == "device"
    for key in ("peer", "kind", "cardinality", "identifier"):
        assert edge[key] == inherited[key], key
    assert "ManagedGenericInterfaces" in _node("OSPFInterface")["inherit_from"]


def test_the_peering_is_off_the_interface_edge() -> None:
    """The peering reaches its interfaces through its OSPF interface settings only."""
    peering = _node("OSPFPeering")
    assert "ManagedGenericInterfaces" not in peering["inherit_from"]
    assert _rel(peering, "ospf_interface")["peer"] == "RoutingOSPFInterface"
