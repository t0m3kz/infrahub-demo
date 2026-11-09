"""Unit tests for CheckInterfaceLayers (checks/interface_layers.py): raw shapes of queries/validation/interface_layers.gql."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.interface_layers import CheckInterfaceLayers
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "interface_layers.gql"


def _caps(*kinds: str, deployments: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {"edges": [{"node": {"__typename": kind}} for kind in kinds] + [{"node": dep} for dep in deployments]}


def _deployment(segment: str, hosting: str = "dc-1") -> dict[str, Any]:
    """A raw segment deployment among a port's capabilities: the segment realized in ``hosting``."""
    return {
        "__typename": "ManagedSegmentDeployment",
        "id": f"sd-{segment}-{hosting}",
        "deployment": {"node": {"id": hosting, "name": {"value": hosting.upper()}}},
        "segment": {"node": {"id": f"seg-{segment}", "name": {"value": segment}}},
    }


def _device_deployment(hosting: str = "dc-1") -> dict[str, Any]:
    """A pod of the DC ``hosting``: the device's hosting deployment is its parent."""
    return {
        "node": {
            "__typename": "TopologyPod",
            "id": f"pod-{hosting}",
            "name": {"value": f"POD-{hosting.upper()}"},
            "parent": {"node": {"__typename": "TopologyDataCenter", "id": hosting, "name": {"value": hosting.upper()}}},
        }
    }


def _port(
    name: str = "Ethernet1",
    *,
    segments: int = 0,
    ip: bool = False,
    subs: int = 0,
    lag: bool = False,
    caps: tuple[str, ...] = (),
    deployments: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """A raw DcimPhysicalInterface / DcimLAGInterface node on a leaf in a pod of dc-1; ``segments`` deployments of
    distinct segments in dc-1, plus ``deployments`` as given."""
    members = tuple(_deployment(f"s{index}") for index in range(segments)) + deployments
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "device": {"node": {"name": {"value": "leaf-1"}, "deployment": _device_deployment()}},
        "lag": {"node": {"id": "lag-1"} if lag else None},
        "ip_address": {"node": {"id": "ip-1"} if ip else None},
        "sub_interfaces": {"count": subs},
        "capabilities": _caps(*caps, deployments=members),
    }


def _run(
    physical: list[dict[str, Any]] | None = None,
    lags: list[dict[str, Any]] | None = None,
    deployments: list[dict[str, Any]] | None = None,
) -> list[str]:
    check = cast(Any, CheckInterfaceLayers.__new__(CheckInterfaceLayers))
    errors: list[str] = []
    check.log_error = lambda message, **_: errors.append(message)
    check.validate(
        {
            "DcimPhysicalInterface": {"edges": [{"node": node} for node in physical or []]},
            "DcimLAGInterface": {"edges": [{"node": node} for node in lags or []]},
            "ManagedSegmentDeployment": {"edges": [{"node": node} for node in deployments or []]},
        }
    )
    return errors


def test_pure_switchports_and_routed_ports_pass() -> None:
    """A segment member, an addressed port, a port with circuits only: one layer each."""
    ports = [
        _port("E1", segments=2),
        _port("E4", ip=True, caps=("ManagedBGPPeering",)),
        _port("E5", subs=2),
        _port("E6", ip=True, caps=("TopologyCarrierCircuit",)),
        _port("E7"),
    ]
    assert _run(ports) == []


def test_r1_segment_and_ip_address_fail() -> None:
    """R1: a segment member with an address."""
    errors = _run([_port(segments=1, ip=True)])
    assert len(errors) == 1 and "both a switchport (segment deployments)" in errors[0] and "ip_address" in errors[0]


def test_r1_segment_and_sub_interface_fail() -> None:
    """R1: a segment member with sub-interfaces."""
    errors = _run([_port(segments=1, subs=1)])
    assert len(errors) == 1 and "segment deployments" in errors[0] and "sub_interfaces" in errors[0]


def test_a_context_is_no_interface_capability_and_no_l2_tag() -> None:
    """A context has no interface edge: a kind of it among a port's capabilities is no tag (the port is judged by the
    rest)."""
    assert _run([_port(ip=True, caps=("ManagedFirewallContext",))]) == []


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


