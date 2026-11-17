"""Unit tests for the vpn link of add_virtual_circuit (the handoff /31 and session are in test_virtual_circuit_session_handoff.py)."""

from __future__ import annotations

import pytest

from tests.unit.simulators.circuit_fakes import FakeClient, vpn_payload
from tests.unit.virtual_circuit_session_helpers import _assert_refused, _peering, _run

VPN = "VC-C001-ZS-VPN"


class TestVpn:
    @pytest.mark.asyncio
    async def test_a_vpn_needs_no_carrier_and_builds_one_regular_session(self) -> None:
        """The tunnel interfaces carry no circuit: the session is still built, eBGP, no communities, BFD on."""
        client = FakeClient()

        await _run(client, vpn_payload())

        peering = _peering(client, "bgp")
        assert peering["name"] == f"{VPN}-bgp"
        assert peering["peering_role"] == "regular" and peering["session_type"] == "EBGP"
        assert peering["ttl"] == 1 and peering["bfd_enabled"] is True
        assert peering["send_community"] is False
        assert "VPN eBGP" in peering["description"]
        assert client.allocations[0]["data"]["description"] == f"VPN {VPN}"
        assert client.allocations[0]["prefix_length"] == 31

    @pytest.mark.asyncio
    async def test_a_physical_end_is_refused(self) -> None:
        """A VPN ends on tunnel interfaces, a physical port is no tunnel."""
        from tests.unit.simulators.circuit_fakes import session_port, tunnel_edge_port

        physical = session_port("if-p", "Ethernet1", "EXT-ZS-FR2", kind="DcimPhysicalInterface")
        await _assert_refused(vpn_payload(ends=[tunnel_edge_port(), physical]), "tunnel interfaces")

    @pytest.mark.asyncio
    async def test_a_vpn_needs_its_namespace(self) -> None:
        """No ip_namespace, no pool: refused before any write."""
        await _assert_refused(vpn_payload(namespace=None), "needs its routing VRF")
