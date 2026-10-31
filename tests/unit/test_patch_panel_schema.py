"""The patch panel model in schemas/extensions/dcim/patch_panel.yml.

DcimPatchPanel is a standalone node (no device, no artifact, no capabilities); its two port kinds inherit DcimEndpoint
only, so a DcimCable joins them unchanged. A jack is a rear port and the front port(s) under it: the pairing is the
``front_ports`` Component edge, which an object template materializes (an attribute relationship it could only copy).
The capability-edge side of this (no ``capabilities`` / ``shared_capabilities`` on any kind or their templates) is
guarded in tests/unit/test_capability_edge_names.py.
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


_PORTS = ("RearPatchPanelPort", "FrontPatchPanelPort")


def test_port_hfid_is_the_computed_port_id_because_it_cannot_go_through_two_relationships() -> None:
    """Both ports are addressed by ``port_id`` (``<panel>:<rear>``, ``<panel>:<rear>:<front>``), a read-only computed
    attribute: the server refuses an HFID through two relationships ("panel is not a valid attribute of ...")."""
    for name in _PORTS:
        port = _node(name)
        port_id = _item(port["attributes"], "port_id")

        assert port["human_friendly_id"] == ["port_id__value"], name
        assert port_id["read_only"] is True and port_id["unique"] is True, name
    rear = _item(_node("RearPatchPanelPort")["attributes"], "port_id")["computed_attribute"]["jinja2_template"]
    front = _item(_node("FrontPatchPanelPort")["attributes"], "port_id")["computed_attribute"]["jinja2_template"]
    assert rear == "{{ panel__panel_id__value }}:{{ name__value }}"
    assert front == "{{ rear_port__port_id__value }}:{{ name__value }}"


def test_a_panel_is_unique_by_its_mandatory_rack_and_its_name() -> None:
    """`PP-01` may exist in every rack: the constraint is (rack, name), which needs the rack mandatory (the server
    refuses a constraint on an optional relationship), and the HFID and the id the ports are built from follow."""
    panel = _node("PatchPanel")
    rack = _item(panel["relationships"], "rack")
    panel_id = _item(panel["attributes"], "panel_id")

    assert panel["uniqueness_constraints"] == [["rack", "name__value"]]
    assert panel["human_friendly_id"] == ["rack__shortname__value", "name__value"]
    assert rack["optional"] is False
    assert not _item(panel["attributes"], "name").get("unique")
    assert panel_id["read_only"] is True and panel_id["unique"] is True
    assert panel_id["computed_attribute"]["jinja2_template"] == "{{ rack__shortname__value }}:{{ name__value }}"


def test_port_uniqueness_is_per_parent() -> None:
    """A rear port is unique in its panel, a front port under its rear port."""
    assert _node("RearPatchPanelPort")["uniqueness_constraints"] == [["panel", "name__value"]]
    assert _node("FrontPatchPanelPort")["uniqueness_constraints"] == [["rear_port", "name__value"]]


def test_ports_inherit_only_the_endpoint_generic() -> None:
    """The cable edge comes from DcimEndpoint; no interface, address, segment or capability generic."""
    for name in _PORTS:
        assert _node(name)["inherit_from"] == ["DcimEndpoint"], name


def test_panel_inherits_nothing() -> None:
    """No device, artifact target, capabilities or application instance."""
    assert not _node("PatchPanel").get("inherit_from")


def test_panel_declares_no_capability_relationship() -> None:
    """Neither of the two device edges nor the interface edge is declared on the panel or its ports."""
    for name in ("PatchPanel", *_PORTS):
        names = {rel["name"] for rel in _node(name).get("relationships") or []}
        assert names.isdisjoint({"capabilities", "shared_capabilities"}), name


def test_the_pairing_is_structure_panel_to_rear_to_front() -> None:
    """Component on the parent, Parent on the child, one identifier each: the front port is a component of its rear
    port, so a template that creates the rear port creates its front port(s), and the pair needs no name."""
    chain = (
        ("PatchPanel", "rear_ports", "RearPatchPanelPort", "panel"),
        ("RearPatchPanelPort", "front_ports", "FrontPatchPanelPort", "rear_port"),
    )
    for parent, down, child, up in chain:
        parent_side = _item(_node(parent)["relationships"], down)
        child_side = _item(_node(child)["relationships"], up)

        assert (parent_side["kind"], child_side["kind"]) == ("Component", "Parent"), parent
        assert parent_side["identifier"] == child_side["identifier"], parent
        assert parent_side["peer"] == f"Dcim{child}" and child_side["optional"] is False, parent


def test_connectors_and_labels() -> None:
    """The six connector choices of the brief on both kinds; the name is required and the label optional."""
    for name in _PORTS:
        attributes = _node(name)["attributes"]

        assert [c["name"] for c in _item(attributes, "connector")["choices"]] == [
            "lc-upc",
            "lc-apc",
            "sc-upc",
            "sc-apc",
            "mpo-12",
            "rj45",
        ], name
        assert _item(attributes, "name")["kind"] == "Text" and _item(attributes, "name")["optional"] is False, name
        assert _item(attributes, "label")["optional"] and _item(attributes, "description")["optional"], name
        assert "face" not in {a["name"] for a in attributes}, name


def test_panel_has_rack_and_rear_ports_and_no_deployment_or_device_type() -> None:
    """The panel is located by its mandatory rack (LocationRack.deployment gives the facility); no deployment, no device
    type."""
    names = {rel["name"] for rel in _node("PatchPanel")["relationships"]}

    assert names == {"rack", "rear_ports"}
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


def _templates() -> dict[str, dict[str, Any]]:
    path = _ROOT / "data" / "bootstrap" / "10_physical_devices_templates_patch_panel.yaml"
    document = yaml.safe_load(path.read_text())
    assert document["spec"]["kind"] == "TemplateDcimPatchPanel"
    return {template["template_name"]: template for template in document["spec"]["data"]}


def test_the_bootstrap_templates_are_panel_templates() -> None:
    """The five panel types, none of them a device template."""
    templates = _templates()

    assert set(templates) == {"PP-24-LC", "PP-48-LC", "PP-12-SC", "PP-24-RJ45", "PP-CASSETTE-MPO12"}
    assert all("device_type" not in template for template in templates.values())


def test_a_jack_template_gives_each_rear_port_exactly_one_front_port_of_the_same_number() -> None:
    """The mapping lives in the template, written out: a ranged nested list would multiply (24 x 24 = 576)."""
    expected = {"PP-24-LC": 24, "PP-48-LC": 48, "PP-12-SC": 12, "PP-24-RJ45": 24}
    templates = _templates()

    for name, jacks in expected.items():
        rears = templates[name]["rear_ports"]
        assert rears["kind"] == "TemplateDcimRearPatchPanelPort" and "parameters" not in rears, name
        assert [r["name"] for r in rears["data"]] == [str(n) for n in range(1, jacks + 1)], name
        for rear in rears["data"]:
            fronts = rear["front_ports"]
            assert fronts["kind"] == "TemplateDcimFrontPatchPanelPort", name
            assert [f["name"] for f in fronts["data"]] == [rear["name"]], (name, rear["name"])
            assert fronts["data"][0]["connector"] == rear["connector"], (name, rear["name"])


def test_the_cassette_template_gives_each_mpo_rear_port_twelve_lc_front_ports() -> None:
    """A cassette is the 1:N case of the same structure."""
    rears = _templates()["PP-CASSETTE-MPO12"]["rear_ports"]["data"]

    assert [r["name"] for r in rears] == ["1", "2", "3", "4"]
    for rear in rears:
        assert rear["connector"] == "mpo-12"
        assert [f["name"] for f in rear["front_ports"]["data"]] == [str(n) for n in range(1, 13)]
        assert {f["connector"] for f in rear["front_ports"]["data"]} == {"lc-upc"}


def test_template_names_are_unique_within_the_file() -> None:
    """A template_name is the template object's key: a repeat would be an upsert of another port."""
    names: list[str] = []
    for template in _templates().values():
        names.append(template["template_name"])
        for rear in template["rear_ports"]["data"]:
            names.append(rear["template_name"])
            names.extend(front["template_name"] for front in rear["front_ports"]["data"])

    assert len(names) == len(set(names))
