"""Unit tests for the PAN-OS non-context path: one ``default`` virtual router holds every L3 interface.

An inter-zone policy needs both zones' interfaces in the same routing table, so the non-context path keeps a single
``default`` virtual router; its members come from the firewall interfaces (not from the route list), so an
inline-terminated interface (no static route) is a member too. The context path (a vsys with its own virtual router)
is covered by tests/unit/test_firewall_transform_contexts.py.
"""

from __future__ import annotations

from typing import Any

from tests.unit.firewall_transform_helpers import _render_template
from transforms.config.firewall import _default_vr_interfaces


def _iface(name: str, namespace: str | None, virtual_ip: str | None = None) -> dict[str, Any]:
    ip_obj: dict[str, Any] = {"address": "10.0.0.1/30"}
    if namespace:
        ip_obj["ip_namespace"] = {"name": namespace}
    return {"name": name, "ip_address": ip_obj, "virtual_ip": virtual_ip}


def _render(interfaces: list[str], routes: list[dict[str, str]]) -> list[str]:
    text = _render_template(
        "paloalto_panos",
        name="fw-1",
        fw_interfaces=[],
        zones=[],
        static_routes=routes,
        default_vr_interfaces=interfaces,
    )
    return [line for line in text.splitlines() if line.startswith("set network virtual-router")]


class TestDefaultVirtualRouterMembership:
    def test_inline_terminated_interface_is_a_member(self) -> None:
        names = _default_vr_interfaces(
            [_iface("ethernet1/2", "VRF-A", virtual_ip="10.0.0.9"), _iface("ethernet1/1", "VRF-B")]
        )

        assert names == ["ethernet1/1", "ethernet1/2"]

    def test_interface_without_a_namespace_is_excluded(self) -> None:
        assert _default_vr_interfaces([_iface("ethernet1/1", None)]) == []

    def test_members_are_deduplicated_and_sorted(self) -> None:
        assert _default_vr_interfaces([_iface("b", "N"), _iface("a", "N"), _iface("b", "N")]) == ["a", "b"]


class TestTemplate:
    def test_everything_lives_in_the_default_virtual_router(self) -> None:
        route = {"vrf": "VRF A", "destination": "10.0.1.0/24", "nexthop": "10.0.0.2", "interface": "e1", "zone": "z"}
        lines = _render(["e1", "e2"], [route])

        assert lines[:2] == [
            "set network virtual-router default interface e1",
            "set network virtual-router default interface e2",
        ]
        assert (
            "set network virtual-router default routing-table ip static-route VRF-A-1 destination 10.0.1.0/24" in lines
        )
        assert not [line for line in lines if "virtual-router vr-" in line]

    def test_no_members_no_interface_lines(self) -> None:
        assert _render([], []) == []
