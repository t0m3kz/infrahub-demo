"""A TopologyPhysicalCircuit in a port's capabilities must not change a rendered config.

TopologyPhysicalCircuit is a ManagedGenericInterfaces since step 5 of the caps series, so every circuit end
port lists its circuit next to the OSPF/segment/firewall capabilities. The shared fragments select only
``__typename`` (and ``id``) for kinds without their own fragment, exactly as a TopologyVirtualCircuit already
appears today. Each smoke fixture below is rendered with such a circuit added to every device interface and
must match its golden output byte for byte.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tests.smoke.fixture_builders.common import run_transform
from tests.smoke.test_configs_smoke import CONFIGS_DIR
from transforms.config.border_leaf import BorderLeaf
from transforms.config.edge import Edge

_CIRCUIT = {"node": {"__typename": "TopologyPhysicalCircuit", "id": "pc-1"}}


def _with_circuit_on_every_port(data: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """A copy of the fixture with the circuit appended to each device interface's capabilities; the count."""
    data = deepcopy(data)
    injected = 0
    for device in data["DcimDevice"]["edges"]:
        for interface in device["node"]["interfaces"]["edges"]:
            interface["node"]["capabilities"]["edges"].append(deepcopy(_CIRCUIT))
            injected += 1
    return data, injected


@pytest.mark.parametrize(
    ("transform_cls", "fixture"),
    [
        pytest.param(Edge, "edge_cisco_nxos_internet_bgw", id="edge"),
        pytest.param(BorderLeaf, "border_leaf_cisco_nxos_bgw_stretched", id="border-leaf"),
    ],
)
def test_a_physical_circuit_on_every_port_leaves_the_config_unchanged(transform_cls: type, fixture: str) -> None:
    """The typename filters of the transforms skip a physical circuit, so the render equals the golden output."""
    directory: Path = CONFIGS_DIR / fixture
    data, injected = _with_circuit_on_every_port(json.loads((directory / "input.json").read_text()))

    assert injected >= 5
    assert run_transform(transform_cls, data) == (directory / "output.txt").read_text()
