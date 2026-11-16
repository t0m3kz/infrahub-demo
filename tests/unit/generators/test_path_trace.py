"""utils/path_trace.py: a cloud connection traced from its network or peering down to L1 and our session end, and the
LAG case of utils/circuit_underlay.py that reuses it.
"""

from __future__ import annotations

from typing import Any

from tests.unit.cloud_path_helpers import (
    carrier,
    end,
    handoff_path,
    link_attachment,
    network_attachment,
    peering,
    port,
    session,
)
from utils.circuit_underlay import cabled_port, port_cabled, port_underlay
from utils.data_cleaning import clean_data
from utils.path_trace import trace_gateway, trace_network, trace_peering


def _clean(node: dict[str, Any]) -> dict[str, Any]:
    return clean_data({"root": {"edges": [{"node": node}]}})["root"][0]


def _kinds(trace: Any) -> list[str]:
    return [hop.kind for hop in trace.hops]


_SESSION = (session("HANDOFF-1", ("eg-01", "Eth1/1.100"), ("dx-1", "vif")),)


class TestTracePeering:
    """Peering -> parent -> (LAG members) -> cable or carrier -> our port -> our interface -> session circuit."""

    def test_a_cabled_port_reaches_our_sub_interface(self) -> None:
        trace = trace_peering(_clean(handoff_path("1")))

        assert trace.ok
        assert _kinds(trace) == ["peering", "port", "cable", "our port", "interface", "session circuit"]
        assert str(trace.hops[3]) == "our port 'eg-01:Eth1/1'"

    def test_a_lag_is_followed_to_its_member_ports(self) -> None:
        member = port("EXT-DX", "et-1", cable_to=end("eg-01", "Eth1/1", subs=("Eth1/1.100",)))
        lag = port("EXT-DX", "ae0", members=(member,))

        trace = trace_peering(_clean(peering("vif", "dx-1", parent=lag, sessions=_SESSION)))

        assert trace.ok
        assert _kinds(trace)[:4] == ["peering", "LAG", "port", "cable"]
        assert str(trace.hops[2]) == "port 'EXT-DX:et-1'"

    def test_a_lag_without_members_stops_at_the_lag(self) -> None:
        trace = trace_peering(
            _clean(peering("vif", "dx-1", parent=port("EXT-DX", "ae0", members=()), sessions=_SESSION))
        )

        assert not trace.ok and trace.missing == "the LAG has no member port"
        assert trace.stop == "LAG 'EXT-DX:ae0'"

    def test_our_sub_interface_on_our_lag_is_found_from_its_member(self) -> None:
        ours = end("eg-01", "Eth1/1")
        ours["lag"] = {"node": {"id": "eg-01:Po1", "sub_interfaces": {"edges": [{"node": {"id": "eg-01:Po1.100"}}]}}}
        circuit = session("HANDOFF-1", ("eg-01", "Po1.100"), ("dx-1", "vif"))

        trace = trace_peering(
            _clean(peering("vif", "dx-1", parent=port("EXT-DX", "p1", cable_to=ours), sessions=(circuit,)))
        )

        assert trace.ok and str(trace.hops[-2]) == "interface 'eg-01:Po1.100'"

    def test_a_carrier_circuit_reaches_our_port(self) -> None:
        provider = port("EXT-DX", "p1")
        provider["capabilities"] = {
            "edges": [{"node": carrier("XC-1", end("EXT-DX", "p1"), end("eg-01", "Eth1/1", subs=("Eth1/1.100",)))}]
        }

        trace = trace_peering(_clean(peering("vif", "dx-1", parent=provider, sessions=_SESSION)))

        assert trace.ok and _kinds(trace)[2] == "carrier circuit"

    def test_a_decommissioned_carrier_is_no_l1(self) -> None:
        provider = port("EXT-DX", "p1")
        provider["capabilities"] = {
            "edges": [{"node": carrier("XC-1", end("EXT-DX", "p1"), end("eg-01", "Eth1/1"), status="decommissioned")}]
        }

        trace = trace_peering(_clean(peering("vif", "dx-1", parent=provider, sessions=_SESSION)))

        assert trace.missing == "no cable or live carrier circuit on EXT-DX:p1"

    def test_no_parent_stops_at_the_peering(self) -> None:
        trace = trace_peering(_clean(peering("vif", "dx-1", sessions=_SESSION)))

        assert trace.stop == "peering 'dx-1:vif'" and "parent" in str(trace.missing)

    def test_no_session_circuit_stops_at_the_peering(self) -> None:
        trace = trace_peering(_clean(peering("vif", "dx-1", parent=port("EXT-DX", "p1"))))

        assert trace.missing == "no session circuit (handoff, vpn or tunnel) ends on it"

    def test_l1_to_a_port_that_carries_no_session_end_names_it(self) -> None:
        provider = port("EXT-DX", "p1", cable_to=end("eg-02", "Eth1/9"))

        trace = trace_peering(_clean(peering("vif", "dx-1", parent=provider, sessions=_SESSION)))

        assert not trace.ok and "reaches eg-02:Eth1/9, which carries no end of its session circuit" in str(
            trace.missing
        )

    def test_a_patch_panel_end_is_not_followed(self) -> None:
        provider = port("EXT-DX", "p1", cable_to={})

        trace = trace_peering(_clean(peering("vif", "dx-1", parent=provider, sessions=_SESSION)))

        assert "patch panel" in str(trace.missing)


