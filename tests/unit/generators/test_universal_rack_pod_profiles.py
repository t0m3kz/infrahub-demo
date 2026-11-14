"""Unit tests for universal standardized profiles and direct individual switch inventory."""

from __future__ import annotations

from generators.helpers.rack import normalize_pod_templates, normalize_rack_templates, parse_rack_data
from generators.model.pod_model import PodFabricModel


def test_normalize_rack_templates_uses_typed_profile_and_quantity_override() -> None:
    rack = {
        "id": "rack-1",
        "name": "MUC-U1-SUITE-1-R1-5",
        "index": 5,
        "row_index": 1,
        "rack_type": "network",
        "switch_quantity": 4,
        "switch_template": {"id": "tpl-fx3", "interfaces": []},
        "deployment": {
            "id": "pod-1",
            "name": "DC14-14-POD-1",
            "index": 1,
            "spine_quantity": 2,
            "spine_template": {"id": "tpl-fx2", "interfaces": [{"name": "Ethernet1/1", "role": "downlink"}]},
        },
    }
    normalized = normalize_rack_templates(rack)
    assert len(normalized["leafs"]) == 1
    assert normalized["leafs"][0]["quantity"] == 4
    assert normalized["leafs"][0]["template"]["id"] == "tpl-fx3"
    assert normalized["deployment"]["fabric_templates"][0]["role"] == "spine"
    assert normalized["deployment"]["fabric_templates"][0]["quantity"] == 2


def test_normalize_rack_and_pod_discovers_direct_individual_devices() -> None:
    raw = {
        "LocationRack": {
            "edges": [
                {
                    "node": {
                        "id": "rack-2",
                        "name": {"value": "BER-U1-SUITE-1-R1-5"},
                        "index": {"value": 5},
                        "row_index": {"value": 1},
                        "rack_type": {"value": "network"},
                        "devices": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "dev-1",
                                        "name": {"value": "custom-leaf-1"},
                                        "role": {"value": "leaf"},
                                    }
                                },
                                {
                                    "node": {
                                        "id": "dev-2",
                                        "name": {"value": "custom-leaf-2"},
                                        "role": {"value": "leaf"},
                                    }
                                },
                            ]
                        },
                        "deployment": {
                            "node": {
                                "id": "pod-2",
                                "name": {"value": "DC15-15-POD-1"},
                                "index": {"value": 1},
                                "devices": {
                                    "edges": [
                                        {
                                            "node": {
                                                "id": "sp-1",
                                                "name": {"value": "custom-spine-1"},
                                                "role": {"value": "spine"},
                                            }
                                        },
                                        {
                                            "node": {
                                                "id": "sp-2",
                                                "name": {"value": "custom-spine-2"},
                                                "role": {"value": "spine"},
                                            }
                                        },
                                    ]
                                },
                            }
                        },
                    }
                }
            ]
        }
    }
    parsed = parse_rack_data(raw)
    assert len(parsed["leafs"]) == 1
    assert parsed["leafs"][0]["quantity"] == 2
    assert [d["name"] for d in parsed["leafs"][0]["devices"]] == ["custom-leaf-1", "custom-leaf-2"]
    pod_spines = parsed["deployment"]["fabric_templates"]
    assert len(pod_spines) == 1
    assert pod_spines[0]["role"] == "spine"
    assert pod_spines[0]["quantity"] == 2


def test_pod_fabric_model_compiles_with_typed_spine_profile() -> None:
    pod_data = {
        "id": "pod-14",
        "name": "DC14-14-POD-1",
        "index": 1,
        "spine_role": "spine",
        "spine_quantity": 2,
        "spine_template": {
            "id": "tpl-spine",
            "interfaces": [
                {"name": "Ethernet1/1", "role": "uplink"},
                {"name": "Ethernet1/2", "role": "downlink"},
            ],
        },
        "design": {
            "name": "M_MIDDLE",
            "max_rows": 4,
            "max_network_racks_per_row": 1,
            "max_compute_racks_per_row": 8,
            "max_spines_per_pod": 4,
            "max_tors_per_compute_rack": 0,
            "max_leafs_per_network_rack": 2,
            "max_border_leafs_per_pod": 2,
        },
        "parent": {
            "id": "dc-14",
            "name": "DC14",
            "index": 14,
            "design": {
                "name": "M",
                "max_pods": 4,
                "max_super_spines_per_fabric": 4,
                "max_border_leafs_per_fabric": 2,
                "pod_templates": [{"name": "M_MIDDLE", "max_spines_per_pod": 4}],
            },
            "children": [],
            "devices": [],
            "fabric_templates": [],
        },
    }
    model = PodFabricModel.compile(normalize_pod_templates(pod_data))
    assert model.spine_role == "spine"
    assert model.spine_count == 2
    assert model.spine_uplink_interfaces == ("Ethernet1/1",)
