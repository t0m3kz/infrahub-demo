"""Every routed tenant SVI is protected by an ACL or by PBR."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import is_routed_tenant_segment


class CheckTenantSviProtection(InfrahubCheck):
    """A routed tenant segment needs an ACL-bearing policy or a PBR firewall context, else it is any-to-any in its VRF."""

    query = "tenant_svi_protection"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        contexts = cleaned.get("ManagedFirewallContext") or []
        served = {dep["id"] for ctx in contexts for dep in ctx.get("served_deployments") or []}
        tenants = {ctx["tenant"]["id"] for ctx in contexts if ctx.get("tenant")}
        has_shared_context = any(not ctx.get("tenant") for ctx in contexts)
        for segment in cleaned.get("ManagedNetworkSegment") or []:
            if not is_routed_tenant_segment(segment) or self._has_acl(segment):
                continue
            if has_shared_context or (self._deployment_ids(segment) & (served | tenants)):
                continue
            self.log_error(
                message=(
                    f"{segment.get('typename')} '{segment.get('name')}' is a routed tenant SVI in a shared "
                    "VRF with neither an ACL-bearing security policy nor a PBR firewall "
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
        """The customer deployments the segment serves."""
        return {dep["id"] for dep in segment.get("customer_deployments") or [] if dep}
