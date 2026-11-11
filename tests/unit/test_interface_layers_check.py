"""Unit tests for CheckInterfaceLayers (checks/interface_layers.py): raw shapes of queries/validation/interface_layers.gql."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.interface_layers import CheckInterfaceLayers
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "interface_layers.gql"


def _caps(*kinds: str, rows: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {"edges": [{"node": {"__typename": kind}} for kind in kinds] + [{"node": row} for row in rows]}


def _row(segment: str, vlan: int = 100) -> dict[str, Any]:
    """A raw VLAN row among a port's capabilities: the switch's row of ``segment`` with ``vlan``."""
    return {
        "__typename": "ManagedSegmentVlan",
        "id": f"sv-{segment}-{vlan}",
        "vlan_id": {"value": vlan},
        "segment_deployment": {"node": {"segment": {"node": {"id": f"seg-{segment}", "name": {"value": segment}}}}},
    }


def _sub(role: str, device_kind: str) -> dict[str, Any]:
    """A raw sub-interface of a port: a routed one (a service sub-interface of a physical device) or a VM NIC."""
    return {
        "__typename": "DcimVirtualInterface",
        "role": {"value": role},
        "device": {"node": {"__typename": device_kind}},
    }


def _port(
    name: str = "Ethernet1",
    *,
    segments: int = 0,
    ip: bool = False,
    subs: int = 0,
    nics: int = 0,
    lag: bool = False,
    caps: tuple[str, ...] = (),
    rows: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """A raw DcimPhysicalInterface / DcimLAGInterface node on a leaf; ``segments`` rows of distinct segments, plus
    ``rows`` as given."""
    members = tuple(_row(f"s{index}", 100 + index) for index in range(segments)) + rows
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "device": {"node": {"name": {"value": "leaf-1"}}},
        "lag": {"node": {"id": "lag-1"} if lag else None},
        "ip_address": {"node": {"id": "ip-1"} if ip else None},
        # the sub-interfaces: the interfaces of the encapsulations that ride on this port (one encapsulation each)
        "encapsulations": {
            "edges": [
                {"node": {"capabilities": {"edges": [{"node": sub}]}}}
                for sub in (
                    [_sub("service", "DcimPhysicalDevice") for _ in range(subs)]
                    + [_sub("uplink", "DcimVirtualDevice") for _ in range(nics)]
                )
            ]
        },
        "capabilities": _caps(*caps, rows=members),
    }


def _run(
    physical: list[dict[str, Any]] | None = None,
    lags: list[dict[str, Any]] | None = None,
    rows: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
    deployments: list[dict[str, Any]] | None = None,
    ospf: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[str]]:
    """(errors, warnings) of the check on raw data."""
    check = cast(Any, CheckInterfaceLayers.__new__(CheckInterfaceLayers))
    errors: list[str] = []
    warnings: list[str] = []
    check.log_error = lambda message, **_: errors.append(message)
    check.log_warning = lambda message, **_: warnings.append(message)
    check.validate(
        {
            "DcimPhysicalInterface": {"edges": [{"node": node} for node in physical or []]},
            "DcimLAGInterface": {"edges": [{"node": node} for node in lags or []]},
            "ManagedSegmentVlan": {"edges": [{"node": node} for node in rows or []]},
            "AppComponent": {"edges": [{"node": node} for node in components or []]},
            "ManagedSegmentDeployment": {"edges": [{"node": node} for node in deployments or []]},
            "RoutingOSPFInterface": {"edges": [{"node": node} for node in ospf or []]},
        }
    )
    return errors, warnings


def _errors(*args: Any, **kwargs: Any) -> list[str]:
    return _run(*args, **kwargs)[0]


def test_pure_switchports_and_routed_ports_pass() -> None:
    """A segment member, an addressed port, a port with circuits only: one layer each."""
    ports = [
        _port("E1", segments=2),
        _port("E4", ip=True, caps=("ManagedBGPPeering",)),
        _port("E5", subs=2),
        _port("E6", ip=True, caps=("TopologyCarrierCircuit",)),
        _port("E7"),
    ]
    assert _errors(ports) == []


def test_a_vm_nic_pinned_to_a_lag_member_is_no_sub_interface_signal() -> None:
    """00_full: ktw1-vm-01 eth0 rides on eno1 of ktw1-srv-01, a member of bond0: the NIC is no routed sub-interface."""
    assert _errors([_port("eno1", lag=True, nics=2)]) == []
    assert _errors([_port("eno1", nics=1)]) == []


def test_a_routed_sub_interface_on_a_lag_member_still_fails() -> None:
    """R2 stays for a real sub-interface: with a NIC beside it only the routed one is reported."""
    errors = _errors([_port("Ethernet1/3", lag=True, subs=1, nics=1)])
    assert len(errors) == 1 and "is a LAG member but carries sub_interfaces" in errors[0]


def test_r1_segment_and_ip_address_fail() -> None:
    """R1: a segment member with an address."""
    errors = _errors([_port(segments=1, ip=True)])
    assert len(errors) == 1 and "both a switchport (segment VLAN rows)" in errors[0] and "ip_address" in errors[0]


def test_r1_segment_and_sub_interface_fail() -> None:
    """R1: a segment member with sub-interfaces."""
    errors = _errors([_port(segments=1, subs=1)])
    assert len(errors) == 1 and "segment VLAN rows" in errors[0] and "sub_interfaces" in errors[0]


def test_a_context_is_no_interface_capability_and_no_l2_tag() -> None:
    """A context has no interface edge: a kind of it among a port's capabilities is no tag (the port is judged by the
    rest)."""
    assert _errors([_port(ip=True, caps=("ManagedFirewallContext",))]) == []


def test_r1_segment_and_bgp_peering_fail() -> None:
    """R1: a routed capability is a signal."""
    assert len(_errors([_port(segments=1, caps=("ManagedBGPPeering",))])) == 1


def test_r1_every_l3_capability_is_a_signal() -> None:
    """R1: the OSPF interface settings and the session circuit count; a carrier circuit never does (nor an OSPF peering
    or a routed exchange: neither is on the interface edge)."""
    for kind in ("RoutingOSPFInterface", "TopologySessionCircuit"):
        assert len(_errors([_port(segments=1, caps=(kind,))])) == 1, kind
    assert _errors([_port(segments=1, caps=("TopologyCarrierCircuit",))]) == []


def test_r1_applies_to_a_lag() -> None:
    """R1: a LAG with a segment and an address."""
    assert len(_errors(lags=[_port("Port-Channel1", segments=1, ip=True)])) == 1


def test_r2_lag_member_with_a_segment_or_a_signal_fails() -> None:
    """R2: the LAG carries them, not the member; one error per member, not an R1 on top."""
    assert len(_errors([_port(lag=True, segments=1)])) == 1
    assert len(_errors([_port(lag=True, ip=True)])) == 1
    assert len(_errors([_port(lag=True, segments=1, ip=True)])) == 1
    assert _errors([_port(lag=True, caps=("TopologyCarrierCircuit",))]) == []
    assert _errors([_port(lag=True)]) == []


def test_r2_lag_member_with_a_segment_and_an_address_is_one_error() -> None:
    """R2: the tag belongs to the LAG, not to its member; R1 does not repeat it."""
    errors = _errors([_port(lag=True, segments=1, ip=True)])
    assert len(errors) == 1 and "LAG member" in errors[0]
    assert errors[0].count("ip_address") == 1


def test_the_fixtures_emit_only_fields_the_query_selects_and_the_layer_fields_are_selected() -> None:
    """The builders match interface_layers.gql, which must select lag, encapsulations and the capability kinds with
    the rows, their VLAN and their deployment's segment, and per row its requests, ports and their cable ends."""
    tree = query_tree(QUERY)
    assert "DcimVirtualInterface" not in tree
    for root, node in (("DcimPhysicalInterface", _port()), ("DcimLAGInterface", _port("Port-Channel1"))):
        selected = tree[root]["edges"]["node"]
        assert {"ip_address", "encapsulations", "capabilities"} <= set(selected)
        assert "segments" not in selected
        assert {"vlan_id", "segment_deployment"} <= set(selected["capabilities"]["edges"]["node"])
        node = _port(node["name"]["value"], segments=1)
        emitted = {key: value for key, value in node.items() if root == "DcimPhysicalInterface" or key != "lag"}
        assert unselected(emitted, selected) == []
    assert "lag" in tree["DcimPhysicalInterface"]["edges"]["node"]
    row = tree["ManagedSegmentVlan"]["edges"]["node"]
    for member in (_member("E1", host="srv-1"), _lag_member("Po1", host="srv-1")):
        assert unselected(_row_with(member), row) == []
    assert unselected(_component("web", "srv-1"), tree["AppComponent"]["edges"]["node"]) == []
    assert unselected(_assigned("DcimVirtualInterface"), tree["ManagedSegmentDeployment"]["edges"]["node"]) == []
    setting = _ospf("ospf-a", ("leaf-1", "E1"), peer_processes=(("p1", "leaf-1"),))
    assert unselected(setting, tree["RoutingOSPFInterface"]["edges"]["node"]) == []


