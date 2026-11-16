"""Unit tests for CheckInterfaceParents (checks/devices/interface_parents.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from checks.devices.interface_parents import CheckInterfaceParents

QUERY = Path(__file__).resolve().parents[3] / "queries" / "validation" / "interface_parents.gql"
ROOT = "DcimSubInterface"


def _device(device_id: str, kind: str = "DcimVirtualDevice", host: str | None = "fw-1") -> dict[str, Any]:
    """A raw ``device`` relationship; only a virtual device has a ``hosting_device``."""
    node: dict[str, Any] = {"id": device_id, "__typename": kind, "name": {"value": device_id}}
    if kind == "DcimVirtualDevice":
        node["hosting_device"] = {"node": {"id": host, "name": {"value": host}} if host else None}
    return {"node": node}


def _parent(
    name: str,
    device_id: str,
    kind: str = "DcimPhysicalInterface",
    cabled: bool = True,
    device_kind: str = "DcimPhysicalDevice",
) -> dict[str, Any]:
    """A raw ``parent`` relationship of a sub-interface."""
    node: dict[str, Any] = {
        "id": f"port-{name}",
        "__typename": kind,
        "name": {"value": name},
        "device": {"node": {"id": device_id, "__typename": device_kind, "name": {"value": device_id}}},
    }
    if kind == "DcimPhysicalInterface":
        node["cable"] = {"node": {"id": f"cable-{name}"} if cabled else None}
    return {"node": node}


def _iface(name: str, role: str, device: dict[str, Any], kind: str = "DcimVirtualInterface") -> dict[str, Any]:
    """A raw interface row."""
    return {
        "id": f"nic-{name}",
        "__typename": kind,
        "name": {"value": name},
        "role": {"value": role},
        "device": device,
    }


def _riding(iface: dict[str, Any], parent: dict[str, Any]) -> dict[str, Any]:
    """A raw sub-interface row: the interface with the ``parent`` it rides on."""
    return {**iface, "parent": parent}


def _on(
    name: str, role: str, device: dict[str, Any], parent: dict[str, Any], kind: str = "DcimVirtualInterface"
) -> dict:
    """The sub-interface row of one interface ``name`` on ``parent``."""
    return _riding(_iface(name, role, device, kind), parent)


def _peering(name: str, parent: dict[str, Any]) -> dict[str, Any]:
    """The sub-interface row of a CloudPeeringInterface of the transport ``dx``: no role, its device is no virtual device."""
    peering = _iface(name, "", _device("dx", kind="CloudDirectConnect"), "CloudPeeringInterface")
    del peering["role"]
    return _riding(peering, parent)


def _nic(name: str, role: str, host_device: str, cabled: bool = True, kind: str | None = None) -> dict:
    """The sub-interface row of a NIC of the VM ``vm-1`` hosted by ``fw-1``, pinned to a port of ``host_device``."""
    parent = _parent(name, host_device, kind or "DcimPhysicalInterface", cabled)
    return _on(name, role, _device("vm-1"), parent)


def _provider_interface(name: str, parent: dict[str, Any]) -> dict[str, Any]:
    """The sub-interface row of a TopologyProviderInterface: the end of a Fabric connection on a port of the connect."""
    device = {"node": {"id": "ext-fabric", "__typename": "TopologyProviderConnect", "name": {"value": "ext-fabric"}}}
    iface = _iface(name, "", device, "TopologyProviderInterface")
    del iface["role"]
    return _riding(iface, parent)


def _run_full(*rows: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """(errors, log_info lines) of the check on raw sub-interface rows."""
    check = cast(Any, CheckInterfaceParents.__new__(CheckInterfaceParents))
    findings: list[dict[str, Any]] = []
    infos: list[str] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.log_info = lambda message, **_: infos.append(message)
    check.validate({ROOT: {"edges": [{"node": enc} for enc in rows]}})
    return findings, infos


def _run(*rows: dict[str, Any]) -> list[dict[str, Any]]:
    """The errors of the check on raw sub-interface rows."""
    return _run_full(*rows)[0]


def test_nics_pinned_to_cabled_ports_of_the_host_pass() -> None:
    """Rules 1-3: management and uplink NICs on ports of the hosting device."""
    assert _run(_nic("eth0", "management", "fw-1"), _nic("eth1", "uplink", "fw-1")) == []


def test_nic_without_a_parent_is_not_reported() -> None:
    """Rule 4: a machine can have more uplinks than its host: eth3 has no host port left, so no parent."""
    assert _run() == []


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
    sub = _on("eth1.3000", "service", _device("vm-1"), _parent("eth1", "vm-1", "DcimVirtualInterface"))
    assert _run(sub) == []


def test_cross_device_sub_interface_on_a_physical_device_fails() -> None:
    """Rule 1: a sub-interface of a physical device whose parent is a port of another device."""
    sub = _on("Ethernet5.100", "service", _device("bl-1", "DcimPhysicalDevice"), _parent("Ethernet5", "bl-2"))
    findings = _run(sub)

    assert len(findings) == 1 and "another device" in findings[0]["message"]


def test_nic_on_a_physical_device_with_a_cross_device_parent_fails() -> None:
    """Rule 1: the NIC exemption is for virtual devices only."""
    nic = _on("eth1", "uplink", _device("bl-1", "DcimPhysicalDevice"), _parent("Ethernet5", "bl-2"))
    assert len(_run(nic)) == 1


def test_sub_interface_of_a_vm_riding_on_a_host_port_fails() -> None:
    """Rule 1: only a NIC may ride on the host's port, a service sub-interface of the VM may not."""
    sub = _on("eth1.3000", "service", _device("vm-1"), _parent("Ethernet5", "fw-1"))
    assert len(_run(sub)) == 1


