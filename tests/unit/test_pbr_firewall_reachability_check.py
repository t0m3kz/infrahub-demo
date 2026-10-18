"""Unit tests for CheckPbrFirewallReachability, fed canned pbr_firewall_reachability responses."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.pbr_firewall_reachability import CheckPbrFirewallReachability


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _leg(namespace: str = "PROD", ns_type: str | None = "prod", role: str = "firewall") -> dict[str, Any]:
    """One leg (a firewall member's VLAN sub-interface) in the raw query shape."""
    ip_address: dict[str, Any] = {"node": None}
    if ns_type:
        ip_address = {"node": {"ip_namespace": {"node": {"name": _v(namespace), "namespace_type": _v(ns_type)}}}}
    return {"node": {"device": {"node": {"role": _v(role)}}, "ip_address": ip_address}}


def _context(
    ctx_id: str = "ctx-1",
    legs: list[dict[str, Any]] | None = None,
    served: list[str] | None = None,
    tenant: str | None = None,
) -> dict[str, Any]:
    """One ManagedFirewallContext edge; ``legs`` defaults to a single PROD leg."""
    return {
        "node": {
            "id": ctx_id,
            "name": _v(f"{ctx_id}-name"),
            "tenant": {"node": {"id": tenant, "display_label": tenant} if tenant else None},
            "served_deployments": {"edges": [{"node": {"id": d, "display_label": d}} for d in served or []]},
            "capabilities": {"edges": legs if legs is not None else [_leg()]},
        }
    }


def _port(device_role: str, context_ids: list[str], extra_kind: bool = False) -> dict[str, Any]:
    """One firewall-role port on a device of ``device_role`` tagged with the given contexts."""
    caps = [{"node": {"__typename": "ManagedFirewallContext", "id": cid}} for cid in context_ids]
    if extra_kind:
        caps.append({"node": {"__typename": "RoutingOSPFInterface"}})
    return {
        "node": {
            "id": "port",
            "device": {"node": {"name": _v("dev"), "role": _v(device_role)}},
            "capabilities": {"edges": caps},
        }
    }


def _run(
    contexts: list[dict[str, Any]],
    physical: list[dict[str, Any]] | None = None,
    virtual: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Run the check against canned data and return the captured errors."""
    check = cast(Any, CheckPbrFirewallReachability.__new__(CheckPbrFirewallReachability))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate(
        {
            "ManagedFirewallContext": {"edges": contexts},
            "FirewallPhysicalPorts": {"edges": physical or []},
            "FirewallVirtualPorts": {"edges": virtual or []},
        }
    )
    return errors


class TestCarried:
    """Contexts the check accepts."""

    @pytest.mark.parametrize("role", ["border-leaf", "border-spine", "edge"])
    def test_context_carried_by_border_or_edge_port_passes(self, role: str) -> None:
        """A firewall-role port on a border or edge tagged with the context carries it."""
        assert _run([_context(served=["dep-a"])], physical=[_port(role, ["ctx-1"])]) == []

    def test_context_carried_by_a_virtual_port_passes(self) -> None:
        """A virtual (LAG) firewall-role port carries it as well."""
        assert _run([_context(served=["dep-a"])], virtual=[_port("edge", ["ctx-1"])]) == []

    def test_dedicated_context_carried_passes(self) -> None:
        """A dedicated context (tenant, no served list) is served by its tenant."""
        assert _run([_context(tenant="dep-a")], physical=[_port("border-leaf", ["ctx-1"])]) == []

    def test_shared_context_carried_with_other_kinds_on_the_port_passes(self) -> None:
        """Other capabilities on the port do not hide the context tag."""
        ports = [_port("border-leaf", ["ctx-2", "ctx-1"], extra_kind=True)]
        assert _run([_context(served=["dep-a", "dep-b"])], physical=ports) == []


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
        """A dedicated context reports its tenant and the non-prod VRF."""
        legs = [_leg("NON-PROD", "non_prod")]
        errors = _run([_context(legs=legs, tenant="dep-t")])
        assert len(errors) == 1
        assert "NON-PROD" in errors[0]
        assert "dep-t" in errors[0]

    def test_port_on_a_leaf_does_not_carry_the_context(self) -> None:
        """Only border and edge devices host the VIP."""
        assert len(_run([_context(served=["dep-a"])], physical=[_port("leaf", ["ctx-1"])])) == 1

    def test_port_tagged_with_another_context_does_not_carry_it(self) -> None:
        """A carried context does not cover its sibling."""
        errors = _run([_context("ctx-1", served=["d"]), _context("ctx-2", served=["d"])], [_port("edge", ["ctx-2"])])
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
        """Only PROD and NON-PROD legs need a carrier."""
        assert _run([_context(legs=legs, served=["dep-a"])]) == []

    def test_context_serving_nothing_is_skipped(self) -> None:
        """No served deployment and no tenant: nothing is redirected to it."""
        assert _run([_context()]) == []

    def test_empty_data_passes(self) -> None:
        """No contexts, no errors."""
        assert _run([]) == []