def _assigned(*kinds: str) -> dict[str, Any]:
    """A raw ManagedSegmentDeployment with one assigned interface of each kind."""
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


def test_r3_a_deployment_or_a_row_lists_only_physical_ports_and_lags() -> None:
    """R3: a virtual interface (an SVI, a NIC) never carries a segment, asked or realized."""
    assert _errors(deployments=[_assigned("DcimPhysicalInterface", "DcimLAGInterface")]) == []
    errors = _errors(deployments=[_assigned("DcimPhysicalInterface", "DcimVirtualInterface")])
    assert errors == [
        "segment 'web' in 'DC-1' lists DcimVirtualInterface 'leaf-1' if1 "
        "(rule R3: only a physical port or a LAG carries a segment)."
    ]
    nic = {**_member("eth0"), "__typename": "DcimVirtualInterface"}
    errors, _ = _run(rows=[_row_with(nic, requested=("if-eth0",))])
    assert len(errors) == 1 and "rule R3" in errors[0] and "VLAN row 100" in errors[0]


def test_a_port_assigned_to_a_deployment_is_a_switchport() -> None:
    """R1: the deployment a port is assigned to is an L2 tag like its row (one error, not two)."""
    errors = _errors([_port(ip=True, caps=("ManagedSegmentDeployment",))])
    assert len(errors) == 1 and "segment deployments" in errors[0]


