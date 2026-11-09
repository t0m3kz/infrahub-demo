"""Unit tests for CheckPbrFirewallReachability, fed canned pbr_firewall_reachability responses."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.pbr_firewall_reachability import CheckPbrFirewallReachability
from tests.unit.vrf_instances_helpers import raw_service_port
from utils.exchange_transit import transit_vlan


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _leg(namespace: str = "PROD", ns_type: str | None = "prod", role: str = "internal-firewall") -> dict[str, Any]:
    """One leg spec: a sub-interface of a member of ``role`` addressed in ``namespace`` (``ns_type`` None: no address)."""
    return {"namespace": namespace, "ns_type": ns_type, "role": role}


def _context(
    ctx_id: str = "ctx-1",
    legs: list[dict[str, Any]] | None = None,
    served: list[str] | None = None,
    tenant: str | None = None,
) -> dict[str, Any]:
    """One ManagedFirewallContext with the legs its member holds (default: a single PROD leg)."""
    return {
        "id": ctx_id,
        "name": _v(f"{ctx_id}-name"),
        "tenant": {"node": {"id": tenant, "display_label": tenant} if tenant else None},
        "served_deployments": _edges([{"id": d, "display_label": d} for d in served or []]),
        "_legs": legs if legs is not None else [_leg()],
    }


def _member(context: dict[str, Any]) -> dict[str, Any]:
    """The firewall member of the context: it belongs to it and holds the sub-interfaces `eth1.<transit VLAN>`."""
    interfaces: list[dict[str, Any]] = [
        {"__typename": "DcimPhysicalInterface", "id": "up", "name": _v("eth1"), "role": _v("uplink")}
    ]
    for leg in context["_legs"]:
        sub: dict[str, Any] = {
            "__typename": "DcimVirtualInterface",
            "id": f"sub-{leg['namespace']}",
            "name": _v(f"eth1.{transit_vlan(3000, leg['ns_type'] or 'prod')}"),
            "vlan_id": _v(transit_vlan(3000, leg["ns_type"] or "prod")),
            "parent_interface": {"node": {"name": _v("eth1")}},
            "role": _v("service"),
            "ip_address": {"node": {"ip_namespace": {"node": {"name": _v("default")}}}}
            if leg["ns_type"]
            else {"node": None},
            "vrf": {"node": {"security_zone": {"node": {"name": _v(leg["namespace"]), "vrf_type": _v(leg["ns_type"])}}}}
            if leg["ns_type"]
            else {"node": None},
        }
        interfaces.append(sub)
    role = context["_legs"][0]["role"] if context["_legs"] else "internal-firewall"
    return {
        "id": f"member-{context['id']}",
        "name": _v(f"fw-{context['id']}"),
        "role": _v(role),
        "shared_capabilities": _edges(
            [{"__typename": "ManagedFirewallContext", "id": context["id"], "vlan_id": _v(3000)}]
        ),
        "interfaces": _edges(interfaces),
    }


def _port(device_role: str, context_ids: list[str]) -> dict[str, Any]:
    """One border or edge device (of ``device_role``) with a firewall-role port facing a member of each context."""
    return {
        "id": f"dev-{device_role}",
        "name": _v(f"dev-{device_role}"),
        "role": _v(device_role),
        "service_ports": _edges(
            [
                raw_service_port(f"Ethernet{n}", "firewall", "ManagedFirewallContext", 3000, context_id=cid)
                for n, cid in enumerate(context_ids, start=1)
            ]
        ),
    }


def _run(contexts: list[dict[str, Any]], carriers: list[dict[str, Any]] | None = None) -> list[str]:
    """Run the check against canned data and return the captured errors."""
    check = cast(Any, CheckPbrFirewallReachability.__new__(CheckPbrFirewallReachability))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate(
        {
            "ManagedFirewallContext": _edges([{k: v for k, v in ctx.items() if k != "_legs"} for ctx in contexts]),
            "FirewallMembers": _edges([_member(ctx) for ctx in contexts]),
            "CarrierDevices": _edges(carriers or []),
        }
    )
    return errors


class TestCarried:
    """Contexts the check accepts."""

    @pytest.mark.parametrize("role", ["border-leaf", "border-spine", "edge"])
    def test_context_carried_by_border_or_edge_port_passes(self, role: str) -> None:
        """A firewall-role port on a border or edge tagged with the context carries it."""
        assert _run([_context(served=["dep-a"])], carriers=[_port(role, ["ctx-1"])]) == []

    def test_dedicated_context_carried_passes(self) -> None:
        """A dedicated context (tenant, no served list) is served by its tenant."""
        assert _run([_context(tenant="dep-a")], carriers=[_port("border-leaf", ["ctx-1"])]) == []

    def test_shared_context_carried_beside_another_context_on_the_device_passes(self) -> None:
        """Another context faced by the device does not hide this one."""
        carriers = [_port("border-leaf", ["ctx-2", "ctx-1"])]
        contexts = [_context("ctx-1", served=["dep-a", "dep-b"]), _context("ctx-2", served=["dep-a"])]
        assert _run(contexts, carriers=carriers) == []


class TestNotCarried:
    """Contexts the check rejects."""

    def test_served_context_without_any_port_is_an_error(self) -> None:
        """No firewall-role port tags the context."""
        errors = _run([_context(served=["dep-a"])])
        assert len(errors) == 1
        assert "ctx-1-name" in errors[0]
        assert "PROD" in errors[0]
        assert "dep-a" in errors[0]

    def test_dedicated_context_without_a_port_names_the_tenant(self) -> None:
        """A dedicated context reports its tenant and the nonprod VRF."""
        legs = [_leg("NONPROD", "non_prod")]
        errors = _run([_context(legs=legs, tenant="dep-t")])
        assert len(errors) == 1
        assert "NONPROD" in errors[0]
        assert "dep-t" in errors[0]

    def test_port_on_a_leaf_does_not_carry_the_context(self) -> None:
        """Only border and edge devices host the VIP."""
        assert len(_run([_context(served=["dep-a"])], carriers=[_port("leaf", ["ctx-1"])])) == 1

    def test_port_tagged_with_another_context_does_not_carry_it(self) -> None:
        """A carried context does not cover its sibling."""
        errors = _run(
            [_context("ctx-1", served=["d"]), _context("ctx-2", served=["d"])], carriers=[_port("edge", ["ctx-2"])]
        )
        assert len(errors) == 1
        assert "ctx-1-name" in errors[0]


class TestSkipped:
    """Contexts out of scope."""

    @pytest.mark.parametrize(
        "legs",
        [
            [_leg("INTERNET", "internet")],
            [_leg("MGMT", "management")],
            [_leg(ns_type=None)],
            [_leg(role="leaf")],
            [],
        ],
        ids=["internet-only", "management-only", "no-address", "non-firewall-member", "no-legs"],
    )
    def test_context_without_a_tenant_vrf_leg_is_skipped(self, legs: list[dict[str, Any]]) -> None:
        """Only PROD and NONPROD legs need a carrier."""
        assert _run([_context(legs=legs, served=["dep-a"])]) == []

    def test_context_serving_nothing_is_skipped(self) -> None:
        """No served deployment and no tenant: nothing is redirected to it."""
        assert _run([_context()]) == []

    def test_empty_data_passes(self) -> None:
        """No contexts, no errors."""
        assert _run([]) == []
