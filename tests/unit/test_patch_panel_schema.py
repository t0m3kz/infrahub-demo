"""The patch panel model in schemas/extensions/dcim/patch_panel.yml.

DcimPatchPanel is a standalone node (no device, no artifact, no capabilities); DcimPatchPanelPort inherits DcimEndpoint
only, so a DcimCable joins it unchanged. The capability-edge side of this (no ``capabilities`` / ``shared_capabilities``
on either kind or their templates) is guarded in tests/unit/test_capability_edge_names.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).parents[2]
_SCHEMA = _ROOT / "schemas" / "extensions" / "dcim" / "patch_panel.yml"


def _document() -> dict[str, Any]:
    return yaml.safe_load(_SCHEMA.read_text())


def _node(name: str) -> dict[str, Any]:
    return next(node for node in _document()["nodes"] if node["name"] == name)


def _item(section: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(item for item in section if item["name"] == name)


def test_port_hfid_and_uniqueness_name_the_panel_the_face_and_the_jack() -> None:
    """A port is addressed by [panel name, face, name] and unique on [panel, face, name]."""
    port = _node("PatchPanelPort")

    assert port["human_friendly_id"] == ["panel__name__value", "face__value", "name__value"]
    assert port["uniqueness_constraints"] == [["panel", "face__value", "name__value"]]


def test_port_inherits_only_the_endpoint_generic() -> None:
    """The cable edge comes from DcimEndpoint; no interface, address, segment or capability generic."""
    assert _node("PatchPanelPort")["inherit_from"] == ["DcimEndpoint"]


def test_panel_inherits_nothing() -> None:
    """No device, artifact target, capabilities or application instance."""
    assert not _node("PatchPanel").get("inherit_from")


def test_panel_declares_no_capability_relationship() -> None:
    """Neither of the two device edges nor the interface edge is declared on the panel or its port."""
    for name in ("PatchPanel", "PatchPanelPort"):
        names = {rel["name"] for rel in _node(name).get("relationships") or []}
        assert names.isdisjoint({"capabilities", "shared_capabilities"}), name


def test_port_is_a_cascading_child_of_its_panel() -> None:
    """Parent on the port, Component on the panel, one identifier."""
    panel_side = _item(_node("PatchPanel")["relationships"], "ports")
    port_side = _item(_node("PatchPanelPort")["relationships"], "panel")

    assert (panel_side["kind"], port_side["kind"]) == ("Component", "Parent")
    assert panel_side["identifier"] == port_side["identifier"]
    assert port_side["optional"] is False


def test_port_faces_and_connectors() -> None:
    """front | rear, and the six connector choices of the brief."""
    attributes = _node("PatchPanelPort")["attributes"]

    assert [c["name"] for c in _item(attributes, "face")["choices"]] == ["front", "rear"]
    assert [c["name"] for c in _item(attributes, "connector")["choices"]] == [
        "lc-upc",
        "lc-apc",
        "sc-upc",
        "sc-apc",
        "mpo-12",
        "rj45",
    ]
    assert _item(attributes, "name")["kind"] == "Text"
    assert _item(attributes, "label")["optional"] and _item(attributes, "description")["optional"]


def test_panel_has_rack_and_ports_and_no_deployment_or_device_type() -> None:
    """The panel is located by its rack (LocationRack.deployment gives the facility); no deployment, no device type."""
    names = {rel["name"] for rel in _node("PatchPanel")["relationships"]}

    assert names == {"rack", "ports"}
    assert "position" not in {a["name"] for a in _node("PatchPanel")["attributes"]}
    assert _item(_node("PatchPanel")["relationships"], "rack")["peer"] == "LocationRack"


def test_the_rack_edge_pairs_by_identifier_and_the_hosting_generic_has_no_panels() -> None:
    """LocationRack.patch_panels is the other end of the panel's rack; nothing was added to TopologyDeviceHosting."""
    rack = _item(
        next(e for e in _document()["extensions"]["nodes"] if e["kind"] == "LocationRack")["relationships"],
        "patch_panels",
    )
    topology = yaml.safe_load((_ROOT / "schemas" / "extensions" / "topology" / "topology.yml").read_text())
    hosting = _item(topology["generics"], "DeviceHosting")

    assert rack["identifier"] == _item(_node("PatchPanel")["relationships"], "rack")["identifier"]
    assert rack["peer"] == "DcimPatchPanel"
    assert "patch_panels" not in {rel["name"] for rel in hosting["relationships"]}


def test_the_panel_role_is_gone_from_the_device_roles() -> None:
    """A panel is not a device: no ``patch-panel`` role."""
    dcim = yaml.safe_load((_ROOT / "schemas" / "base" / "dcim.yml").read_text())
    device = _item(dcim["generics"], "Device")
    roles = {choice["name"] for choice in _item(device["attributes"], "role")["choices"]}

    assert "patch-panel" not in roles


def test_the_bootstrap_templates_are_panel_templates_with_front_and_rear_blocks() -> None:
    """TemplateDcimPatchPanel, each template with a front and a rear block of [1-24]."""
    path = _ROOT / "data" / "bootstrap" / "10_physical_devices_templates_patch_panel.yaml"
    document = yaml.safe_load(path.read_text())

    assert document["spec"]["kind"] == "TemplateDcimPatchPanel"
    assert all("device_type" not in template for template in document["spec"]["data"])
    assert {t["template_name"] for t in document["spec"]["data"]} == {"PP-24-LC", "PP-24-RJ45"}
    for template in document["spec"]["data"]:
        ports = template["ports"]
        assert ports["kind"] == "TemplateDcimPatchPanelPort" and ports["parameters"]["expand_range"] is True
        assert [(p["face"], p["name"]) for p in ports["data"]] == [("front", "[1-24]"), ("rear", "[1-24]")]
