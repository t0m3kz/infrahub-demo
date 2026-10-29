"""Unit tests for CheckInterfaceParents (checks/interface_parents.py).

The payloads are raw GraphQL responses shaped like queries/validation/interface_parents.gql, run through clean_data.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.interface_parents import CheckInterfaceParents
from tests.unit.query_shape_helpers import query_tree, unselected

QUERY = Path(__file__).resolve().parents[2] / "queries" / "validation" / "interface_parents.gql"


def _device(device_id: str, kind: str = "DcimVirtualDevice", host: str | None = "fw-1") -> dict[str, Any]:
    """A raw ``device`` relationship; only a virtual device has a ``hosting_device``."""
    node: dict[str, Any] = {"id": device_id, "__typename": kind, "name": {"value": device_id}}
    if kind == "DcimVirtualDevice":
        node["hosting_device"] = {"node": {"id": host, "name": {"value": host}} if host else None}
    return {"node": node}


def _parent(
    name: str, device_id: str, kind: str = "DcimPhysicalInterface", cabled: bool = True
) -> dict[str, Any] | None:
    """A raw ``parent_interface`` relationship."""
    node: dict[str, Any] = {
        "id": f"port-{name}",
        "__typename": kind,
        "name": {"value": name},
        "device": {"node": {"id": device_id, "name": {"value": device_id}}},
    }
    if kind == "DcimPhysicalInterface":
        node["cable"] = {"node": {"id": f"cable-{name}"} if cabled else None}
    return {"node": node}


def _iface(
    name: str, role: str, device: dict[str, Any], parent: dict[str, Any] | None = None, *caps: str
) -> dict[str, Any]:
    """A raw DcimVirtualInterface row; ``parent`` None is an unset parent_interface, ``caps`` the capability kinds."""
    return {
        "id": f"nic-{name}",
        "__typename": "DcimVirtualInterface",
        "name": {"value": name},
        "role": {"value": role},
        "device": device,
        "parent_interface": parent if parent is not None else {"node": None},
        "capabilities": {"edges": [{"node": {"__typename": kind}} for kind in caps]},
    }


def _nic(name: str, role: str, host_device: str | None = None, cabled: bool = True, kind: str | None = None) -> dict:
    """A NIC of the VM ``vm-1`` hosted by ``fw-1``; ``host_device`` None is an unpinned NIC."""
    parent = _parent(name, host_device, kind or "DcimPhysicalInterface", cabled) if host_device else None
    return _iface(name, role, _device("vm-1"), parent)


def _run(*ifaces: dict[str, Any]) -> list[dict[str, Any]]:
    check = cast(Any, CheckInterfaceParents.__new__(CheckInterfaceParents))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate({"DcimVirtualInterface": {"edges": [{"node": iface} for iface in ifaces]}})
    return findings


def test_nics_pinned_to_cabled_ports_of_the_host_pass() -> None:
    """Rules 1-3: management and uplink NICs on ports of the hosting device."""
    assert _run(_nic("eth0", "management", "fw-1"), _nic("eth1", "uplink", "fw-1")) == []


def test_unpinned_nic_passes() -> None:
    """Rule 4: a machine can have more uplinks than its host: eth3 has no host port left."""
    assert _run(_nic("eth3", "uplink")) == []


def test_nic_pinned_to_a_port_of_another_device_fails() -> None:
    """Rule 2: the host port is on a device that is not the hosting one."""
    findings = _run(_nic("eth1", "uplink", "fw-2"))

    assert len(findings) == 1
    assert findings[0]["object_id"] == "nic-eth1"
    assert "not of its hosting device 'fw-1'" in findings[0]["message"]


def test_uplink_pinned_to_an_uncabled_host_port_fails() -> None:
    """Rule 3."""
    findings = _run(_nic("eth1", "uplink", "fw-1", cabled=False))

    assert len(findings) == 1
    assert "which is not cabled" in findings[0]["message"]


def test_management_nic_on_an_uncabled_host_port_passes() -> None:
    """Only an uplink must reach the border leaf; the management port needs no cable."""
    assert _run(_nic("eth0", "management", "fw-1", cabled=False)) == []


def test_nic_whose_parent_is_not_a_physical_port_fails() -> None:
    """Rule 5: the host port is a physical port, not a LAG."""
    findings = _run(_nic("eth1", "uplink", "fw-1", kind="DcimLAGInterface"))

    assert len(findings) == 1 and "not a physical host port" in findings[0]["message"]


def test_sub_interface_of_a_nic_on_the_same_vm_passes() -> None:
    """Rule 1: a context sub-interface (role service) rides on a NIC of its own device."""
    sub = _iface("eth1.3000", "service", _device("vm-1"), _parent("eth1", "vm-1", "DcimVirtualInterface"))
    assert _run(sub) == []


def test_cross_device_sub_interface_on_a_physical_device_fails() -> None:
    """Rule 1: a sub-interface of a physical device whose parent is a port of another device."""
    sub = _iface("Ethernet5.100", "service", _device("bl-1", "DcimPhysicalDevice"), _parent("Ethernet5", "bl-2"))
    findings = _run(sub)

    assert len(findings) == 1 and "another device" in findings[0]["message"]


def test_nic_on_a_physical_device_with_a_cross_device_parent_fails() -> None:
    """Rule 1: the NIC exemption is for virtual devices only."""
    nic = _iface("eth1", "uplink", _device("bl-1", "DcimPhysicalDevice"), _parent("Ethernet5", "bl-2"))
    assert len(_run(nic)) == 1


def test_sub_interface_of_a_vm_riding_on_a_host_port_fails() -> None:
    """Rule 1: only a NIC may ride on the host's port, a service sub-interface of the VM may not."""
    sub = _iface("eth1.3000", "service", _device("vm-1"), _parent("Ethernet5", "fw-1"))
    assert len(_run(sub)) == 1


