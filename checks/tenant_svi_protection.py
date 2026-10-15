"""Every routed tenant SVI is protected by an ACL or by PBR."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import is_routed_tenant_segment


class CheckTenantSviProtection(InfrahubCheck):
    """A routed tenant segment must not be an any-to-any SVI inside the shared VRF.

    All customers of an environment share the PROD or NON-PROD VRF, so isolation
    rests on the SVI ACL and on PBR to a firewall context. A routed segment (a
    gateway in a PROD or NON-PROD namespace, outside the exchange zones) is
    protected when it has:

    - an enabled security policy with rules or a default action (the leaf ACL);
    - or an inline firewall;
    - or a firewall context that PBR redirects it to: one serving one of its
      customer deployments, or a shared (tenant-less) one.
    """

    query = "tenant_svi_protection"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        contexts = cleaned.get("ManagedFirewallContext") or []
        served = {dep["id"] for ctx in contexts for dep in ctx.get("served_deployments") or []}
        tenants = {ctx["tenant"]["id"] for ctx in contexts if ctx.get("tenant")}
        has_shared_context = any(not ctx.get("tenant") for ctx in contexts)
        for segment in cleaned.get("ManagedNetworkSegment") or []:
            if not is_routed_tenant_segment(segment) or self._has_acl(segment) or segment.get("inline_service"):
                continue
            if has_shared_context or (self._deployment_ids(segment) & (served | tenants)):
                continue
            self.log_error(
                message=(
                    f"{segment.get('typename')} '{segment.get('name')}' is a routed tenant SVI in a shared "
                    "VRF with neither an ACL-bearing security policy, an inline firewall nor a PBR firewall "
                    "context: it is any-to-any inside the VRF"
                )
            )

    @staticmethod
    def _has_acl(segment: dict[str, Any]) -> bool:
        """An enabled policy with rules or a default action renders the SVI ACL."""
        policy = segment.get("security_policy") or {}
        return (
            bool(policy) and policy.get("enabled", True) and bool(policy.get("rules") or policy.get("default_action"))
        )

    @staticmethod
    def _deployment_ids(segment: dict[str, Any]) -> set[str]:
        """The customer deployments the segment serves (single on VLAN, many on VXLAN)."""
        single = segment.get("customer_deployment")
        many = segment.get("customer_deployments") or []
        return {dep["id"] for dep in [*many, single] if dep}
