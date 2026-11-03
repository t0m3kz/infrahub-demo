"""The patch panel model in schemas/extensions/dcim/patch_panel.yml.

DcimPatchPanel is a standalone node (no device, no artifact, no capabilities); its two port kinds inherit DcimEndpoint
only, so a DcimCable joins them unchanged. The model is flat: the panel lists its rear ports and its front ports, and a
front port names its rear port (``rear_port``). An object template cannot pair them (the server refuses a template port
as the peer), so a generator pairs them (tests/unit/test_patch_panel_pairing.py).
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
    """Both ports are addressed by ``port_id`` (``<rack>:<panel>:<rear>``, ``<rack>:<panel>:F<front>``), a read-only
    computed attribute: the server refuses an HFID through two relationships."""
    for name in _PORTS:
        port = _node(name)
        port_id = _item(port["attributes"], "port_id")

        assert port["human_friendly_id"] == ["port_id__value"], name
        assert port_id["read_only"] is True and port_id["unique"] is True, name
    rear = _item(_node("RearPatchPanelPort")["attributes"], "port_id")["computed_attribute"]["jinja2_template"]
    front = _item(_node("FrontPatchPanelPort")["attributes"], "port_id")["computed_attribute"]["jinja2_template"]
    assert rear == "{{ panel__panel_id__value }}:{{ name__value }}"
    assert front == "{{ panel__panel_id__value }}:F{{ name__value }}"


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


def test_port_uniqueness_is_per_panel() -> None:
    """A rear port and a front port are each unique by name within their panel."""
    for name in _PORTS:
        assert _node(name)["uniqueness_constraints"] == [["panel", "name__value"]], name


def test_ports_inherit_only_the_endpoint_generic() -> None:
    """The cable edge comes from DcimEndpoint; no interface, address, segment or capability generic."""
    for name in _PORTS:
        assert _node(name)["inherit_from"] == ["DcimEndpoint"], name


def test_panel_inherits_nothing() -> None:
    """No device, artifact target, capabilities or application instance."""
    assert not _node("PatchPanel").get("inherit_from")


def test_panel_rack_place_matches_the_physical_device() -> None:
    """`position` and `rack_face` mean on a panel what they mean on a DcimPhysicalDevice (the panel inherits no generic,
    so they are declared twice): same kind, optionality, default and choices; only the wording names a panel."""
    dcim = yaml.safe_load((_ROOT / "schemas" / "base" / "dcim.yml").read_text())
    device = next(node for node in dcim["nodes"] if node["name"] == "PhysicalDevice")
    panel = _node("PatchPanel")
    for name in ("position", "rack_face"):
        mine, theirs = _item(panel["attributes"], name), _item(device["attributes"], name)
        for key in ("kind", "label", "optional", "default_value"):
            assert mine.get(key) == theirs.get(key), (name, key)
        assert [c["name"] for c in mine.get("choices") or []] == [c["name"] for c in theirs.get("choices") or []]


def test_panel_declares_no_capability_relationship() -> None:
    """Neither of the two device edges nor the interface edge is declared on the panel or its ports."""
    for name in ("PatchPanel", *_PORTS):
        names = {rel["name"] for rel in _node(name).get("relationships") or []}
        assert names.isdisjoint({"capabilities", "shared_capabilities"}), name


def test_the_panel_lists_both_port_kinds_as_components() -> None:
    """Flat: the panel holds its rear and its front ports (Component on the panel, Parent on the port), so both lists
    show on the panel and a template creates both."""
    for down, child in (("rear_ports", "RearPatchPanelPort"), ("front_ports", "FrontPatchPanelPort")):
        parent_side = _item(_node("PatchPanel")["relationships"], down)
        child_side = _item(_node(child)["relationships"], "panel")

        assert (parent_side["kind"], child_side["kind"]) == ("Component", "Parent"), child
        assert parent_side["identifier"] == child_side["identifier"], child
        assert parent_side["peer"] == f"Dcim{child}" and child_side["optional"] is False, child


def test_a_front_port_names_its_rear_port_inside_its_own_panel() -> None:
    """``rear_port`` is optional (a template cannot set it) and ``common_parent: panel`` makes the server refuse a rear
    port of another panel; the rear port lists its front ports on the same identifier."""
    front = _item(_node("FrontPatchPanelPort")["relationships"], "rear_port")
    rear = _item(_node("RearPatchPanelPort")["relationships"], "front_ports")

    assert front["kind"] == "Attribute" and front["optional"] is True and front["common_parent"] == "panel"
    assert front["peer"] == "DcimRearPatchPanelPort" and rear["peer"] == "DcimFrontPatchPanelPort"
    assert front["identifier"] == rear["identifier"]


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


def test_panel_has_rack_and_both_port_lists_and_no_deployment_or_device_type() -> None:
    """The panel is located by its mandatory rack (LocationRack.deployment gives the facility); no deployment, no device
    type."""
    names = {rel["name"] for rel in _node("PatchPanel")["relationships"]}

    assert names == {"rack", "rear_ports", "front_ports"}
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
    device = _item(dcim["generics"], "DeviceManaged")
    roles = {choice["name"] for choice in _item(device["attributes"], "role")["choices"]}

    assert "patch-panel" not in roles


def _templates(part: str) -> dict[str, dict[str, Any]]:
    path = _ROOT / "data" / "bootstrap" / f"10_physical_devices_templates_patch_panel_{part}.yaml"
    document = yaml.safe_load(path.read_text())
    assert document["spec"]["kind"] == "TemplateDcimPatchPanel"
    return {template["template_name"]: template for template in document["spec"]["data"]}


def test_the_bootstrap_templates_are_panel_templates_in_both_files() -> None:
    """The five panel types, none of them a device template, in the rear file and again in the front file."""
    rear, front = _templates("rear"), _templates("front")
    expected = {"PP-24-LC", "PP-48-LC", "PP-12-SC", "PP-24-RJ45", "PP-CASSETTE-MPO12"}

    assert set(rear) == expected and set(front) == expected
    assert all("device_type" not in template for template in (*rear.values(), *front.values()))


def test_the_templates_give_each_panel_its_rear_and_its_front_ports() -> None:
    """Ranges, never nested (a nested range multiplies): one ranged block per port kind. A jack panel has as many front
    as rear ports, a cassette twelve front LC ports per MPO-12 rear port."""
    expected = {
        "PP-24-LC": (24, "lc-upc", 24, "lc-upc"),
        "PP-48-LC": (48, "lc-upc", 48, "lc-upc"),
        "PP-12-SC": (12, "sc-upc", 12, "sc-upc"),
        "PP-24-RJ45": (24, "rj45", 24, "rj45"),
        "PP-CASSETTE-MPO12": (4, "mpo-12", 48, "lc-upc"),
    }
    rear, front = _templates("rear"), _templates("front")

    for name, (rears, rear_connector, fronts, front_connector) in expected.items():
        for block, count, connector, kind, key in (
            (rear[name]["rear_ports"], rears, rear_connector, "TemplateDcimRearPatchPanelPort", "rear"),
            (front[name]["front_ports"], fronts, front_connector, "TemplateDcimFrontPatchPanelPort", "front"),
        ):
            (port,) = block["data"]
            assert block["kind"] == kind and block["parameters"] == {"expand_range": True}, (name, key)
            assert port["name"] == f"[1-{count}]" and port["template_name"] == f"{name}_{key}_[1-{count}]", name
            assert port["connector"] == connector and "front_ports" not in port, name