def test_sub_interface_whose_parent_is_on_another_device_fails() -> None:
    """Rule 1: a sub-interface on a virtual device with a parent of a different virtual device."""
    sub = _on("eth1.3000", "service", _device("vm-1"), _parent("eth1", "vm-2", "DcimVirtualInterface"))
    findings = _run(sub)

    assert len(findings) == 1 and "'vm-2'" in findings[0]["message"]


def test_nic_of_a_device_without_a_host_is_not_reported_on_its_own_device() -> None:
    """A virtual device with no hosting_device: a parent on its own device passes, one elsewhere fails (rule 1)."""
    own = _on("eth1", "uplink", _device("vm-1", host=None), _parent("eth0", "vm-1", "DcimVirtualInterface"))
    elsewhere = _on("eth2", "uplink", _device("vm-1", host=None), _parent("Ethernet5", "fw-2"))
    assert _run(own) == []
    assert len(_run(elsewhere)) == 1


def test_a_peering_riding_on_a_port_of_its_transport_passes() -> None:
    """Rule 1 covers the cloud peering interface: the parent is on its own transport."""
    assert _run(_peering("lag1.1000", _parent("lag1", "dx", "DcimLAGInterface"))) == []


def test_a_peering_riding_on_a_port_of_another_device_fails() -> None:
    """Rule 1: the edge accepts the port of the partner's transport; the check reports it."""
    findings = _run(_peering("er-pri", _parent("partner-port", "partner")))

    assert len(findings) == 1 and "'partner'" in findings[0]["message"]
    assert findings[0]["object_type"] == "CloudPeeringInterface"


def test_a_peering_riding_on_a_port_of_the_provider_connect_that_holds_it_passes() -> None:
    """Rule 1: a Direct Connect virtual interface rides on the LAG of the provider's device in the cage."""
    lag = _parent("lag1", "aws-dx-par", "DcimLAGInterface", device_kind="TopologyProviderConnect")

    assert _run(_peering("lag1.1000", lag)) == []


def test_a_virtual_interface_on_a_port_of_a_provider_connect_still_fails() -> None:
    """Only a cloud peering is exempt: a sub-interface of a device never rides on a provider's port."""
    port = _parent("dxcon-1a", "aws-dx-par", device_kind="TopologyProviderConnect")
    findings = _run(_on("Ethernet1/3.1", "svi", _device("edge", kind="DcimPhysicalDevice"), port))

    assert len(findings) == 1 and "'aws-dx-par'" in findings[0]["message"]


def test_a_provider_interface_riding_on_a_port_of_its_own_provider_connect_passes() -> None:
    """00_full: et-0/0/0.1101 of EXT-FABRIC-PAR rides on et-0/0/0 of EXT-FABRIC-PAR (the same device)."""
    parent = _parent("et-0/0/0", "ext-fabric", device_kind="TopologyProviderConnect")

    assert _run(_provider_interface("et-0/0/0.1101", parent)) == []


def test_a_provider_interface_on_a_port_of_another_device_fails() -> None:
    """Rule 1 holds for it too: the connection rides on the port of ITS OWN provider connect."""
    parent = _parent("et-0/0/0", "ext-other", device_kind="TopologyProviderConnect")
    findings = _run(_provider_interface("et-0/0/0.1101", parent))

    assert len(findings) == 1 and "'ext-other'" in findings[0]["message"]
    assert findings[0]["object_type"] == "TopologyProviderInterface"


# -- the rules of the sub-interface itself (7) ---------------------------------------------------------------------------


def test_an_interface_that_is_its_own_parent_fails() -> None:
    """Rule 7: the parent edge points back at the interface itself."""
    own = {"node": {**_parent("eth0", "vm-1", "DcimVirtualInterface")["node"], "id": "nic-eth0"}}
    findings = _run(_on("eth0", "service", _device("vm-1"), own))

    assert len(findings) == 1 and "is its own parent" in findings[0]["message"]
    assert findings[0]["object_id"] == "nic-eth0"


def test_a_well_formed_set_logs_neither_errors_nor_warnings() -> None:
    """A parent of the right device, not the interface itself: silent."""
    findings, infos = _run_full(_nic("eth0", "management", "fw-1"), _nic("eth1", "uplink", "fw-1"))

    assert findings == [] and infos == []