def test_sub_interface_whose_parent_is_on_another_device_fails() -> None:
    """Rule 1: a sub-interface on a virtual device with a parent of a different virtual device."""
    sub = _iface("eth1.3000", "service", _device("vm-1"), _parent("eth1", "vm-2", "DcimVirtualInterface"))
    findings = _run(sub)

    assert len(findings) == 1 and "'vm-2'" in findings[0]["message"]


def test_nic_of_a_device_without_a_host_is_not_reported_on_its_own_device() -> None:
    """A virtual device with no hosting_device: a parent on its own device passes, one elsewhere fails (rule 1)."""
    own = _iface("eth1", "uplink", _device("vm-1", host=None), _parent("eth0", "vm-1", "DcimVirtualInterface"))
    elsewhere = _iface("eth2", "uplink", _device("vm-1", host=None), _parent("Ethernet5", "fw-2"))
    assert _run(own) == []
    assert len(_run(elsewhere)) == 1


def _termination(name: str, parent: str | None, *caps: str) -> dict[str, Any]:
    """A sub-interface of the physical device bl-1 whose parent is ``parent`` on the same device."""
    return _iface(
        name, "service", _device("bl-1", "DcimPhysicalDevice"), _parent(parent, "bl-1") if parent else None, *caps
    )


def test_rule_6_inline_termination_on_the_dotted_sub_interface_passes() -> None:
    """Rule 6: <parent name>.<vlan> carrying the segment."""
    assert _run(_termination("Ethernet5.100", "Ethernet5", "ManagedVxlanSegment")) == []


def test_rule_6_segment_on_a_nic_or_a_misnamed_sub_interface_fails() -> None:
    """Rule 6: no parent, a name that is not the parent's, or a non-numeric vlan."""
    for bad in (
        _termination("eth1", None, "ManagedVxlanSegment"),
        _termination("Ethernet6.100", "Ethernet5", "ManagedVxlanSegment"),
        _termination("Ethernet5.x", "Ethernet5", "ManagedVxlanSegment"),
    ):
        findings = _run(bad)
        assert len(findings) == 1 and "inline termination" in findings[0]["message"]


def test_rule_6_ignores_virtual_interfaces_without_a_segment() -> None:
    """Rule 6: a context leg, an SVI or a NIC without a segment is not its concern."""
    assert _run(_termination("eth1", None, "ManagedFirewallContext"), _termination("Vlan100", None)) == []


def test_the_fixtures_emit_only_fields_the_query_selects_and_the_parent_fields_are_selected() -> None:
    """The builders match interface_parents.gql, which must select parent_interface, its cable and the capability kinds."""
    node = query_tree(QUERY)["DcimVirtualInterface"]["edges"]["node"]
    assert {"parent_interface", "capabilities", "role", "device"} <= set(node)
    assert "cable" in node["parent_interface"]["node"]
    fixtures = [_nic("eth1", "uplink", "fw-1"), _termination("Ethernet5.100", "Ethernet5", "ManagedVxlanSegment")]
    assert unselected(fixtures, node) == []