def test_r2_lag_member_with_a_segment_and_an_address_is_one_error() -> None:
    """R2: the tag belongs to the LAG, not to its member; R1 does not repeat it."""
    errors = _run([_port(lag=True, segments=1, ip=True)])
    assert len(errors) == 1 and "LAG member" in errors[0]
    assert errors[0].count("ip_address") == 1


def test_the_fixtures_emit_only_fields_the_query_selects_and_the_layer_fields_are_selected() -> None:
    """The builders match interface_layers.gql, which must select lag, sub_interfaces, the capability kinds with the
    segment deployments and the device's deployment."""
    tree = query_tree(QUERY)
    assert "DcimVirtualInterface" not in tree
    for root, node in (("DcimPhysicalInterface", _port()), ("DcimLAGInterface", _port("Port-Channel1"))):
        selected = tree[root]["edges"]["node"]
        assert {"ip_address", "sub_interfaces", "capabilities"} <= set(selected)
        assert "segments" not in selected
        assert {"deployment", "segment"} <= set(selected["capabilities"]["edges"]["node"])
        assert "deployment" in selected["device"]["node"]
        node = _port(node["name"]["value"], segments=1)
        emitted = {key: value for key, value in node.items() if root == "DcimPhysicalInterface" or key != "lag"}
        assert unselected(emitted, selected) == []
    assert "lag" in tree["DcimPhysicalInterface"]["edges"]["node"]
    assert unselected(_kind_deployment("DcimVirtualInterface"), tree["ManagedSegmentDeployment"]["edges"]["node"]) == []


def _kind_deployment(*kinds: str) -> dict[str, Any]:
    """A raw ManagedSegmentDeployment listing one interface of each kind."""
    return {
        "id": "sd-web-dc-1",
        "segment": {"node": {"name": {"value": "web"}}},
        "deployment": {"node": {"name": {"value": "DC-1"}}},
        "capabilities": {
            "edges": [
                {
                    "node": {
                        "__typename": kind,
                        "id": f"if-{index}",
                        "name": {"value": f"if{index}"},
                        "device": {"node": {"name": {"value": "leaf-1"}}},
                    }
                }
                for index, kind in enumerate(kinds)
            ]
        },
    }


def test_r3_a_deployment_lists_only_physical_ports_and_lags() -> None:
    """R3: a virtual interface (an SVI, a NIC) or a peering never carries a segment."""
    assert _run(deployments=[_kind_deployment("DcimPhysicalInterface", "DcimLAGInterface")]) == []
    errors = _run(deployments=[_kind_deployment("DcimPhysicalInterface", "DcimVirtualInterface")])
    assert len(errors) == 1 and "rule R3" in errors[0] and "DcimVirtualInterface 'leaf-1' if1" in errors[0]


def test_r4_a_deployment_of_another_topology_fails() -> None:
    """R4: the port's device sits in a pod of dc-1, so a deployment in dc-2 is the wrong realization."""
    assert _run([_port(deployments=(_deployment("web"),))]) == []
    errors = _run([_port(deployments=(_deployment("web", "dc-2"),))])
    assert len(errors) == 1 and "rule R4" in errors[0] and "in 'DC-2' but its device sits in 'DC-1'" in errors[0]


def test_r4_a_device_without_a_hosting_deployment_is_not_judged() -> None:
    """R4 needs the device's hosting deployment; without one nothing is compared."""
    port = _port(deployments=(_deployment("web", "dc-2"),))
    port["device"]["node"].pop("deployment")
    assert _run([port]) == []


def test_r5_two_deployments_of_one_segment_on_a_port_fail() -> None:
    """R5: one deployment of a segment per port; two segments are fine."""
    assert _run([_port(deployments=(_deployment("web"), _deployment("app")))]) == []
    errors = _run([_port(deployments=(_deployment("web"), _deployment("web", "dc-2")))])
    assert sum("rule R5" in error for error in errors) == 1
    assert any("2 deployments of segment 'web' (DC-1, DC-2)" in error for error in errors)