class TestTraceNetwork:
    """Network -> attachment -> hub -> the hub's link -> edge gateway <- peering -> L1."""

    def _roots(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        attachments = [
            network_attachment("tgw-vpc", "tgw", "vpc-a", "C003", table="rt-a", propagates=("rt-onprem",)),
            link_attachment("tgw-hybrid", "tgw", "dxgw"),
        ]
        return [_clean(a) for a in attachments], [_clean(handoff_path("1", gateway="dxgw"))]

    def test_the_network_reaches_l1_through_the_hub(self) -> None:
        attachments, peerings = self._roots()

        trace = trace_network({"id": "vpc-a", "name": "vpc-a"}, attachments, peerings)

        assert trace.ok
        assert _kinds(trace)[:4] == ["virtual network", "attachment", "gateway", "peering"]

    def test_a_network_without_attachment_stops_there(self) -> None:
        attachments, peerings = self._roots()

        trace = trace_network({"id": "vpc-z", "name": "vpc-z"}, attachments, peerings)

        assert trace.missing == "no gateway attachment targets it"

    def test_a_hub_whose_link_lands_no_peering_stops_at_the_hub(self) -> None:
        attachments, _ = self._roots()

        trace = trace_gateway({"id": "tgw", "name": "tgw"}, attachments, [])

        assert trace.stop == "gateway 'tgw'" and "no peering lands" in str(trace.missing)


class TestCircuitUnderlayLag:
    """A sub-interface on a LAG is carried by the cables and carriers of the LAG's members (was one level only)."""

    def _sub_on_lag(self, member: dict[str, Any]) -> dict[str, Any]:
        return _clean(
            {"id": "sub", "name": {"value": "ae0.100"}, "parent": {"node": port("X", "ae0", members=(member,))}}
        )

    def test_a_cabled_member_carries_the_sub_interface(self) -> None:
        sub = self._sub_on_lag(port("X", "et-1", cable_to=end("eg-01", "Eth1/1")))

        assert port_cabled(sub)
        assert (cabled_port(sub) or {}).get("id") == "X:et-1"

    def test_a_carrier_on_a_member_is_the_sub_interfaces_underlay(self) -> None:
        sub = self._sub_on_lag(port("X", "et-1", carriers=(carrier("XC-9", end("X", "et-1")),)))

        assert [c["id"] for c in port_underlay(sub)] == ["XC-9"]

    def test_a_lag_without_cabled_members_is_not_carried(self) -> None:
        assert not port_cabled(self._sub_on_lag(port("X", "et-1")))
