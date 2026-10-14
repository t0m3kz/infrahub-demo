"""Every firewall context with a tenant-VRF leg is carried by a border or edge."""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data

TENANT_NAMESPACE_TYPES = frozenset({"prod", "non_prod"})
# Devices that terminate the firewall transit legs (get_exchange_transits). A
# collapsed border-spine carries them like a border-leaf.
CARRIER_ROLES = frozenset({"border-leaf", "border-spine", "edge"})
PORT_KEYS = ("FirewallPhysicalPorts", "FirewallVirtualPorts")


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
        carried = self._carried_context_ids([port for key in PORT_KEYS for port in cleaned.get(key) or []])
        for ctx in cleaned.get("ManagedFirewallContext") or []:
            vrfs = self._tenant_vrfs(ctx)
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
    def _carried_context_ids(ports: list[dict[str, Any]]) -> set[str]:
        """Ids of the contexts tagged on a firewall-role port of a border-leaf or edge."""
        return {
            cap["id"]
            for port in ports
            if ((port.get("device") or {}).get("role")) in CARRIER_ROLES
            for cap in port.get("interface_capabilities") or []
            if cap.get("typename") == "ManagedFirewallContext" and cap.get("id")
        }

    @staticmethod
    def _tenant_vrfs(ctx: dict[str, Any]) -> list[str]:
        """Names of the PROD/NON-PROD VRFs the context has a firewall-member leg in."""
        return sorted(
            {
                leg["ip_address"]["ip_namespace"]["name"]
                for leg in ctx.get("interface_capabilities") or []
                if (leg.get("device") or {}).get("role") == "firewall"
                and ((leg.get("ip_address") or {}).get("ip_namespace") or {}).get("namespace_type")
                in TENANT_NAMESPACE_TYPES
            }
        )

    @staticmethod
    def _served(ctx: dict[str, Any]) -> list[str]:
        """Labels of the deployments the context serves: its served list plus a dedicated tenant."""
        deployments = [*(ctx.get("served_deployments") or []), ctx.get("tenant")]
        return sorted({dep.get("display_label") or dep["id"] for dep in deployments if dep})
