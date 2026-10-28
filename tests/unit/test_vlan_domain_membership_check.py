"""Unit tests for CheckVlanDomainMembership (checks/vlan_domain_membership.py).

The payloads are raw GraphQL responses shaped like queries/validation/vlan_domain_membership.gql.
"""

from __future__ import annotations

from typing import Any, cast

from checks.vlan_domain_membership import CheckVlanDomainMembership


def _port(name: str, device: str, *, mlag: str | None = None) -> dict[str, Any]:
    """A raw port row; ``mlag`` is the MLAG the port's device is a member of."""
    capabilities = [{"node": {"id": mlag, "__typename": "ManagedMLAG"}}] if mlag else []
    return {
        "id": f"port-{device}-{name}",
        "name": {"value": name},
        "device": {
            "node": {"id": device, "name": {"value": device}, "capabilities": {"edges": capabilities}},
        },
    }


def _vlan(domain: str, ports: list[dict[str, Any]], *, vlan_id: int = 20) -> dict[str, Any]:
    return {
        "id": f"vlan-{vlan_id}",
        "name": {"value": "transfer-fw"},
        "vlan_id": {"value": vlan_id},
        "vlan_domain": {"node": {"id": domain, "__typename": "ManagedMLAG", "name": {"value": domain}}},
        "capabilities": {"edges": [{"node": port} for port in ports]},
    }


def _run(vlans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    check = cast(Any, CheckVlanDomainMembership.__new__(CheckVlanDomainMembership))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate({"ManagedVlan": {"edges": [{"node": vlan} for vlan in vlans]}})
    return findings


def test_ports_of_both_mlag_members_pass() -> None:
    """An MLAG pair is one domain: a port of either member is in it."""
    ports = [_port("Ethernet11", "leaf-1", mlag="mlag-1"), _port("Ethernet11", "leaf-2", mlag="mlag-1")]

    assert _run([_vlan("mlag-1", ports)]) == []


def test_port_of_a_device_without_mlag_is_in_its_own_domain() -> None:
    assert _run([_vlan("leaf-3", [_port("Ethernet11", "leaf-3")])]) == []


def test_port_of_a_device_of_another_domain_fails() -> None:
    findings = _run([_vlan("mlag-1", [_port("Ethernet11", "leaf-9", mlag="mlag-2")])])

    assert len(findings) == 1
    assert findings[0]["object_id"] == "port-leaf-9-Ethernet11"
    assert "not in that domain" in findings[0]["message"]


def test_vlan_on_an_mlag_member_as_its_own_domain_fails() -> None:
    """A member of an MLAG is never a domain itself: the VLAN belongs to the pair."""
    findings = _run([_vlan("leaf-1", [_port("Ethernet11", "leaf-1", mlag="mlag-1")])])

    assert len(findings) == 1


def test_only_the_foreign_port_is_reported() -> None:
    ports = [_port("Ethernet11", "leaf-1", mlag="mlag-1"), _port("Ethernet11", "leaf-9")]

    assert [f["object_id"] for f in _run([_vlan("mlag-1", ports)])] == ["port-leaf-9-Ethernet11"]


def test_vlan_without_ports_passes() -> None:
    assert _run([_vlan("mlag-1", [])]) == []
