"""CheckCircuitPlacement: the vpn circuit (a tunnel interface pair, model only)."""

from __future__ import annotations

from typing import Any

from tests.unit.circuit_placement_check_helpers import (
    AWS_TUNNEL,
    BL10_TUNNEL,
    C001_OFFICE,
    DC10,
    EUC1,
    INET_DC10,
    VPN_CIRCUIT,
    _edges,
    _run,
    _vpn,
    _vport,
)


class TestVpnCircuit:
    """A vpn needs two ends and an ip_namespace."""

    def test_vpn_passes(self) -> None:
        """The reference vpn has no finding."""
        assert _run(vpns=[VPN_CIRCUIT]) == []

    def test_end_order_does_not_matter(self) -> None:
        """The query does not order the ends."""
        circuit = _vpn("V", [DC10, EUC1], [AWS_TUNNEL, BL10_TUNNEL])
        assert _run(vpns=[circuit]) == []

    def test_not_two_end_ports_fails(self) -> None:
        """One end port is reported."""
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [BL10_TUNNEL])])
        assert messages == ["Vpn circuit 'V' has 1 end port(s): it terminates on exactly 2 ports, one per end."]

    def test_no_namespace_fails(self) -> None:
        """The session runs in an environment namespace."""
        messages = _run(vpns=[_vpn("V", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL], namespace=None)])
        assert messages == ["Vpn circuit 'V' has no vrf: its session runs in an environment routing domain."]

    def test_registered_prefixes_fail(self) -> None:
        """Only a tunnel registers prefixes."""
        messages = _run(vpns=[{**VPN_CIRCUIT, "registered_prefixes": _edges([{"id": "pfx-1"}])}])
        assert len(messages) == 1 and "sets registered_prefixes" in messages[0]

    def test_an_office_in_the_locations_fails(self) -> None:
        """A vpn is unowned: no office among its locations."""
        messages = _run(vpns=[_vpn("V", [DC10, C001_OFFICE], [BL10_TUNNEL, AWS_TUNNEL])])
        assert any("names no office" in message for message in messages)

    def test_decommissioned_vpn_is_exempt(self) -> None:
        """No tunnel is built for a decommissioned vpn."""
        circuit: dict[str, Any] = {**_vpn("V", [DC10], []), "status": {"value": "decommissioned"}}
        assert _run(vpns=[circuit]) == []

    def test_prod_vpn_meeting_non_prod_vpn_is_an_environment_conflict(self) -> None:
        """A vpn takes its environment class from its ip_namespace like a handoff."""
        prod = VPN_CIRCUIT
        near = _vport("bl-dc101101", "Tunnel2", parent=("Ethernet1/36", (INET_DC10,)))
        far = _vport("EXT-AWS-VPN-EUC1", "Tunnel2")
        non_prod = _vpn("V2", [DC10, EUC1], [near, far], namespace=("NONPROD", "non_prod"))
        messages = _run(vpns=[prod, non_prod])
        assert len(messages) == 2 and all("different environments" in message for message in messages)

    def test_two_vpns_on_one_port_fail(self) -> None:
        """A tunnel port is an end of at most one session circuit."""
        messages = _run(vpns=[VPN_CIRCUIT, _vpn("V2", [DC10, EUC1], [BL10_TUNNEL, AWS_TUNNEL])])
        assert len(messages) == 1 and "all terminate on" in messages[0] and "Vpn circuit" in messages[0]
