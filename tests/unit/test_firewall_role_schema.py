"""The firewall device roles are one spelling in the schema, the constants and the bootstrap data."""

from __future__ import annotations

from typing import Any

import yaml

from tests.integration.demo_data import ROOT
from utils.fabric_roles import FIREWALL_ROLES


def _role_choices(path: str, kind: str) -> set[str]:
    """The names of the ``role`` Dropdown choices of a node or generic in a schema file."""
    schema = yaml.safe_load((ROOT / path).read_text())
    for section in ("nodes", "generics"):
        for node in schema.get(section) or []:
            if node["name"] == kind:
                attribute = next(a for a in node["attributes"] if a["name"] == "role")
                return {choice["name"] for choice in attribute["choices"]}
    raise AssertionError(f"{kind} not found in {path}")


_DEVICE = _role_choices("schemas/base/dcim.yml", "DeviceManaged")
_ELEMENT = _role_choices("schemas/extensions/topology/topology_dc.yml", "Element")


def test_both_firewall_roles_are_choices_of_the_device_and_of_the_design_element() -> None:
    for choices in (_DEVICE, _ELEMENT):
        assert FIREWALL_ROLES <= choices


def test_every_role_of_a_bootstrap_design_element_is_a_choice() -> None:
    documents: list[dict[str, Any]] = [
        doc for doc in yaml.safe_load_all((ROOT / "data/bootstrap/14_design_elements.yml").read_text()) if doc
    ]
    roles = {
        row["role"]
        for doc in documents
        if doc["spec"]["kind"] == "TopologyElement"
        for row in doc["spec"]["data"]
        if "role" in row
    }
    assert roles and roles <= _ELEMENT, sorted(roles - _ELEMENT)
