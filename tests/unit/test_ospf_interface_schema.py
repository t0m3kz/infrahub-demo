"""The OSPF objects and the interface capabilities edge (schemas/extensions/capabilities/ospf.yml).

``RoutingOSPFInterface`` is an interface-side object on the standard interface edge of ``ManagedGenericInterfaces``:
no device field, no re-declared edge, no same-device rule in the schema (checks/interface_layers.py R7 holds it).
``ManagedOSPFPeering`` is not on the interface edge: its interfaces are those of its ``ospf_interface`` settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_OSPF = _ROOT / "schemas" / "extensions" / "capabilities" / "ospf.yml"


def _node(name: str) -> dict[str, Any]:
    return next(node for node in yaml.safe_load(_OSPF.read_text())["nodes"] if node["name"] == name)


def _rel(node: dict[str, Any], name: str) -> dict[str, Any]:
    return next(rel for rel in node["relationships"] if rel["name"] == name)


def test_the_settings_use_the_inherited_interface_edge_and_have_no_device() -> None:
    """No ``device`` Parent, no re-declared ``capabilities``, no reverse edge on the device."""
    node = _node("OSPFInterface")
    assert "ManagedGenericInterfaces" in node["inherit_from"]
    names = {rel["name"] for rel in node["relationships"]}
    assert not {"device", "capabilities"} & names
    extended = {ext["kind"] for ext in yaml.safe_load(_OSPF.read_text())["extensions"]["nodes"]}
    assert "DcimPhysicalDevice" not in extended


def test_the_peering_is_off_the_interface_edge() -> None:
    """The peering reaches its interfaces through its OSPF interface settings only."""
    peering = _node("OSPFPeering")
    assert "ManagedGenericInterfaces" not in peering["inherit_from"]
    assert _rel(peering, "ospf_interface")["peer"] == "RoutingOSPFInterface"
