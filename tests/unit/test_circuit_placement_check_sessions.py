"""Unit tests for CheckCircuitPlacement session rules: one session circuit per port, environment separation and
decommissioned session circuits.

The payload builders are in tests/unit/circuit_placement_check_helpers.py; the placement rules per circuit kind are
in tests/unit/test_circuit_placement_check.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.circuit_placement_check_helpers import (
    AWS_VLAN,
    AWS_VLAN_NO_CARRIER,
    BL10,
    C001_OFFICE,
    DC10,
    DCI,
    DF10,
    EG_FR_DF10,
    EG_FR_VLAN,
    EUC1,
    FR2,
    GW_PUBLIC,
    HANDOFF,
    INET_GW,
    INET_OFFICE,
    OFFICE_EDGE,
    TUNNEL,
    XC,
    _handoff,
    _interconnect,
    _run,
    _tunnel,
    _vport,
)


class TestSessionPorts:
    """A port is an end of at most one Interconnect or Handoff circuit; a tunnel is exempt."""

    def test_two_handoffs_on_the_same_pair_of_ports_are_one_finding(self) -> None:
        """Two handoffs on the same two ports give one finding naming both circuits, not one per port."""
        other = _handoff("HO-B", None, [EG_FR_VLAN, AWS_VLAN])
        messages = _run(handoffs=[HANDOFF, other])
        assert messages == [
            "Circuits 'HO-B', 'HO-C005-P-AWS-EUC1' all terminate on 'EXT-AWS-DX-FR2:et-0/0/0.3101', "
            "'eg-fr201:Ethernet1/13.3101': a port is an end of at most one Interconnect or Handoff circuit, one "
            "session per address."
        ]

    def test_two_handoffs_sharing_one_port_fail(self) -> None:
        """Sharing a single port is enough: one port carries one session."""
        other_stub = _vport("EXT-AWS-DX-FR2", "et-0/0/1.3101", parent=("et-0/0/1", (XC,)))
        messages = _run(handoffs=[HANDOFF, _handoff("HO-B", None, [EG_FR_VLAN, other_stub])])
        assert len(messages) == 1
        assert "all terminate on 'eg-fr201:Ethernet1/13.3101'" in messages[0]

    def test_an_interconnect_and_a_handoff_sharing_a_port_fail(self) -> None:
        """The one-session-per-port rule spans both kinds: a DCI and a handoff on one port conflict."""
        handoff = _handoff("HO-B", None, [_vport("eg-fr201", "Ethernet1/10", (DF10,)), AWS_VLAN])
        messages = [m for m in _run(interconnects=[DCI], handoffs=[handoff]) if "all terminate on" in m]
        assert len(messages) == 1
        assert "'DCI-DC10-FR2', 'HO-B' all terminate on 'eg-fr201:Ethernet1/10'" in messages[0]

    def test_handoffs_on_different_vlans_of_one_port_pass(self) -> None:
        """Sub-interfaces are ports of their own: a cage port carries one handoff per VLAN."""
        edge = _vport("eg-fr201", "Ethernet1/13.3102", parent=("Ethernet1/13", (XC,)))
        stub = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        assert _run(handoffs=[HANDOFF, _handoff("HO-B", None, [edge, stub])]) == []


class TestEnvironmentSeparation:
    """Circuits with a port on the same device are of one environment class: prod never meets non-prod."""

    @staticmethod
    def _on_edge(name: str, namespace: tuple[str, str | None] | None, vlan: int = 3101) -> dict[str, Any]:
        """A handoff on the cage edge eg-fr201 (VLAN ``vlan``) towards its own stub."""
        edge = _vport("eg-fr201", f"Ethernet1/13.{vlan}", parent=("Ethernet1/13", (XC,)))
        stub = _vport(f"EXT-{name}", f"et-0/0/0.{vlan}", parent=("et-0/0/0", (XC,)))
        return _handoff(name, None, [edge, stub], namespace=namespace)

    def test_handoffs_of_one_class_sharing_an_edge_pass(self) -> None:
        """Two prod handoffs (VLANs 3101 and 3102) on one edge are the same environment class."""
        circuits = [self._on_edge("HO-A", ("PROD", "prod"), 3101), self._on_edge("HO-B", ("PROD", "prod"), 3102)]
        assert _run(handoffs=circuits) == []

    def test_prod_and_non_prod_on_one_device_fail_once(self) -> None:
        """One finding for the device, naming it and both circuits with their namespaces, whatever the order."""
        prod = self._on_edge("HO-P", ("PROD", "prod"), 3101)
        non_prod = self._on_edge("HO-N", ("NON-PROD", "non_prod"), 3102)
        for circuits in ([prod, non_prod], [non_prod, prod]):
            messages = _run(handoffs=circuits)
            assert len(messages) == 1
            assert "Device 'eg-fr201' carries virtual circuits of different environments" in messages[0]
            assert "'HO-P' (namespace 'PROD', prod)" in messages[0]
            assert "'HO-N' (namespace 'NON-PROD', non_prod)" in messages[0]

    def test_conflict_among_many_circuits_is_one_finding_per_device(self) -> None:
        """Three circuits on one device with prod and non-prod among them give a single finding for the device."""
        circuits = [
            self._on_edge("HO-P1", ("PROD", "prod"), 3101),
            self._on_edge("HO-P2", ("PROD", "prod"), 3102),
            self._on_edge("HO-N", ("NON-PROD", "non_prod"), 3103),
        ]
        assert len(_run(handoffs=circuits)) == 1

    def test_each_conflicting_device_is_reported(self) -> None:
        """A prod and a non-prod handoff sharing both of their devices: one finding for each device."""
        gateway = _vport("EQX-FR2-SDWAN-GW1", "eth2.3101", parent=("eth2", (XC,)))
        edge = _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
        prod = _handoff("HO-P", None, [edge, gateway], namespace=("PROD", "prod"))
        non_prod = _handoff("HO-N", None, [edge, gateway], namespace=("NON-PROD", "non_prod"))
        messages = _run(handoffs=[prod, non_prod])
        env_messages = [message for message in messages if "different environments" in message]
        assert sorted(message.split("'")[1] for message in env_messages) == ["EQX-FR2-SDWAN-GW1", "eg-fr201"]

    @pytest.mark.parametrize(
        ("namespace", "expected"),
        [(("PROD", "prod"), 0), (("NON-PROD", "non_prod"), 1)],
        ids=["tunnel-meets-prod", "tunnel-meets-non-prod"],
    )
    def test_tunnel_counts_as_prod(self, namespace: tuple[str, str], expected: int) -> None:
        """An office is always PROD: its tunnel meets a prod handoff on the gateway, conflicts with a non-prod one."""
        edge = _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
        gateway = _vport("EQX-FR2-SDWAN-GW1", "eth2.3101", parent=("eth2", (XC,)))
        handoff = _handoff("HO", None, [edge, gateway], namespace=namespace)
        messages = [message for message in _run(tunnels=[TUNNEL], handoffs=[handoff]) if "environments" in message]
        assert len(messages) == expected
        if expected:
            assert "Device 'EQX-FR2-SDWAN-GW1'" in messages[0]
            assert "'C001-OFFICE-SDWAN-FR2' (office 'C001-OFFICE', prod)" in messages[0]

    @pytest.mark.parametrize(
        "namespace",
        [("INTERNET", "internet"), ("MANAGEMENT", "management"), ("X", None), None],
        ids=["internet", "management", "untyped", "no-namespace"],
    )
    def test_a_handoff_in_a_namespace_with_no_tenant_class_is_skipped(
        self, namespace: tuple[str, str | None] | None
    ) -> None:
        """Internet, management, untyped or absent namespaces state no environment class, so they never conflict."""
        prod = self._on_edge("HO-P", ("PROD", "prod"), 3101)
        other = self._on_edge("HO-X", namespace, 3102)
        assert _run(handoffs=[prod, other]) == []

    def test_circuits_alone_on_their_devices_pass(self) -> None:
        """A non-prod handoff on its own edge does not meet the prod one on eg-fr201."""
        edge = _vport("eg-pa401", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
        stub = _vport("EXT-NONPROD-PA", "et-0/0/0.3101", parent=("et-0/0/0", (XC,)))
        non_prod = _handoff("HO-N", None, [edge, stub], namespace=("NON-PROD", "non_prod"))
        assert _run(handoffs=[self._on_edge("HO-P", ("PROD", "prod")), non_prod]) == []

    def test_interconnect_states_no_class(self) -> None:
        """A DCI runs in the underlay: a non-prod handoff on the same edge does not conflict with it."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, EG_FR_DF10])
        edge = _vport("eg-fr201", "Ethernet1/10.3101", parent=("Ethernet1/10", (DF10,)))
        stub = _vport("EXT-NONPROD", "et-0/0/0.3101", parent=("et-0/0/0", (XC,)))
        handoff = _handoff("HO-N", None, [edge, stub], namespace=("NON-PROD", "non_prod"))
        messages = _run(interconnects=[circuit], handoffs=[handoff])
        assert not any("environments" in message for message in messages)


