"""Unit tests for the link of add_virtual_circuit: a /31 from the namespace pool with our end the lower address, the vpn."""

from __future__ import annotations

import pytest

from tests.unit.simulators.circuit_fakes import FakeClient, handoff_payload, vpn_payload
from tests.unit.virtual_circuit_session_helpers import _assert_refused, _peering, _run

VPN = "VC-C001-ZS-VPN"
BASE = "10.255.128.0"


def _addresses(client: FakeClient) -> dict[str, str]:
    """Interface id -> the address id it was given."""
    return {iface_id: iface.ip_address.id for iface_id, iface in client.interfaces.items()}


class TestHandoffLink:
    @pytest.mark.asyncio
    async def test_a_slash_31_uses_offsets_0_and_1(self) -> None:
        """Our end is the lower address of a /31 allocated from the namespace pool."""
        client = FakeClient()

        await _run(client, handoff_payload())

        assert client.allocations[0]["prefix_length"] == 31
        assert _addresses(client) == {"if-hs": f"IpamIPAddress:{BASE}/31", "if-aws": "IpamIPAddress:10.255.128.1/31"}

    @pytest.mark.asyncio
    async def test_the_session_is_directly_connected_with_bfd(self) -> None:
        """One eBGP session, TTL 1, BFD on."""
        client = FakeClient()

        await _run(client, handoff_payload())

        peering = _peering(client, "bgp")
        assert (peering["session_type"], peering["ttl"], peering["bfd_enabled"]) == ("EBGP", 1, True)


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
        await _assert_refused(vpn_payload(namespace=None), "needs its ip_namespace")
