"""Pure unit tests for ColocationFabricModel compiler across physical, virtual, and mixed flavors."""

from __future__ import annotations

from generators.model import ColocationFabricModel


def _phys_edge(interfaces: list[dict] | None = None) -> dict:
    return {
        "role": "edge",
        "quantity": 2,
        "template": {
            "id": "tpl-edge",
            "template_kind": "TemplateDcimPhysicalDevice",
            "interfaces": interfaces or [{"name": "et-0/0/1", "role": "firewall"}],
        },
    }


def _phys_fw() -> dict:
    return {
        "role": "internal-firewall",
        "quantity": 2,
        "template": {"id": "tpl-fw", "template_kind": "TemplateDcimPhysicalDevice"},
    }


def _virt_fw() -> dict:
    return {
        "role": "internal-firewall",
        "quantity": 2,
        "template": {"id": "tpl-vfw", "template_kind": "TemplateCloudNetworkAppliance"},
    }


def test_colocation_model_physical_flavor_filters_virtual_appliances() -> None:
    payload = {
        "id": "zone-1",
        "name": "COLO-FRA-Z1",
        "fabric_templates": [_phys_edge(), _phys_fw(), _virt_fw()],
    }
    model = ColocationFabricModel.compile(payload, flavor="physical", label="zone")
    assert model.effective_flavor == "physical"
    assert len(model.physical_templates) == 2
    assert len(model.virtual_templates) == 0
    assert len(model.warnings) == 1
    assert any("cannot host" in w for w in model.warnings)


def test_colocation_model_virtual_flavor_allows_appliances_without_edge_ports() -> None:
    payload = {
        "id": "vcolo-1",
        "name": "VCOLO-AWS-1",
        "fabric_templates": [_virt_fw()],
    }
    model = ColocationFabricModel.compile(payload, flavor="virtual", label="virtual colocation")
    assert model.effective_flavor == "virtual"
    assert len(model.virtual_templates) == 1
    assert len(model.physical_templates) == 0
    assert model.warnings == ()


def test_colocation_model_mixed_flavor_hosts_both_physical_and_virtual() -> None:
    payload = {
        "id": "zone-mix",
        "name": "COLO-AMS-MIX",
        "design": {"name": "COLO_STANDARD", "max_edges": 2, "max_firewalls": 4, "max_load_balancers": 2},
        "fabric_templates": [_phys_edge(), _phys_fw(), _virt_fw()],
    }
    model = ColocationFabricModel.compile(payload, flavor="mixed", label="zone")
    assert model.effective_flavor == "mixed"
    assert len(model.physical_templates) == 2
    assert len(model.virtual_templates) == 1
    assert model.warnings == ()


def test_colocation_model_enforces_design_caps_and_edge_port_roles() -> None:
    payload = {
        "id": "zone-cap",
        "name": "COLO-PAR-Z1",
        "design": {"name": "COLO_SMALL", "max_edges": 2, "max_firewalls": 1, "max_load_balancers": 0},
        "fabric_templates": [
            _phys_edge(interfaces=[{"name": "et-0/0/1", "role": "uplink"}]),
            _phys_fw(),
        ],
    }
    model = ColocationFabricModel.compile(payload, flavor="physical", label="zone")
    assert len(model.usable_templates) == 1
    assert any("no matching 'firewall'-role port" in w for w in model.warnings)