class TestDecommissionedSessionCircuits:
    """add_virtual_circuit builds nothing for a decommissioned session circuit, so the check does not enforce the
    session rules on it; the same defect on an active circuit is still reported."""

    @staticmethod
    def _uncarried_dci(status: str) -> dict[str, Any]:
        """A DCI whose far end is on no physical circuit."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, _vport("eg-fr201", "Ethernet1/10")])
        return {**circuit, "status": {"value": status}}

    @staticmethod
    def _broken_chain_dci(status: str) -> dict[str, Any]:
        """A DCI whose carrier stops at FR2, short of the region."""
        circuit = _interconnect("DCI", [DC10, EUC1], [BL10, EG_FR_DF10])
        return {**circuit, "status": {"value": status}}

    @staticmethod
    def _bad_handoff(name: str, status: str, ends: list[dict[str, Any]]) -> dict[str, Any]:
        """A handoff in FR2 with the given ends and status, for defects the check must or must not report."""
        return {**_handoff(name, None, ends), "status": {"value": status}}

    def test_decommissioned_interconnect_with_an_uncarried_end_is_not_reported(self) -> None:
        """A decommissioned DCI is not built, so an uncarried end on it is not a finding."""
        assert _run(interconnects=[self._uncarried_dci("decommissioned")]) == []

    def test_decommissioned_interconnect_with_a_non_contiguous_carrier_is_not_reported(self) -> None:
        """A decommissioned DCI is not built, so a broken carrier chain on it is not a finding."""
        assert _run(interconnects=[self._broken_chain_dci("decommissioned")]) == []

    def test_decommissioned_interconnect_with_other_defects_is_not_reported(self) -> None:
        """Owned, one end port and no overlay process: nothing is reported for a circuit nothing builds."""
        circuit = _interconnect("DCI", [DC10], [_vport("bl-dc101101", "Ethernet1/35", (DF10,), bgp=())], owner="C001")
        assert _run(interconnects=[{**circuit, "status": {"value": "decommissioned"}}]) == []

    def test_active_interconnect_with_the_same_defects_is_reported(self) -> None:
        """The same two defects on an active DCI are findings, so the exemption is by status only."""
        uncarried = _run(interconnects=[self._uncarried_dci("active")])
        assert len(uncarried) == 1
        assert "end 'eg-fr201:Ethernet1/10' is on no live physical circuit" in uncarried[0]
        chain = _run(interconnects=[self._broken_chain_dci("active")])
        assert len(chain) == 1
        assert "is not one contiguous path" in chain[0]

    def test_decommissioned_handoff_with_no_carrier_is_not_reported(self) -> None:
        """A decommissioned handoff is not built, so a missing carrier is not a finding."""
        ends = [_vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        assert _run(handoffs=[self._bad_handoff("HO", "decommissioned", ends)]) == []

    def test_decommissioned_handoff_with_mismatched_vlans_is_not_reported(self) -> None:
        """A decommissioned handoff is not built, so differing VLANs (3101, 3102) are not a finding."""
        other = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        assert _run(handoffs=[self._bad_handoff("HO", "decommissioned", [EG_FR_VLAN, other])]) == []

    def test_active_handoff_with_the_same_defects_is_reported(self) -> None:
        """The same two defects on an active handoff are findings, so the exemption is by status only."""
        no_carrier = [_vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        messages = _run(handoffs=[self._bad_handoff("HO", "active", no_carrier)])
        assert len(messages) == 1
        assert "has no live physical circuit and no cable" in messages[0]
        other = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        messages = _run(handoffs=[self._bad_handoff("HO", "active", [EG_FR_VLAN, other])])
        assert len(messages) == 1
        assert "ends carry different VLANs" in messages[0]

    @pytest.mark.parametrize("live_first", [True, False], ids=["live-first", "dead-first"])
    def test_decommissioned_circuit_sharing_ports_with_a_live_one_is_not_reported(self, live_first: bool) -> None:
        """The dead circuit holds no port: neither it nor the live circuit gets the one-session-per-port finding."""
        dead = self._bad_handoff("HO-OLD", "decommissioned", [EG_FR_VLAN, AWS_VLAN])
        circuits = [HANDOFF, dead] if live_first else [dead, HANDOFF]
        assert _run(handoffs=circuits) == []

    def test_two_active_circuits_sharing_ports_are_still_reported(self) -> None:
        """Two live circuits on the same ports still conflict."""
        live = self._bad_handoff("HO-OLD", "active", [EG_FR_VLAN, AWS_VLAN])
        messages = _run(handoffs=[HANDOFF, live])
        assert len(messages) == 1
        assert "all terminate on" in messages[0]

    def test_decommissioned_interconnect_sharing_a_port_with_a_live_handoff_is_not_reported(self) -> None:
        """A dead DCI holds no port, so the live handoff on the same port is not reported."""
        handoff = _handoff("HO-B", None, [_vport("eg-fr201", "Ethernet1/10", (DF10,)), AWS_VLAN])
        dead = {**DCI, "status": {"value": "decommissioned"}}
        assert not any("all terminate on" in m for m in _run(interconnects=[dead], handoffs=[handoff]))

    def test_decommissioned_prod_handoff_meeting_a_live_non_prod_one_is_not_reported(self) -> None:
        """A dead prod handoff meets nothing, so the live non-prod one on the same device is not a conflict."""
        prod = TestEnvironmentSeparation._on_edge("HO-P", ("PROD", "prod"), 3101)
        non_prod = TestEnvironmentSeparation._on_edge("HO-N", ("NON-PROD", "non_prod"), 3102)
        dead = {**prod, "status": {"value": "decommissioned"}}
        assert _run(handoffs=[dead, non_prod]) == []

    def test_active_prod_handoff_meeting_a_non_prod_one_is_still_reported(self) -> None:
        """With an explicit active status the prod and non-prod handoffs on one device still conflict."""
        prod = {**TestEnvironmentSeparation._on_edge("HO-P", ("PROD", "prod"), 3101), "status": {"value": "active"}}
        non_prod = TestEnvironmentSeparation._on_edge("HO-N", ("NON-PROD", "non_prod"), 3102)
        messages = _run(handoffs=[prod, non_prod])
        assert len(messages) == 1
        assert "different environments" in messages[0]

    def test_decommissioned_tunnel_is_still_validated(self) -> None:
        """A tunnel is an inventory record no generator reads the status of: not exempt."""
        tunnel = {
            **_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, GW_PUBLIC], owner=None),
            "status": {"value": "decommissioned"},
        }
        messages = _run(tunnels=[tunnel])
        assert len(messages) == 1
        assert "is unowned" in messages[0]


def test_all_circuits_together_pass() -> None:
    """The demo shapes side by side: physical carriers, a tunnel, a DCI and a handoff raise nothing."""
    assert (
        _run(physical=[DF10, XC, INET_OFFICE, INET_GW], tunnels=[TUNNEL], interconnects=[DCI], handoffs=[HANDOFF]) == []
    )
