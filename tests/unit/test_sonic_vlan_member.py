"""SONiC ConfigDB VLAN_MEMBER for host ports (templates/configs/common/sonic_common.j2).

A trunk host port is a tagged member of each of its VLANs, an access port an untagged
member of its single VLAN. The shared leaf template serves leaf, tor, access-leaf and
the border roles on SONiC and Dell SONiC; the l2-leaf template shares the same block.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from transforms.config.access_leaf import AccessLeaf
from transforms.config.border_leaf import BorderLeaf
from transforms.config.border_spine import BorderSpine
from transforms.config.l2_leaf import L2Leaf
from transforms.config.leaf import Leaf
from transforms.config.spine import Spine
from transforms.config.tor import ToR

_CONFIGS = Path(__file__).resolve().parents[1] / "smoke" / "configs"
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TRANSFORMS = {
    "leaf": Leaf,
    "tor": ToR,
    "access_leaf": AccessLeaf,
    "l2_leaf": L2Leaf,
    "border_leaf": BorderLeaf,
    "border_spine": BorderSpine,
}
_TAGGED = {"tagging_mode": "tagged"}
_UNTAGGED = {"tagging_mode": "untagged"}


def _load(name: str) -> dict[str, Any]:
    return json.loads((_CONFIGS / name / "input.json").read_text())


def _render(transform_cls: type, data: dict[str, Any]) -> dict[str, Any]:
    transform = transform_cls(client=MagicMock(), infrahub_node=MagicMock(), root_directory=str(_PROJECT_ROOT))
    return json.loads(asyncio.run(transform.transform(data)))


def _port(data: dict[str, Any], name: str) -> dict[str, Any]:
    edges = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    return next(e["node"] for e in edges if e["node"]["name"]["value"] == name)


@pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
@pytest.mark.parametrize("role", sorted(_TRANSFORMS))
def test_trunk_host_port_is_a_tagged_member_of_each_vlan(role: str, platform: str) -> None:
    """Ethernet10 carries VLANs 100 and 200: one tagged VLAN_MEMBER entry each."""
    name = f"{role}_{platform}_ebgp_ibgp"
    configdb = _render(_TRANSFORMS[role], _load(name))
    assert configdb["VLAN_MEMBER"] == {"Vlan100|Ethernet10": _TAGGED, "Vlan200|Ethernet10": _TAGGED}
    assert set(configdb["VLAN"]) >= {"Vlan100", "Vlan200"}


@pytest.mark.parametrize("platform", ["sonic", "dell_sonic"])
def test_access_host_port_is_an_untagged_member(platform: str) -> None:
    """A single-VLAN host port keeps its access rendering: untagged."""
    configdb = _render(Leaf, _load(f"leaf_{platform}_inline_segment"))
    assert configdb["VLAN_MEMBER"] == {"Vlan150|Ethernet10": _UNTAGGED}


def test_single_vlan_access_and_trunk_ports_are_distinguished() -> None:
    """Dropping the second VLAN of the trunk turns it into an untagged access member."""
    data = _load("leaf_sonic_ebgp_ibgp")
    port = _port(data, "Ethernet10")
    caps = port["capabilities"]["edges"]
    port["capabilities"]["edges"] = caps[:1]
    configdb = _render(Leaf, data)
    assert list(configdb["VLAN_MEMBER"].values()) == [_UNTAGGED]


def test_no_vlan_on_any_port_renders_no_vlan_member_table() -> None:
    """Failure path: with no port carrying a VLAN the table is absent, not empty."""
    data = _load("leaf_sonic_ebgp_ibgp")
    _port(data, "Ethernet10")["capabilities"]["edges"] = []
    assert "VLAN_MEMBER" not in _render(Leaf, data)


def test_spine_renders_no_vlan_member_table() -> None:
    """A spine has no host ports; its template does not grow the table."""
    assert "VLAN_MEMBER" not in _render(Spine, _load("spine_sonic_ebgp_ibgp"))


def test_mlag_peer_link_membership_is_merged_not_replaced() -> None:
    """Host-port members and the Vlan4094 peer-link member coexist in one table."""
    data = _load("leaf_sonic_mlag")
    host = copy.deepcopy(_port(_load("leaf_sonic_ebgp_ibgp"), "Ethernet10"))
    node_interfaces = data["DcimDevice"]["edges"][0]["node"]["interfaces"]["edges"]
    index = next(i for i, e in enumerate(node_interfaces) if e["node"]["name"]["value"] == "Ethernet10")
    # The segments' local VLAN is allocated per VLAN domain: this device's is its MLAG.
    node_interfaces[index]["node"]["capabilities"] = json.loads(
        json.dumps(host["capabilities"]).replace("dev-dc1-leaf-01", "mlag-dom-1")
    )
    members = _render(Leaf, data)["VLAN_MEMBER"]
    assert members["Vlan4094|PortChannel100"] == _TAGGED
    assert members["Vlan100|Ethernet10"] == _TAGGED
    assert members["Vlan200|Ethernet10"] == _TAGGED
