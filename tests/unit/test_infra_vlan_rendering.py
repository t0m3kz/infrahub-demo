"""An infrastructure VLAN (ManagedVlan capability of a port) is declared on the device and tagged on the port."""

from __future__ import annotations

from typing import Any

from transforms.helpers.segments import get_vlans
from transforms.helpers.vxlan import get_interfaces


def _capability(vlan_id: int, name: str = "transfer-fw") -> dict[str, Any]:
    return {"typename": "ManagedVlan", "id": f"vlan-{vlan_id}", "name": name, "vlan_id": vlan_id}


def _port(name: str, *capabilities: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "typename": "DcimPhysicalInterface", "role": "server", "capabilities": list(capabilities)}


def _segment_activation(vlan_id: int) -> dict[str, Any]:
    return {
        "vlan_id": vlan_id,
        "vni": 10000 + vlan_id,
        "segment": {"name": f"seg-{vlan_id}", "customer_name": f"seg-{vlan_id}", "gateway": None},
    }


def test_infra_vlan_is_declared_as_pure_l2() -> None:
    vlans = get_vlans(interfaces=[_port("Ethernet11", _capability(20))])

    assert [(v["vlan_id"], v["name"], v["terminate_inline"], v["gateway_ip"], v["vrf"]) for v in vlans] == [
        (20, "transfer-fw", True, None, None)
    ]


def test_infra_vlan_carried_by_two_ports_is_declared_once() -> None:
    ports = [_port("Ethernet11", _capability(20)), _port("Ethernet12", _capability(20))]

    assert [v["vlan_id"] for v in get_vlans(interfaces=ports)] == [20]


def test_infra_vlans_follow_the_segment_vlans_sorted() -> None:
    vlans = get_vlans(activations=[_segment_activation(100)], interfaces=[_port("Ethernet11", _capability(20))])

    assert [v["vlan_id"] for v in vlans] == [20, 100]


def test_ports_without_infra_vlans_change_nothing() -> None:
    assert get_vlans(interfaces=[_port("Ethernet11")]) == []
    assert [v["vlan_id"] for v in get_vlans(activations=[_segment_activation(100)], interfaces=[_port("e1")])] == [100]


def test_port_carries_the_infra_vlan_as_access() -> None:
    ports = get_interfaces([_port("Ethernet11", _capability(20))])

    assert ports[0]["vlans"] == [20]
    assert ports[0]["mode"] is None


def test_port_with_two_infra_vlans_is_a_sorted_trunk() -> None:
    ports = get_interfaces([_port("Ethernet12", _capability(30, "mgmt-oob"), _capability(20))])

    assert ports[0]["vlans"] == [20, 30]
    assert ports[0]["mode"] == "trunk"
