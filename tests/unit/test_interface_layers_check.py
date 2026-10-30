"""Unit tests for CheckInterfaceLayers (checks/interface_layers.py): raw shapes of queries/validation/interface_layers.gql."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.interface_layers import CheckInterfaceLayers
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "interface_layers.gql"


def _caps(*kinds: str) -> dict[str, Any]:
    return {"edges": [{"node": {"__typename": kind}} for kind in kinds]}


def _port(
    name: str = "Ethernet1",
    *,
    segments: int = 0,
    ip: bool = False,
    subs: int = 0,
    lag: bool = False,
    caps: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A raw DcimPhysicalInterface / DcimLAGInterface node."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "device": {"node": {"name": {"value": "leaf-1"}}},
        "lag": {"node": {"id": "lag-1"} if lag else None},
        "ip_address": {"node": {"id": "ip-1"} if ip else None},
        "segments": {"count": segments},
        "sub_interfaces": {"count": subs},
        "capabilities": _caps(*caps),
    }


def _run(physical: list[dict[str, Any]] | None = None, lags: list[dict[str, Any]] | None = None) -> list[str]:
    check = cast(Any, CheckInterfaceLayers.__new__(CheckInterfaceLayers))
    errors: list[str] = []
    check.log_error = lambda message, **_: errors.append(message)
    check.validate(
        {
            "DcimPhysicalInterface": {"edges": [{"node": node} for node in physical or []]},
            "DcimLAGInterface": {"edges": [{"node": node} for node in lags or []]},
        }
    )
    return errors


def test_pure_switchports_and_routed_ports_pass() -> None:
    """A segment member, a context port, an addressed port, a port with circuits only: one layer each."""
    ports = [
        _port("E1", segments=2),
        _port("E2", caps=("ManagedFirewallContext",)),
        _port("E3", caps=("ManagedLoadbalancerContext",)),
        _port("E4", ip=True, caps=("ManagedBGPPeering",)),
        _port("E5", subs=2),
        _port("E6", ip=True, caps=("TopologyCarrierCircuit",)),
        _port("E7"),
    ]
    assert _run(ports) == []


def test_r1_segment_and_ip_address_fail() -> None:
    """R1: a segment member with an address."""
    errors = _run([_port(segments=1, ip=True)])
    assert len(errors) == 1 and "both a switchport (segments)" in errors[0] and "ip_address" in errors[0]


def test_r1_context_and_sub_interface_fail() -> None:
    """R1: a firewall context tag with sub-interfaces."""
    errors = _run([_port(subs=1, caps=("ManagedFirewallContext",))])
    assert len(errors) == 1 and "ManagedFirewallContext" in errors[0] and "sub_interfaces" in errors[0]


def test_r1_segment_and_bgp_peering_fail() -> None:
    """R1: a routed capability is a signal."""
    assert len(_run([_port(segments=1, caps=("ManagedBGPPeering",))])) == 1


def test_r1_every_l3_capability_is_a_signal() -> None:
    """R1: the OSPF, exchange and circuit kinds all count; a carrier circuit never does."""
    for kind in (
        "RoutingOSPFInterface",
        "ManagedOSPFPeering",
        "TopologyRoutedExchange",
        "TopologySessionCircuit",
    ):
        assert len(_run([_port(segments=1, caps=(kind,))])) == 1, kind
    assert _run([_port(segments=1, caps=("TopologyCarrierCircuit",))]) == []


def test_r1_applies_to_a_lag() -> None:
    """R1: a LAG with a segment and an address."""
    assert len(_run(lags=[_port("Port-Channel1", segments=1, ip=True)])) == 1


def test_r2_lag_member_with_a_segment_or_a_signal_fails() -> None:
    """R2: the LAG carries them, not the member; one error per member, not an R1 on top."""
    assert len(_run([_port(lag=True, segments=1)])) == 1
    assert len(_run([_port(lag=True, ip=True)])) == 1
    assert len(_run([_port(lag=True, segments=1, ip=True)])) == 1
    assert _run([_port(lag=True, caps=("TopologyCarrierCircuit",))]) == []
    assert _run([_port(lag=True)]) == []


def test_r2_lag_member_carrying_a_firewall_context_fails() -> None:
    """R2: the context tag belongs to the LAG, not to its member; R1 does not repeat it."""
    errors = _run([_port(lag=True, caps=("ManagedFirewallContext",))])
    assert len(errors) == 1 and "LAG member" in errors[0] and "ManagedFirewallContext" in errors[0]
    assert _run([_port(lag=True, caps=("ManagedLoadbalancerContext",), ip=True)])[0].count("ip_address") == 1


def test_r1_firewall_context_with_an_address_fails_on_a_non_member() -> None:
    """R1: a context port (L2 tag) with an address is both layers."""
    errors = _run([_port(ip=True, caps=("ManagedFirewallContext",))])
    assert len(errors) == 1 and "both a switchport (ManagedFirewallContext)" in errors[0]


def test_the_fixtures_emit_only_fields_the_query_selects_and_the_layer_fields_are_selected() -> None:
    """The builders match interface_layers.gql, which must select segments, lag, sub_interfaces and the capability kinds."""
    tree = query_tree(QUERY)
    assert "DcimVirtualInterface" not in tree
    for root, node in (("DcimPhysicalInterface", _port()), ("DcimLAGInterface", _port("Port-Channel1"))):
        selected = tree[root]["edges"]["node"]
        assert {"segments", "ip_address", "sub_interfaces", "capabilities"} <= set(selected)
        emitted = {key: value for key, value in node.items() if root == "DcimPhysicalInterface" or key != "lag"}
        assert unselected(emitted, selected) == []
    assert "lag" in tree["DcimPhysicalInterface"]["edges"]["node"]