def _cable_to(host: str | None, role: str = "uplink") -> dict[str, Any]:
    """The cable of a switch port whose far end is a NIC of ``host`` with ``role`` (no cable for None)."""
    if host is None:
        return {"node": None}
    far = {"id": f"{host}-nic", "role": {"value": role}, "device": {"node": {"id": host}}}
    return {"node": {"endpoints": {"edges": [{"node": far}]}}}


def _member(name: str, *, host: str | None = None, role: str = "customer") -> dict[str, Any]:
    """A raw physical port listed by a row, cabled to ``host``."""
    return {
        "__typename": "DcimPhysicalInterface",
        "id": f"if-{name}",
        "name": {"value": name},
        "role": {"value": role},
        "device": {"node": {"name": {"value": "leaf-1"}}},
        "cable": _cable_to(host),
    }


def _lag_member(name: str, *, host: str | None = None) -> dict[str, Any]:
    """A raw LAG listed by a row whose one member (role lag) is cabled to a LAG NIC of ``host``."""
    return {
        "__typename": "DcimLAGInterface",
        "id": f"if-{name}",
        "name": {"value": name},
        "role": {"value": "customer"},
        "device": {"node": {"name": {"value": "leaf-1"}}},
        "member_interfaces": {
            "edges": [{"node": {"id": f"if-{name}-m1", "role": {"value": "lag"}, "cable": _cable_to(host, "lag")}}]
        },
    }


def _row_with(*members: dict[str, Any], requested: tuple[str, ...] = (), terminated: bool = False) -> dict[str, Any]:
    """A raw ManagedSegmentVlan of segment web listing ``members``; ``requested``: the ids of the ports assigned to its
    deployment."""
    segment: dict[str, Any] = {"id": "seg-web", "name": {"value": "web"}}
    if terminated:
        segment["terminated_on"] = {"node": {"id": "ctx-1"}}
    return {
        "id": "sv-web-100",
        "vlan_id": {"value": 100},
        "device": {"node": {"name": {"value": "leaf-1"}}},
        "segment_deployment": {
            "node": {
                "capabilities": {"edges": [{"node": {"id": port_id}} for port_id in requested]},
                "segment": {"node": segment},
            }
        },
        "capabilities": {"edges": [{"node": member} for member in members]},
    }


