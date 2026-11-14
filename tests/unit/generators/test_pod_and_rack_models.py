"""Pure unit tests for PodFabricModel and RackFabricModel compilers (zero Infrahub I/O)."""

from __future__ import annotations

import pytest

from generators.model import PodFabricModel, RackFabricModel


def _pod_design(name: str = "POD_MIXED", *, max_spines: int = 2) -> dict:
    return {
        "name": name,
        "max_spines_per_pod": max_spines,
        "max_rows": 2,
        "max_network_racks_per_row": 2,
        "max_leafs_per_network_rack": 2,
        "max_compute_racks_per_row": 4,
        "max_tors_per_compute_rack": 2,
        "max_border_leafs_per_pod": 2,
    }


def _pod_payload(*, design_name: str = "POD_MIXED", requested_spines: int = 2) -> dict:
    pod_des = _pod_design(design_name)
    return {
        "id": "pod-1",
        "name": "DC6-1-POD-1",
        "index": 1,
        "design": pod_des,
        "fabric_templates": [
            {
                "role": "spine",
                "quantity": requested_spines,
                "template": {"interfaces": [{"name": "et-0/0/1", "role": "uplink"}]},
            }
        ],
        "parent": {
            "id": "dc-1",
            "name": "DC6",
            "index": 6,
            "underlay_protocol": "ipv6",
            "routing_strategy": "ebgp-ebgp",
            "children": [{"id": "pod-1"}],
            "devices": [{"name": "dc6-ss-01"}],
            "fabric_templates": [
                {"role": "super-spine", "quantity": 1, "template": {"interfaces": [{"name": "et-0/0/1"}]}}
            ],
            "design": {
                "name": "DC_LARGE",
                "max_pods": 4,
                "max_super_spines_per_fabric": 2,
                "max_border_leafs_per_fabric": 4,
                "max_hyper_spines_per_fabric": 0,
                "pod_templates": [_pod_design("POD_MIXED")],
            },
        },
    }


def test_pod_fabric_model_compiles_pools_and_uplink_mode() -> None:
    model = PodFabricModel.compile(_pod_payload())
    assert model.pod_slug == "dc6-1-pod-1"
    assert model.fabric_name == "dc6"
    assert model.spine_role == "spine"
    assert model.spine_count == 2
    assert model.mesh_cabling is False
    assert set(model.pools_to_allocate) == {"technical", "loopback"}


def test_pod_fabric_model_rejects_disallowed_pod_design() -> None:
    with pytest.raises(ValueError, match="not one of DC DC6's design"):
        PodFabricModel.compile(_pod_payload(design_name="POD_UNKNOWN"))


def test_rack_fabric_model_validates_roles_and_computes_offsets() -> None:
    rack_data = {
        "id": "rack-2",
        "name": "RACK-02",
        "index": 2,
        "row_index": 2,
        "deployment": {"design": _pod_design()},
    }
    valid = RackFabricModel.compile(rack_data, {"tor"})
    assert valid.role_errors == ()
    assert valid.cabling_offset(_pod_design(), device_count=2, device_type="tor", racks_in_previous_rows=4) > 0

    invalid = RackFabricModel.compile(rack_data, {"tor", "l2_leaf"})
    assert any("mutually exclusive" in err for err in invalid.role_errors)
