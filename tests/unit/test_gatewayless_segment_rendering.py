"""A VXLAN segment without a gateway is the infrastructure VLAN: the fabric renders it pure L2.

A transfer or management L2 link is a ManagedVxlanSegment with no ``gateway`` (AGENTS.md, "Segments are VXLAN only"): the
leaf declares its VLAN and maps it to its L2 VNI, the port carries it, and nothing L3 is rendered for it (no SVI, no VRF
member, no ACL).
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from transforms.config.leaf import Leaf

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "tests" / "smoke" / "configs" / "leaf_cisco_nxos_ebgp_ibgp" / "input.json"
_GATEWAYLESS_VLAN = 200
_ROUTED_VLAN = 100


def _without_the_second_gateway(data: dict[str, Any]) -> dict[str, Any]:
    """The fixture leaf with the segment of VLAN 200 stripped of its gateway, wherever the payload carries it."""
    data = copy.deepcopy(data)
    gateway = "10.200.0.1/24"

    def strip(node: Any) -> None:
        if isinstance(node, dict):
            if ((node.get("gateway") or {}).get("node") or {}).get("address", {}).get("value") == gateway:
                node["gateway"] = {"node": None}
            for value in node.values():
                strip(value)
        elif isinstance(node, list):
            for value in node:
                strip(value)

    strip(data)
    return data


@pytest.fixture(scope="module")
def rendered() -> str:
    data = _without_the_second_gateway(json.loads(_FIXTURE.read_text()))
    transform = Leaf(client=MagicMock(), infrahub_node=MagicMock(), root_directory=str(_ROOT))
    return asyncio.run(transform.transform(data))


def test_the_vlan_is_declared_and_mapped_to_its_l2_vni(rendered: str) -> None:
    assert re.search(rf"^vlan {_GATEWAYLESS_VLAN}\n", rendered, re.MULTILINE)
    assert re.search(rf"^vlan {_GATEWAYLESS_VLAN}\n  vn-segment 10200$", rendered, re.MULTILINE)


def test_no_svi_is_rendered_for_it(rendered: str) -> None:
    assert f"interface Vlan{_GATEWAYLESS_VLAN}" not in rendered


def test_the_port_still_carries_it(rendered: str) -> None:
    assert f"switchport trunk allowed vlan {_ROUTED_VLAN},{_GATEWAYLESS_VLAN}" in rendered


def test_the_routed_segment_keeps_its_svi(rendered: str) -> None:
    """Only the segment without a gateway lost the L3 side."""
    assert f"interface Vlan{_ROUTED_VLAN}" in rendered
