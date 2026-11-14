"""Unit tests for rack-first physical placement and role-compatibility by rack_type."""

from __future__ import annotations

import pytest

from generators.helpers.rack import (
    normalize_dc_templates,
    normalize_pod_templates,
    normalize_rack_templates,
)


def test_dc_discovers_super_spines_and_multi_role_border_rack_with_rack_ids() -> None:
    dc = {
        "id": "dc-1",
        "name": "DC1",
        "racks": [
            {
                "id": "rack-core-1",
                "name": "CORE-R1-1",
                "row_index": 1,
                "index": 1,
                "rack_type": "network",
                "fabric_templates": [
                    {"role": "super-spine", "quantity": 2, "template": {"id": "tpl-ss"}},
                ],
            },
            {
                "id": "rack-border-1",
                "name": "BORDER-R1-2",
                "row_index": 1,
                "index": 2,
                "rack_type": "network",
                "fabric_templates": [
                    {"role": "border-leaf", "quantity": 2, "template": {"id": "tpl-bl"}},
                    {"role": "internal-firewall", "quantity": 2, "template": {"id": "tpl-fw"}},
                    {"role": "load-balancer", "quantity": 2, "template": {"id": "tpl-lb"}},
                ],
            },
        ],
    }
    normalized = normalize_dc_templates(dc)
    entries = normalized["fabric_templates"]
    by_role = {e["role"]: e for e in entries}
    assert set(by_role) == {"super-spine", "border-leaf", "internal-firewall", "load-balancer"}
    assert by_role["super-spine"]["rack_id"] == "rack-core-1"
    assert by_role["border-leaf"]["rack_id"] == "rack-border-1"
    assert by_role["internal-firewall"]["rack_id"] == "rack-border-1"
    assert by_role["load-balancer"]["rack_id"] == "rack-border-1"


def test_pod_discovers_spines_from_pod_network_rack_with_rack_id() -> None:
    pod = {
        "id": "pod-1",
        "name": "DC1-1-POD-1",
        "racks": [
            {
                "id": "rack-spine-1",
                "name": "SPINE-R1-1",
                "row_index": 1,
                "index": 1,
                "rack_type": "network",
                "devices": [
                    {
                        "id": "sp-1",
                        "name": "dc1-1-pod-1-spine-1",
                        "role": "spine",
                        "interfaces": [{"name": "Ethernet1/1", "role": "uplink"}],
                    },
                    {
                        "id": "sp-2",
                        "name": "dc1-1-pod-1-spine-2",
                        "role": "spine",
                        "interfaces": [{"name": "Ethernet1/1", "role": "uplink"}],
                    },
                ],
            }
        ],
    }
    normalized = normalize_pod_templates(pod)
    entries = normalized["fabric_templates"]
    assert len(entries) == 1
    assert entries[0]["role"] == "spine"
    assert entries[0]["quantity"] == 2
    assert entries[0]["rack_id"] == "rack-spine-1"


def test_compute_rack_rejects_super_spine_and_spine_roles() -> None:
    with pytest.raises(ValueError, match="cannot host role"):
        normalize_rack_templates(
            {
                "id": "rack-comp-1",
                "name": "COMP-R1-3",
                "rack_type": "compute",
                "fabric_templates": [{"role": "super-spine", "quantity": 2, "template": {"id": "tpl-ss"}}],
            }
        )
