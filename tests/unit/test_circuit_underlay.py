"""Unit tests for utils/circuit_underlay.py: what carries a port (a physical circuit or a cable)."""

from __future__ import annotations

from typing import Any

import pytest

from utils.circuit_underlay import has_carrier, port_cabled

CIRCUIT = {"typename": "TopologyPhysicalCircuit", "id": "pc-1", "status": "decommissioned"}
OTHER = {"typename": "ManagedVxlanSegment"}


@pytest.mark.parametrize(
    ("port", "cabled"),
    [
        pytest.param({"cable": "cable-1"}, True, id="cable-on-the-port"),
        pytest.param({"cable": None}, False, id="no-cable-on-the-port"),
        pytest.param({}, False, id="no-cable-selection"),
        pytest.param({"parent_interface": {"cable": "cable-1"}}, True, id="cable-on-the-parent"),
        pytest.param({"parent_interface": {"cable": None}}, False, id="no-cable-on-the-parent"),
        pytest.param({"parent_interface": None}, False, id="no-parent"),
    ],
)
def test_port_cabled_reads_the_port_and_its_parent(port: dict[str, Any], cabled: bool) -> None:
    """A cable at the port, or at the parent of a sub-interface, cables the port."""
    assert port_cabled(port) is cabled


@pytest.mark.parametrize(
    ("port", "carried"),
    [
        pytest.param({"capabilities": [CIRCUIT]}, True, id="circuit-any-status"),
        pytest.param({"parent_interface": {"capabilities": [CIRCUIT]}}, True, id="circuit-on-the-parent"),
        pytest.param({"cable": "cable-1", "capabilities": []}, True, id="cable-only"),
        pytest.param({"parent_interface": {"cable": "cable-1", "capabilities": []}}, True, id="cable-on-the-parent"),
        pytest.param({"cable": None, "capabilities": [OTHER]}, False, id="a-segment-is-no-carrier"),
        pytest.param({"capabilities": []}, False, id="nothing"),
    ],
)
def test_has_carrier_is_a_circuit_or_a_cable(port: dict[str, Any], carried: bool) -> None:
    """A port is carried by a physical circuit (whatever its status) or a cable, at itself or its parent."""
    assert has_carrier(port) is carried
