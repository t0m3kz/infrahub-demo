"""CheckCircuitPlacement: the vpn circuit (a tunnel over the near facility's internet access, on a connection).

Payload builders: tests/unit/circuit_placement_check_helpers.py (``_vpn``, ``VPN_CIRCUIT``).
"""

from __future__ import annotations

from typing import Any

from tests.unit.circuit_placement_check_helpers import (
    AWS_TUNNEL,
    BL10_TUNNEL,
    C001_OFFICE,
    DC10,
    EUC1,
    FR2,
    INET_DC10,
    VPN_CIRCUIT,
    _decommissioned,
    _edges,
    _run,
    _vpn,
    _vport,
)


class TestVpnCircuit:
    """A vpn needs an internet_vpn connection, two ends, the near end on an internet carrier, an ip_namespace."""

    def test_vpn_passes(self) -> None:
        """The reference vpn (border leaf tunnel over the Orange internet circuit) has no finding."""
        assert _run(vpns=[VPN_CIRCUIT]) == []

    def test_end_order_does_not_matter(self) -> None:
        """The query does not order the ends."""
        circuit = _vpn("V", [DC10, EUC1], [AWS_TUNNEL, BL10_TUNNEL])
        assert _run(vpns=[circuit]) == []

    def test_no_connection_fails(self) -> None:
        """A vpn on no connection is reported and nothing else depends on it."""
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL], connection=None)])
        assert messages == [
            "Vpn circuit 'V' needs a provider connection with path_type internet_vpn: it is on no connection."
        ]

    def test_dedicated_connection_fails(self) -> None:
        """A vpn on a dedicated connection is reported with the connection's path_type."""
        circuit = _vpn("V", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL], connection=("PC-1", "dedicated", DC10))
        messages = _run(vpns=[circuit])
        assert messages == [
            "Vpn circuit 'V' needs a provider connection with path_type internet_vpn: its connection 'PC-1' is "
            "dedicated."
        ]

    def test_not_two_end_ports_fails(self) -> None:
        """One end port is reported."""
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [BL10_TUNNEL])])
        assert messages == ["Vpn circuit 'V' has 1 end port(s): it terminates on exactly 2 ports, one per end."]

    def test_near_end_on_no_internet_carrier_fails(self) -> None:
        """The near end whose port rides no carrier is reported."""
        bare = _vport("bl-dc101101", "Tunnel1", parent=("Ethernet1/36", ()))
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [bare, AWS_TUNNEL])])
        assert len(messages) == 1
        assert "end 'bl-dc101101:Tunnel1' rides no live internet carrier circuit" in messages[0]
        assert "it is on no live one" in messages[0]

    def test_near_end_on_a_decommissioned_internet_carrier_fails(self) -> None:
        """A decommissioned internet circuit carries nothing."""
        down = _vport("bl-dc101101", "Tunnel1", parent=("Ethernet1/36", (_decommissioned(INET_DC10),)))
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [down, AWS_TUNNEL])])
        assert len(messages) == 1 and "rides no live internet carrier circuit" in messages[0]

    def test_no_end_in_the_near_facility_fails(self) -> None:
        """Neither end sits in the connection's near facility (here the connection starts in FR2)."""
        circuit = _vpn("V", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL], connection=("PC-1", "internet_vpn", FR2))
        messages = _run(vpns=[circuit])
        assert (
            len(messages) == 1 and "no end port on a device deployed in its connection's near facility" in messages[0]
        )

    def test_no_namespace_fails(self) -> None:
        """The session runs in an environment namespace."""
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL], namespace=None)])
        assert messages == ["Vpn circuit 'V' has no ip_namespace: its session runs in an environment namespace."]

    def test_registered_prefixes_fail(self) -> None:
        """Only a tunnel registers prefixes."""
        messages = _run(vpns=[{**VPN_CIRCUIT, "registered_prefixes": _edges([{"id": "pfx-1"}])}])
        assert len(messages) == 1 and "sets registered_prefixes" in messages[0]

    def test_an_office_in_the_locations_fails(self) -> None:
        """A vpn takes its owner from the connection: no office among its locations."""
        messages = _run(vpns=[_vpn("V", [DC10, C001_OFFICE], [BL10_TUNNEL, AWS_TUNNEL])])
        assert any("names no office" in message for message in messages)

    def test_decommissioned_vpn_is_exempt(self) -> None:
        """No tunnel is built for a decommissioned vpn."""
        circuit: dict[str, Any] = {**_vpn("V", [DC10], [], connection=None), "status": {"value": "decommissioned"}}
        assert _run(vpns=[circuit]) == []

    def test_prod_vpn_meeting_non_prod_vpn_is_an_environment_conflict(self) -> None:
        """A vpn takes its environment class from its ip_namespace like a handoff."""
        prod = VPN_CIRCUIT
        near = _vport("bl-dc101101", "Tunnel2", parent=("Ethernet1/36", (INET_DC10,)))
        far = _vport("EXT-AWS-VPN-EUC1", "Tunnel2")
        non_prod = _vpn("V2", [DC10, EUC1], [near, far], namespace=("NON-PROD", "non_prod"))
        messages = _run(vpns=[prod, non_prod])
        assert len(messages) == 2 and all("different environments" in message for message in messages)

    def test_two_vpns_on_one_port_fail(self) -> None:
        """A tunnel port is an end of at most one session circuit."""
        messages = _run(vpns=[VPN_CIRCUIT, _vpn("V2", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL])])
        assert len(messages) == 1 and "all terminate on" in messages[0] and "Vpn circuit" in messages[0]