def _component(segment: str, *hosts: str) -> dict[str, Any]:
    """A raw AppComponent on ``segment`` with one physical instance per host."""
    return {
        "id": f"comp-{segment}",
        "network_segment": {"node": {"id": f"seg-{segment}"}},
        "instances": {"edges": [{"node": {"id": host, "__typename": "DcimPhysicalDevice"}} for host in hosts]},
    }


def test_r4_two_rows_of_one_segment_on_a_port_fail() -> None:
    """R4: one row of a segment per port; two segments are fine."""
    assert _errors([_port(rows=(_row("web"), _row("app", 200)))]) == []
    errors = _errors([_port(rows=(_row("web"), _row("web", 300)))])
    assert errors == [
        "port 'leaf-1' Ethernet1 is a member of 2 VLAN rows of segment 'web' (VLANs 100, 300) "
        "(rule R4: one row of a segment per port)."
    ]


def test_r5_an_assigned_or_derived_member_is_no_warning() -> None:
    """R5: a port assigned to the deployment, a port facing a host of a component, a LAG whose member faces one, and a firewall
    service port of a terminated segment were all asked for."""
    assert _run(rows=[_row_with(_member("E1"), requested=("if-E1",))]) == ([], [])
    components = [_component("web", "srv-1")]
    assert _run(rows=[_row_with(_member("E2", host="srv-1"))], components=components) == ([], [])
    assert _run(rows=[_row_with(_lag_member("Po1", host="srv-1"))], components=components) == ([], [])
    assert _run(rows=[_row_with(_member("E3", role="firewall"), terminated=True)]) == ([], [])


def test_r5_a_member_nobody_asked_for_is_a_warning() -> None:
    """R5: not assigned, facing no host of the segment's components (or one of another segment), not a service port
    of a terminated segment: the next reconcile removes it, the check shows it meanwhile."""
    components = [_component("other", "srv-1")]
    errors, warnings = _run(
        rows=[_row_with(_member("E1"), _member("E2", host="srv-1"), _member("E3", role="firewall"))],
        components=components,
    )
    assert errors == [] and len(warnings) == 3
    assert all("rule R5" in warning and "VLAN row 100 of segment 'web' on 'leaf-1'" in warning for warning in warnings)


def _other_device(member: dict[str, Any], device: str) -> dict[str, Any]:
    return {**member, "device": {"node": {"name": {"value": device}}}}


def test_r6_a_row_lists_only_ports_of_its_own_switch() -> None:
    """R6: a port of another device in a row is an ERROR naming the row and the port; the row's own ports pass."""
    assert (
        _errors(
            rows=[_row_with(_member("E1", host="srv-1"), _member("E2", host="srv-1"))],
            components=[_component("web", "srv-1")],
        )
        == []
    )
    stray = _other_device(_member("E9", host="srv-1"), "leaf-2")
    errors = _errors(rows=[_row_with(_member("E1", host="srv-1"), stray)], components=[_component("web", "srv-1")])
    assert len(errors) == 1
    assert (
        "rule R6" in errors[0]
        and "VLAN row 100 of segment 'web' on 'leaf-1'" in errors[0]
        and "'leaf-2' E9" in errors[0]
    )


