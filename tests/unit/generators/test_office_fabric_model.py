"""Pure unit tests for OfficeFabricModel compiler (zero Infrahub I/O)."""

from __future__ import annotations

from generators.model import OfficeFabricModel


def test_office_fabric_model_compiles_valid_templates() -> None:
    payload = {
        "id": "off-1",
        "name": "C900-OFFICE",
        "fabric_templates": [
            {"role": "distribution-switch", "quantity": 2, "template": {"template_kind": "TemplateDcimPhysicalDevice"}},
            {"role": "access-switch", "quantity": 4, "template": {"template_kind": "TemplateDcimPhysicalDevice"}},
        ],
    }
    model = OfficeFabricModel.compile(payload)
    assert model.is_requested is True
    assert model.fabric_name == "c900-office"
    assert model.distribution_quantity == 2
    assert model.access_quantity == 4
    assert model.warnings == ()


def test_office_fabric_model_emits_warnings_for_invalid_entries() -> None:
    payload = {
        "id": "off-1",
        "name": "C900-OFFICE",
        "fabric_templates": [
            {"role": "spine", "quantity": 2, "template": {"template_kind": "TemplateDcimPhysicalDevice"}},
            {"role": "access-switch", "quantity": 0, "template": {"template_kind": "TemplateDcimPhysicalDevice"}},
            {"role": "distribution-switch", "quantity": 2, "template": {"template_kind": "TemplateDcimVirtualDevice"}},
        ],
    }
    model = OfficeFabricModel.compile(payload)
    assert model.is_requested is False
    assert len(model.warnings) == 3
