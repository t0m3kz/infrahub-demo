"""Unit tests for CheckTenantSviProtection, fed canned tenant_svi_protection query responses."""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.tenant_svi_protection import CheckTenantSviProtection


def _v(value: Any) -> dict[str, Any]:
    return {"value": value}


def _segment(
    seg_id: str = "seg-a",
    kind: str = "ManagedVxlanSegment",
    namespace_type: str | None = "prod",
    zone: str = "PROD-ZONE",
    policy: dict[str, Any] | None = None,
    inline: bool = False,
    deployments: list[str] | None = None,
) -> dict[str, Any]:
    """One ManagedNetworkSegment edge in the raw query shape."""
    gateway: dict[str, Any] = {"node": None}
    if namespace_type:
        namespace = {"node": {"name": _v("NS"), "namespace_type": _v(namespace_type)}}
        gateway = {"node": {"ip_prefix": {"node": {"prefix": _v("10.0.0.0/24"), "ip_namespace": namespace}}}}
    node: dict[str, Any] = {
        "__typename": kind,
        "id": seg_id,
        "name": _v(seg_id),
        "security_zone": {"node": {"name": _v(zone)}},
        "gateway": gateway,
        "inline_service": {"node": {"id": "fw"} if inline else None},
        "security_policy": {"node": policy},
    }
    if kind == "ManagedVxlanSegment":
        node["customer_deployments"] = {"edges": [{"node": {"id": d}} for d in deployments or []]}
    else:
        node["customer_deployment"] = {"node": {"id": deployments[0]} if deployments else None}
    return {"node": node}


def _policy(*, enabled: bool = True, rules: int = 0, default_action: str | None = "deny") -> dict[str, Any]:
    return {
        "id": "pol",
        "enabled": _v(enabled),
        "default_action": _v(default_action),
        "rules": {"count": rules},
    }


def _context(tenant: str | None = None, served: list[str] | None = None) -> dict[str, Any]:
    return {
        "node": {
            "id": "ctx",
            "tenant": {"node": {"id": tenant} if tenant else None},
            "served_deployments": {"edges": [{"node": {"id": d}} for d in served or []]},
        }
    }


def _run(segments: list[dict[str, Any]], contexts: list[dict[str, Any]] | None = None) -> list[str]:
    """Run the check against canned data and return the captured errors."""
    check = cast(Any, CheckTenantSviProtection.__new__(CheckTenantSviProtection))
    errors: list[str] = []
    check.log_error = lambda message: errors.append(message)
    check.validate(
        {
            "ManagedNetworkSegment": {"edges": segments},
            "ManagedFirewallContext": {"edges": contexts or []},
        }
    )
    return errors


class TestProtected:
    """Segments the check accepts."""

    @pytest.mark.parametrize("namespace_type", ["prod", "non_prod"])
    def test_segment_with_an_acl_bearing_policy_passes(self, namespace_type: str) -> None:
        """An enabled policy with a default action renders the SVI ACL."""
        assert _run([_segment(namespace_type=namespace_type, policy=_policy())]) == []

    def test_segment_with_rules_and_no_default_action_passes(self) -> None:
        """Rules alone also make the policy ACL-bearing."""
        assert _run([_segment(policy=_policy(rules=2, default_action=None))]) == []

    def test_segment_with_an_inline_firewall_passes(self) -> None:
        """The inline firewall protects it."""
        assert _run([_segment(inline=True)]) == []

    def test_segment_covered_by_a_shared_context_passes(self) -> None:
        """PBR default-redirects to the shared (tenant-less) context."""
        assert _run([_segment()], [_context()]) == []

    def test_segment_served_by_a_dedicated_context_passes(self) -> None:
        """PBR redirects a served customer deployment to its dedicated context."""
        assert _run([_segment(deployments=["dep-a"])], [_context(tenant="dep-x", served=["dep-a"])]) == []

    def test_vlan_segment_with_a_dedicated_tenant_context_passes(self) -> None:
        """customer_deployment (single) matches the context tenant."""
        segment = _segment(kind="ManagedVlanSegment", deployments=["dep-a"])
        assert _run([segment], [_context(tenant="dep-a")]) == []

    @pytest.mark.parametrize(
        "segment",
        [
            _segment(namespace_type="internet"),
            _segment(namespace_type="management"),
            _segment(namespace_type=None),
            _segment(zone="INTERNET-ZONE"),
            _segment(kind="ManagedExternalSegment"),
            _segment(kind="CloudNetworkSegment"),
        ],
        ids=["internet-vrf", "management-vrf", "l2-only", "exchange-zone", "external", "cloud"],
    )
    def test_non_tenant_or_non_routed_segments_are_exempt(self, segment: dict[str, Any]) -> None:
        """Only routed PROD/NON-PROD tenant SVIs are in scope."""
        assert _run([segment]) == []


class TestUnprotected:
    """Segments the check rejects."""

    def test_routed_tenant_segment_without_any_protection_is_an_error(self) -> None:
        """No policy, no firewall, no context: any-to-any inside the VRF."""
        errors = _run([_segment()])
        assert len(errors) == 1
        assert "seg-a" in errors[0]
        assert "any-to-any" in errors[0]

    def test_disabled_policy_is_not_an_acl(self) -> None:
        """A disabled policy renders nothing."""
        assert len(_run([_segment(policy=_policy(enabled=False, rules=3))])) == 1

    def test_policy_without_rules_or_default_action_is_not_an_acl(self) -> None:
        """An empty policy shell carries no ACL."""
        assert len(_run([_segment(policy=_policy(default_action=None))])) == 1

    def test_context_serving_other_deployments_does_not_cover_the_segment(self) -> None:
        """A dedicated context of someone else's deployment is no PBR for this segment."""
        errors = _run([_segment(deployments=["dep-a"])], [_context(tenant="dep-x", served=["dep-b"])])
        assert len(errors) == 1

    def test_each_unprotected_segment_is_reported(self) -> None:
        """One error per offending segment; protected neighbours stay clean."""
        errors = _run(
            [_segment("one"), _segment("two", policy=_policy()), _segment("three", namespace_type="non_prod")]
        )
        assert len(errors) == 2
