"""Shared builders for the firewall helper unit tests (split from test_firewall_helpers.py)."""


def _make_segment_with_prefix(prefix: str) -> dict:
    """Build a minimal segment dict that segment_prefix() can parse."""
    return {"gateway": {"ip_prefix": {"prefix": prefix}}}


# ===========================================================================
# get_customer_pbr_rules()
# ===========================================================================


def _make_pbr_activation(
    *,
    vlan_id: int = 100,
    deployment_id: str | None = "dep-a",
    security_policies: list | None = None,
    environment: str | None = None,
) -> dict:
    """deployment_id models VxlanSegment.customer_deployments (TopologyCustomer
    ids) — the field FirewallContext.tenant is actually matched against, NOT
    segment.owner (an OrganizationCustomer, a different kind with different
    ids that never equals FirewallContext.tenant.id)."""
    seg: dict = {
        "id": "seg-1",
        "name": "seg-1",
        "customer_name": "web",
        "customer_deployments": [{"id": deployment_id}] if deployment_id else [],
    }
    if environment is not None:
        seg["environment"] = environment
    # The segment's own policy, as a 0/1-item list; None leaves it unqueried.
    if security_policies is not None:
        seg["security_policy"] = security_policies[0] if security_policies else None
    seg["gateway"] = {"ip_prefix": {"prefix": "10.0.1.0/24", "ip_namespace": {"name": "PROD"}}}
    return {"vlan_id": vlan_id, "segment": seg}
