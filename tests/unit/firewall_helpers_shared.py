"""Shared builders for the firewall helper unit tests (split from test_firewall_helpers.py)."""

from typing import Any

from tests.unit.encapsulation_helpers import cleaned_encapsulation
from transforms.helpers.firewall_contexts import get_firewall_contexts
from utils.exchange_transit import FIREWALL_CONTEXT_KIND, TRANSIT_SLOT, transit_vlan
from utils.interface_encapsulation import parent_of
from utils.routing_domain import interface_vrf


def fw_contexts(
    interfaces: list[dict[str, Any]] | None,
    ha: dict[str, Any] | None = None,
    cluster: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """``get_firewall_contexts`` of a firewall whose sub-interfaces are declared with the context they are a leg of."""
    contexts: dict[str, dict[str, Any]] = {c["id"]: c for c in cluster or []}
    trunks: set[str] = set()
    plain: list[dict[str, Any]] = []
    for iface in interfaces or []:
        context = next(
            (c for c in iface.get("capabilities") or [] if c.get("typename") == FIREWALL_CONTEXT_KIND and c.get("id")),
            None,
        )
        leg = {key: value for key, value in iface.items() if key != "capabilities"}
        if context is not None:
            contexts.setdefault(context["id"], context)
            trunk = (parent_of(iface) or {}).get("name") or "eth1"
            trunks.add(trunk)
            ns_type = (interface_vrf(iface).get("security_zone") or {}).get("vrf_type")
            if isinstance(context.get("vlan_id"), int):
                leg["name"] = (
                    f"{trunk}.{transit_vlan(context['vlan_id'], ns_type if ns_type in TRANSIT_SLOT else 'prod')}"
                )
            tag = (
                transit_vlan(context["vlan_id"], ns_type if ns_type in TRANSIT_SLOT else "prod")
                if isinstance(context.get("vlan_id"), int)
                else None
            )
            leg.update(cleaned_encapsulation(tag, parent_of(iface) or {"name": trunk, "device": {"id": "dev-fw-1"}}))
            leg.setdefault("device", {"id": "dev-fw-1"})
            leg.setdefault("typename", "DcimVirtualInterface")
            leg.setdefault("role", "service")
        plain.append(leg)
    uplinks = [
        {"typename": "DcimPhysicalInterface", "name": trunk, "role": "uplink", "device": {"id": "dev-fw-1"}}
        for trunk in sorted(trunks)
    ]
    device = {
        "id": "dev-fw-1",
        "name": "fw-1",
        "role": "internal-firewall",
        "shared_capabilities": [{"typename": FIREWALL_CONTEXT_KIND, **c} for c in contexts.values()],
        "interfaces": [*uplinks, *plain],
    }
    return get_firewall_contexts(device, ha)


def _make_segment_with_prefix(prefix: str) -> dict:
    """Build a minimal segment dict that segment_prefix() can parse."""
    return {"security_zone": {"name": "PROD", "vrf_type": "prod"}, "gateway": {"ip_prefix": {"prefix": prefix}}}


# get_customer_pbr_rules()


def _make_pbr_activation(
    *,
    vlan_id: int = 100,
    deployment_id: str | None = "dep-a",
    security_policies: list | None = None,
    environment: str | None = None,
) -> dict:
    """deployment_id models VxlanSegment.customer_deployments (TopologyCustomer ids)."""
    seg: dict = {
        "id": "seg-1",
        "name": "seg-1",
        "customer_name": "web",
        "security_zone": {"name": "PROD", "vrf_type": "prod"},
        "customer_deployments": [{"id": deployment_id}] if deployment_id else [],
    }
    if environment is not None:
        seg["environment"] = environment
    # The segment's own policy, as a 0/1-item list; None leaves it unqueried.
    if security_policies is not None:
        seg["security_policy"] = security_policies[0] if security_policies else None
    seg["gateway"] = {
        "ip_prefix": {
            "prefix": "10.0.1.0/24",
            "ip_namespace": {"node": {"name": {"value": "default"}}},
        }
    }
    return {"vlan_id": vlan_id, "segment": seg}
