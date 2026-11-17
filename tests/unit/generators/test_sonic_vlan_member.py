"""SONiC ConfigDB VLAN_MEMBER for host ports (templates/configs/common/sonic_common.j2)."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.smoke.fixture_builders.vrf import with_vrf_objects
from tests.unit.segment_port_helpers import raw_deployment_edges, raw_port_segments
from transforms.config.leaf import Leaf

_CONFIGS = Path(__file__).resolve().parents[2] / "smoke" / "configs"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_TAGGED = {"tagging_mode": "tagged"}
_UNTAGGED = {"tagging_mode": "untagged"}


def _load(name: str) -> dict[str, Any]:
    return json.loads((_CONFIGS / name / "input.json").read_text())


def _render(transform_cls: type, data: dict[str, Any]) -> dict[str, Any]:
    transform = transform_cls(client=MagicMock(), infrahub_node=MagicMock(), root_directory=str(_PROJECT_ROOT))
    return json.loads(asyncio.run(transform.transform(with_vrf_objects(transform, data))))


def _port(data: dict[str, Any], name: str) -> dict[str, Any]:
    edges = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    return next(e["node"] for e in edges if e["node"]["name"]["value"] == name)


@pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
def test_single_vlan_access_and_trunk_ports_are_distinguished(platform: str) -> None:
    """Dropping the second VLAN of the trunk turns it into an untagged access member."""
    data = _load(f"leaf_{platform}_ebgp_ibgp")
    port = _port(data, "Ethernet10")
    port["capabilities"]["edges"] = raw_deployment_edges(*raw_port_segments(port)[:1])
    configdb = _render(Leaf, data)
    assert list(configdb["VLAN_MEMBER"].values()) == [_UNTAGGED]


def test_no_vlan_on_any_port_renders_no_vlan_member_table() -> None:
    """Failure path: with no port carrying a VLAN the table is absent, not empty."""
    data = _load("leaf_sonic_ebgp_ibgp")
    _port(data, "Ethernet10")["capabilities"]["edges"] = []
    assert "VLAN_MEMBER" not in _render(Leaf, data)


def test_mlag_peer_link_membership_is_merged_not_replaced() -> None:
    """Host-port members and the Vlan4094 peer-link member coexist in one table."""
    data = _load("leaf_sonic_mlag")
    host = copy.deepcopy(_port(_load("leaf_sonic_ebgp_ibgp"), "Ethernet10"))
    node_interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    index = next(i for i, e in enumerate(node_interfaces) if e["node"]["name"]["value"] == "Ethernet10")
    # The segments' local VLAN is this device's own row: point the host fixture's rows at the MLAG fixture's device.
    own_id = data["DcimDevice"]["edges"][0]["node"]["id"]
    node_interfaces[index]["node"]["capabilities"] = json.loads(
        json.dumps(host["capabilities"]).replace(
            _load("leaf_sonic_ebgp_ibgp")["DcimDevice"]["edges"][0]["node"]["id"], own_id
        )
    )
    members = _render(Leaf, data)["VLAN_MEMBER"]
    assert members["Vlan4094|PortChannel100"] == _TAGGED
    assert members["Vlan100|Ethernet10"] == _TAGGED
    assert members["Vlan200|Ethernet10"] == _TAGGED
