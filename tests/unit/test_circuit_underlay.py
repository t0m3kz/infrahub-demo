"""Unit tests for utils/circuit_underlay.py: what carries a port (a physical circuit or a cable)."""

from __future__ import annotations

from typing import Any

import pytest

from utils.circuit_underlay import (
    HANDOFF_CIRCUIT,
    INTERCONNECT_CIRCUIT,
    SESSION_CIRCUIT_KINDS,
    TUNNEL_CIRCUIT,
    has_carrier,
    overlay_processes,
    port_cabled,
    subinterface_vlan,
)

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


def _bgp(role: str, process_id: str = "p") -> dict[str, Any]:
    """A cleaned ManagedBGP process of a device's ``capabilities``."""
    return {"typename": "ManagedBGP", "id": process_id, "process_role": role}


@pytest.mark.parametrize(
    ("capabilities", "ids"),
    [
        pytest.param([_bgp("overlay", "a"), _bgp("underlay", "b")], ["a"], id="one-overlay-among-others"),
        pytest.param([_bgp("overlay", "a"), _bgp("overlay", "b")], ["a", "b"], id="two-overlays"),
        pytest.param([_bgp("underlay", "a"), _bgp("regular", "b")], [], id="none"),
        pytest.param([{"typename": "ManagedOSPF", "process_role": "overlay"}], [], id="another-kind-is-no-process"),
        pytest.param(["not-a-dict", None, _bgp("overlay", "a")], ["a"], id="stray-entries-are-skipped"),
        pytest.param([], [], id="empty"),
        pytest.param(None, [], id="no-capabilities"),
    ],
)
def test_overlay_processes_are_the_managed_bgp_of_role_overlay(capabilities: Any, ids: list[str]) -> None:
    """The overlay BGP processes of a device: the check counts them, the generator picks the single one."""
    assert [process["id"] for process in overlay_processes({"capabilities": capabilities})] == ids


@pytest.mark.parametrize(
    ("name", "vlan"),
    [
        pytest.param("Ethernet1/10.1900", 1900, id="physical-parent"),
        pytest.param("et-0/0/0.3101", 3101, id="juniper"),
        pytest.param("eth0.7", 7, id="short"),
        pytest.param("a.b.100", 100, id="last-dot-wins"),
        pytest.param("Ethernet1/10", None, id="no-dot"),
        pytest.param("Ethernet1.10a", None, id="vlan-not-numeric"),
        pytest.param("Ethernet1.", None, id="empty-vlan"),
        pytest.param(".100", None, id="no-parent"),
        pytest.param("", None, id="empty-name"),
        pytest.param("?", None, id="unknown-name"),
    ],
)
def test_subinterface_vlan_parses_port_dot_vlan(name: str, vlan: int | None) -> None:
    """A handoff end is named <port>.<vlan>; anything else has no VLAN."""
    assert subinterface_vlan(name) == vlan


def test_the_session_kinds_are_the_interconnect_and_the_handoff_never_the_tunnel() -> None:
    """One source of truth for the kind names: a tunnel circuit has no session."""
    assert SESSION_CIRCUIT_KINDS == (INTERCONNECT_CIRCUIT, HANDOFF_CIRCUIT)
    assert TUNNEL_CIRCUIT not in SESSION_CIRCUIT_KINDS
    assert (INTERCONNECT_CIRCUIT, HANDOFF_CIRCUIT, TUNNEL_CIRCUIT) == (
        "TopologyInterconnectCircuit",
        "TopologyHandoffCircuit",
        "TopologyTunnelCircuit",
    )


def test_the_kind_names_are_those_of_the_protocol_classes() -> None:
    """The strings are the schema kinds the protocol classes name, so a rename fails here."""
    from generators.protocols import TopologyHandoffCircuit, TopologyInterconnectCircuit, TopologyTunnelCircuit

    assert HANDOFF_CIRCUIT == TopologyHandoffCircuit.__name__
    assert INTERCONNECT_CIRCUIT == TopologyInterconnectCircuit.__name__
    assert TUNNEL_CIRCUIT == TopologyTunnelCircuit.__name__
