"""Unit tests for CheckVirtualNicPinning (checks/virtual_nic_pinning.py).

The payloads are raw GraphQL responses shaped like queries/validation/virtual_nic_pinning.gql.
"""

from __future__ import annotations

from typing import Any, cast

from checks.virtual_nic_pinning import CheckVirtualNicPinning


def _nic(
    name: str, role: str, *, parent_device: str | None = None, cabled: bool = True, kind: str = "DcimVirtualInterface"
) -> dict[str, Any]:
    """A raw interface row; ``parent_device`` None is an unpinned NIC."""
    node: dict[str, Any] = {"id": f"nic-{name}", "__typename": kind, "name": {"value": name}, "role": {"value": role}}
    if kind == "DcimVirtualInterface":
        parent = (
            {
                "id": f"port-{name}",
                "name": {"value": name},
                "device": {"node": {"id": parent_device, "name": {"value": parent_device}}},
                "cable": {"node": {"id": f"cable-{name}"} if cabled else None},
            }
            if parent_device
            else None
        )
        node["parent_interface"] = {"node": parent}
    return node


def _device(nics: list[dict[str, Any]], host: str | None = "fw-1") -> dict[str, Any]:
    return {
        "id": "vm-1",
        "name": {"value": "fw-1-C007-dedicated"},
        "hosting_device": {"node": {"id": host, "name": {"value": host}} if host else None},
        "interfaces": {"edges": [{"node": nic} for nic in nics]},
    }


def _run(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    check = cast(Any, CheckVirtualNicPinning.__new__(CheckVirtualNicPinning))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate({"DcimVirtualDevice": {"edges": [{"node": device} for device in devices]}})
    return findings


def test_nics_pinned_to_cabled_ports_of_the_host_pass() -> None:
    nics = [_nic("eth0", "management", parent_device="fw-1"), _nic("eth1", "uplink", parent_device="fw-1")]

    assert _run([_device(nics)]) == []


def test_unpinned_nic_passes() -> None:
    """A machine can have more uplinks than its host: eth3 has no host port left."""
    assert _run([_device([_nic("eth3", "uplink")])]) == []


def test_nic_pinned_to_a_port_of_another_device_fails() -> None:
    findings = _run([_device([_nic("eth1", "uplink", parent_device="fw-2")])])

    assert len(findings) == 1
    assert findings[0]["object_id"] == "nic-eth1"
    assert "not of its hosting device 'fw-1'" in findings[0]["message"]


def test_uplink_pinned_to_an_uncabled_host_port_fails() -> None:
    findings = _run([_device([_nic("eth1", "uplink", parent_device="fw-1", cabled=False)])])

    assert len(findings) == 1
    assert "which is not cabled" in findings[0]["message"]


def test_management_nic_on_an_uncabled_host_port_passes() -> None:
    """Only an uplink must reach the border leaf; the management port needs no cable."""
    assert _run([_device([_nic("eth0", "management", parent_device="fw-1", cabled=False)])]) == []


def test_context_sub_interface_is_not_a_base_nic() -> None:
    """A tenant sub-interface (role service) rides on a NIC of its own device, not on a host port."""
    assert _run([_device([_nic("eth1.3000", "service", parent_device="vm-1")])]) == []


def test_device_without_a_host_is_not_reported() -> None:
    assert _run([_device([_nic("eth1", "uplink", parent_device="fw-2")], host=None)]) == []
