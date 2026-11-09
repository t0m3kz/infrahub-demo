"""Every firewall context with a tenant-VRF leg is carried by a border or edge."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, TENANT_NAMESPACE_TYPES
from utils.fabric_roles import CARRIER_ROLES, INTERNAL_FIREWALL_ROLE
from utils.routing_domain import interface_security_zone
from utils.service_contexts import contexts_with_legs, port_contexts


class CheckPbrFirewallReachability(InfrahubCheck):
    """A PBR leaf bridges to a context's leg VIP; some border or edge must host it.

    A leaf redirects to the VIP of a context's leg in its own VRF and reaches it
    over the transit L2VNI. The VIP lives on the border-leaf or edge that carries
    the context: a firewall-role port tagged with it. A context with a PROD or
    NON-PROD leg that serves a deployment, and no such port, leaves that VNI
    empty and the redirected traffic blackholes. Contexts without a tenant-VRF
    leg or serving nothing are skipped.
    """

    query = "pbr_firewall_reachability"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        carried = self._carried_context_ids(cleaned.get("CarrierDevices") or [])
        legs = {
            ctx["id"]: ctx["legs"]
            for ctx in contexts_with_legs(cleaned.get("FirewallMembers") or [])
            if ctx.get("typename") == FIREWALL_CONTEXT_KIND
        }
        for ctx in cleaned.get("ManagedFirewallContext") or []:
            vrfs = self._tenant_vrfs(legs.get(ctx.get("id"), []))
            served = self._served(ctx)
            if not vrfs or not served or ctx.get("id") in carried:
                continue
            self.log_error(
                message=(
                    f"Firewall context '{ctx.get('name')}' has a leg in VRF {', '.join(vrfs)} serving "
                    f"{', '.join(served)}, but no border-leaf or edge firewall-role port carries it: "
                    "the leaf-local transit leg would bridge to a VIP no border hosts and PBR blackholes"
                )
            )

    @staticmethod
    def _carried_context_ids(devices: list[dict[str, Any]]) -> set[str]:
        """Ids of the contexts behind a firewall-role port of a border-leaf or edge (``port_contexts``)."""
        return {
            context_id
            for device in devices
            if device.get("role") in CARRIER_ROLES
            for contexts in port_contexts(device).values()
            for context_id, ctx in contexts.items()
            if ctx.get("typename") == FIREWALL_CONTEXT_KIND
        }

    @staticmethod
    def _tenant_vrfs(legs: list[dict[str, Any]]) -> list[str]:
        """Names of the PROD/NON-PROD VRFs the context has a firewall-member leg in."""
        return sorted(
            {
                interface_security_zone(leg)["name"]
                for leg in legs
                if (leg.get("device") or {}).get("role") == INTERNAL_FIREWALL_ROLE
                and interface_security_zone(leg).get("vrf_type") in TENANT_NAMESPACE_TYPES
            }
        )

    @staticmethod
    def _served(ctx: dict[str, Any]) -> list[str]:
        """Labels of the deployments the context serves: its served list plus a dedicated tenant."""
        deployments = [*(ctx.get("served_deployments") or []), ctx.get("tenant")]
        return sorted({dep.get("display_label") or dep["id"] for dep in deployments if dep})