def _ospf(
    name: str,
    *ports: tuple[str, str],
    peer_processes: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    """A raw RoutingOSPFInterface listing ``ports`` (device, interface); ``peer_processes``: (process, device) of its
    peering (no peering for None)."""
    peering = None
    if peer_processes is not None:
        peering = {
            "node": {
                "ospf_process": {
                    "edges": [
                        {
                            "node": {
                                "name": {"value": proc},
                                "capabilities": {"edges": [{"node": {"name": {"value": dev}}}]},
                            }
                        }
                        for proc, dev in peer_processes
                    ]
                }
            }
        }
    return {
        "id": f"ospf-{name}",
        "name": {"value": name},
        "capabilities": {
            "edges": [
                {"node": {"id": f"{dev}-{ifc}", "name": {"value": ifc}, "device": {"node": {"name": {"value": dev}}}}}
                for dev, ifc in ports
            ]
        },
        "peering": {"node": peering["node"]} if peering else {"node": None},
    }


def test_r7_the_interfaces_of_an_ospf_setting_are_on_one_device() -> None:
    """R7: interfaces of two devices in one setting are an ERROR naming both devices."""
    assert _run(ospf=[_ospf("ospf-a", ("leaf-1", "E1"), ("leaf-1", "E2"))]) == ([], [])
    errors = _errors(ospf=[_ospf("ospf-a", ("leaf-1", "E1"), ("leaf-2", "E1"))])
    assert len(errors) == 1 and "rule R7" in errors[0] and "leaf-1, leaf-2" in errors[0] and "'ospf-a'" in errors[0]


def test_r8_an_ospf_setting_with_no_interface_is_a_warning() -> None:
    """R8: a setting nothing applies (its device or interface went) is a WARNING, not an ERROR."""
    errors, warnings = _run(ospf=[_ospf("ospf-orphan")])
    assert errors == [] and len(warnings) == 1 and "rule R8" in warnings[0] and "'ospf-orphan'" in warnings[0]


def test_r9_an_ospf_setting_is_on_a_device_of_its_peering() -> None:
    """R9: with a peering whose processes run on other devices, the setting is an ERROR; no peering, no judgment."""
    ok = _ospf("ospf-a", ("leaf-1", "E1"), peer_processes=(("p1", "leaf-1"), ("p2", "spine-1")))
    assert _run(ospf=[ok]) == ([], [])
    assert _run(ospf=[_ospf("ospf-a", ("leaf-1", "E1"))]) == ([], [])
    errors = _errors(ospf=[_ospf("ospf-a", ("leaf-3", "E1"), peer_processes=(("p1", "leaf-1"), ("p2", "spine-1")))])
    assert len(errors) == 1 and "rule R9" in errors[0] and "leaf-1, spine-1" in errors[0]


def _svi_member(name: str = "Vlan100", *, device: str = "leaf-1", role: str = "svi") -> dict[str, Any]:
    """A raw segment SVI listed by a row: a virtual interface of ``role`` on ``device``."""
    return {
        "__typename": "DcimVirtualInterface",
        "id": f"if-{device}-{name}",
        "name": {"value": name},
        "role": {"value": role},
        "device": {"node": {"name": {"value": device}}},
    }


def test_r3_r5_a_row_s_svi_is_a_member_that_needs_no_request_and_no_host() -> None:
    """The SVI of a routed row is a member of it (not a port): R3 allows it, R5 does not ask who requested it."""
    row = _row_with(_member("E1", host="srv-1"), _svi_member())
    assert _run(rows=[row], components=[_component("web", "srv-1")]) == ([], [])
    assert _run(rows=[_row_with(_svi_member())]) == ([], [])


def test_r3_only_a_role_svi_virtual_interface_is_allowed_in_a_row() -> None:
    """R3: a virtual interface of another role (a NIC, a service sub-interface) is still no member of a row."""
    errors, _ = _run(rows=[_row_with(_svi_member(role="service"))])
    assert len(errors) == 1 and "rule R3" in errors[0]


def test_r6_the_svi_of_another_switch_in_a_row_fails() -> None:
    """R6: the edge has no same-device rule in the schema, so an SVI of leaf-2 on the row of leaf-1 is an ERROR."""
    errors = _errors(rows=[_row_with(_svi_member(device="leaf-2"))])
    assert len(errors) == 1 and "rule R6" in errors[0] and "'leaf-2' Vlan100" in errors[0]


def test_r10_a_row_has_at_most_one_svi() -> None:
    """R10: two SVIs in one row are an ERROR naming both; one is fine."""
    assert _errors(rows=[_row_with(_svi_member())]) == []
    errors = _errors(rows=[_row_with(_svi_member("Vlan100"), _svi_member("Vlan101"))])
    assert len(errors) == 1 and "rule R10" in errors[0] and "Vlan100, Vlan101" in errors[0]
