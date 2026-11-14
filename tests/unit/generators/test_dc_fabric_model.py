"""Pure unit tests for DcFabricModel compiler (zero Infrahub SDK I/O)."""

from __future__ import annotations

import pytest

from generators.model import DcFabricModel


def _sample_dc_data(*, max_super_spines: int = 2, requested_super_spines: int = 2, strategy: str = "ebgp-ebgp") -> dict:
    return {
        "id": "dc-1",
        "name": "DC6",
        "index": 6,
        "underlay_protocol": "ipv6",
        "routing_strategy": strategy,
        "fabric_templates": [
            {"role": "super-spine", "quantity": requested_super_spines},
            {"role": "border-leaf", "quantity": 2},
        ],
        "design": {
            "name": "large-dc",
            "max_pods": 4,
            "max_super_spines_per_fabric": max_super_spines,
            "max_border_leafs_per_fabric": 4,
            "max_hyper_spines_per_fabric": 0,
            "pod_templates": [
                {
                    "max_spines_per_pod": 2,
                    "max_rows": 2,
                    "max_network_racks_per_row": 2,
                    "max_leafs_per_network_rack": 2,
                    "max_compute_racks_per_row": 2,
                    "max_tors_per_compute_rack": 2,
                    "max_border_leafs_per_pod": 2,
                }
            ],
        },
    }


def test_dc_fabric_model_compiles_pools_asns_and_roles() -> None:
    model = DcFabricModel.compile(_sample_dc_data())
    assert model.fabric_name == "dc6"
    assert model.design_mode == "super-spine"
    assert set(model.pools_to_allocate) == {"technical", "loopback", "management", "dc-fabric-loopback"}
    assert model.pools_to_allocate["management"] < 32
    assert model.pools_to_allocate["technical"] < 128
    assert model.requires_asn_pool is True
    asn_start, asn_end = model.asn_range
    assert asn_start < asn_end
    assert model.role_quantities == {"super-spine": 2, "border-leaf": 2}


def test_dc_fabric_model_back_to_back_and_ospf_skips_asn_range() -> None:
    model = DcFabricModel.compile(_sample_dc_data(max_super_spines=0, requested_super_spines=0, strategy="ospf-ibgp"))
    assert model.design_mode == "back-to-back"
    assert model.requires_asn_pool is False


def test_dc_fabric_model_rejects_excess_super_spines() -> None:
    with pytest.raises(RuntimeError, match="requests 4 super-spines"):
        DcFabricModel.compile(_sample_dc_data(max_super_spines=2, requested_super_spines=4))


def test_dc_fabric_model_rejects_invalid_role() -> None:
    bad = _sample_dc_data()
    bad["fabric_templates"] = [{"role": "not-a-valid-role", "quantity": 1}]
    with pytest.raises(ValueError, match="invalid fabric template role"):
        DcFabricModel.compile(bad)
