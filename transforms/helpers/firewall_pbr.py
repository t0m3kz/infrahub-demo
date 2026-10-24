"""Customer PBR rules: redirect inter-segment traffic to the serving firewall context, bypass on a permit.

Split out of ``transforms.helpers.firewall`` (the context leg lookup ``_context_leg_ip`` stays there, shared with the
exchange transits); the context list is built in ``transforms.helpers.firewall_contexts``.
"""

from typing import Any

from transforms.helpers.firewall import _context_leg_ip
from transforms.helpers.policy import rule_selector, segment_flows
from transforms.helpers.segments import _get_segment_namespace
from utils.exchange_transit import MANAGEMENT_TYPE, exchange_peer_zones
from utils.segment_addressing import segment_prefix


def _segment_deployment_ids(segment: dict[str, Any]) -> list[str]:
    """TopologyCustomer ids a segment serves: customer_deployment on a
    VlanSegment or CloudNetworkSegment, customer_deployments on a VxlanSegment
    (one relationship, split by cardinality)."""
    deployment_ids: list[str] = []
    single_deployment = segment.get("customer_deployment")
    if single_deployment and single_deployment.get("id"):
        deployment_ids.append(single_deployment["id"])
    for dep in segment.get("customer_deployments") or []:
        if dep.get("id"):
            deployment_ids.append(dep["id"])
    return deployment_ids


def _has_firewall_leg(ctx: dict[str, Any]) -> bool:
    """True when the context has an addressed leg on a firewall member (inline contexts have none)."""
    return any(
        (leg.get("device") or {}).get("role") == "firewall" and (leg.get("ip_address") or {}).get("address")
        for leg in ctx.get("capabilities") or []
    )


def _resolve_context_nexthops(
    firewall_contexts: list[dict[str, Any]] | None, namespace: str | None = None
) -> tuple[dict[str, str], str | None]:
    """Resolve each FirewallContext's nexthop in ``namespace`` (_context_leg_ip), keyed
    by dedicated tenant deployment id, plus the one shared (tenant-less) context's
    nexthop if any.
    """
    context_nexthop_by_deployment: dict[str, str] = {}
    shared_nexthop: str | None = None
    for ctx in firewall_contexts or []:
        fw_ip = _context_leg_ip(ctx, namespace)
        if fw_ip is None:
            continue
        tenant = ctx.get("tenant") or {}
        tenant_id = tenant.get("id")
        if tenant_id:
            context_nexthop_by_deployment[tenant_id] = fw_ip
        else:
            shared_nexthop = fw_ip
    return context_nexthop_by_deployment, shared_nexthop


def _redirect_nexthop(
    deployment_ids: list[str], nexthop_by_deployment: dict[str, str], shared_nexthop: str | None
) -> str | None:
    """Nexthop of the deployment's dedicated context, else the shared one."""
    return next((nexthop_by_deployment[d] for d in deployment_ids if d in nexthop_by_deployment), shared_nexthop)


def _serving_context_nexthops(
    firewall_contexts: list[dict[str, Any]] | None, namespace: str | None = None
) -> dict[str, str]:
    """Deployment id -> nexthop (in ``namespace``) of the context its segments terminate on.

    Strict: only a context that names the deployment (its dedicated tenant,
    or one of its served_deployments) counts. Unlike the redirect nexthop
    there is no shared fallback, since a deployment served by another DC's
    firewall would otherwise look like it shares this DC's shared context.
    """
    serving: dict[str, str] = {}
    for ctx in firewall_contexts or []:
        fw_ip = _context_leg_ip(ctx, namespace)
        if fw_ip is None:
            continue
        tenant_id = (ctx.get("tenant") or {}).get("id")
        for deployment_id in [tenant_id] + [d.get("id") for d in ctx.get("served_deployments") or []]:
            if deployment_id:
                serving.setdefault(deployment_id, fw_ip)
    return serving


def _needs_firewall(
    rule: dict[str, Any], own_nexthop: str, peer: dict[str, Any] | None, serving: dict[str, str]
) -> bool:
    """A permit is enforced on the firewall, not bypassed, when it carries a
    security profile (inspection) and its peer segment terminates on the
    same context as this one. Both directions then cross that one context,
    which holds the session. Across contexts no context sees both legs, so
    the flow stays on the fabric (bypass plus leaf ACL)."""
    if not rule.get("security_profile"):
        return False
    peer_nexthop = next((serving[d] for d in _segment_deployment_ids(peer or {}) if d in serving), None)
    return peer_nexthop == own_nexthop


def get_customer_pbr_rules(
    activations: list[dict[str, Any]] | None,
    firewall_contexts: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build PBR rules that redirect all traffic to the firewall by default.

    All inter-segment traffic defaults to the firewall (stateful inspection);
    a SecurityPolicyRule permit is the only bypass — the SAME rule data
    get_acls() already reads for its ACL rendering (segment_prefix
    per rule.destination_segment), just consumed here for a different
    purpose. The bypass is symmetric: a permit A -> B bypasses B on A's
    VLAN and, via B's inbound_rules, A on B's VLAN, so the reply never
    reaches a firewall that saw no forward packet. A permit to a pure prefix
    selector in the segment's own VRF bypasses that CIDR too; one in another
    VRF (or with no namespace) is redirected, and so is its reply.

    The one exception is an inspected flow: a permit with a security
    profile between two segments served by the SAME context is not
    bypassed on either side, so both directions cross that context
    (_needs_firewall). Between contexts every permit stays on the fabric. Not
    filtered by owner: a cross-owner permit bypasses PBR the same as a
    same-owner one, since intra- and inter-customer traffic use
    one unified default-redirect model.

    fw_nexthop resolution: `firewall_contexts` comes from
    _flatten_deployment_firewall_contexts(data["deployment"]) — a
    device-scoped traversal (this device's own deployment's firewall-role
    devices -> their ManagedFirewallHA cluster -> contexts), NOT a global
    query root and NOT scoped to the rendered device's own interfaces
    either. This matters because the anycast gateway/symmetric-IRB
    model puts every leaf, not just the border-leaf, in a position to make
    the PBR redirect decision, and no leaf ever owns a FirewallContext
    interface itself (only the firewall and, in pbr connectivity_mode, the
    border-leaf do). The nexthop is the context's /29 leg VIP inside the
    segment's own tenant VRF, a connected next hop: every leaf that redirects
    holds that transit leg locally (get_leaf_firewall_transits), bridges to the
    VIP over the transit L2VNI and never relies on a remote EVPN route to it
    (which would be routed in the L3VNI and bypass the firewall after the
    border decapsulates). ``fw_namespace`` names the leg used.
    inline connectivity_mode never allocates that IP (see
    generators/topology/customer_dc.py's _ensure_context_subinterface),
    so contexts without one are skipped — no PBR rule for them, since the
    firewall is already physically in the forwarding path.

    Dedicated-context matching is by DEPLOYMENT id, not OrganizationCustomer
    id: FirewallContext.tenant peers TopologyCustomer (the specific
    deployment footprint the dedicated_firewall flag was set on, e.g.
    C005-P-DC10), not the customer's org — segment.owner is an
    OrganizationCustomer and would never equal it. Segments carry their own
    TopologyCustomer link via customer_deployment (VlanSegment, cardinality
    one) / customer_deployments (VxlanSegment, cardinality many) — same
    identifier, both sides of one relationship split by cardinality.
    """
    if not activations:
        return []

    if not any(_has_firewall_leg(ctx) for ctx in firewall_contexts or []):
        return []

    # Nexthop maps per VRF namespace: a segment is redirected to the VIP of the
    # context's leg in the segment's own namespace.
    nexthops_by_namespace: dict[str | None, tuple[dict[str, str], str | None, dict[str, str]]] = {}

    def _nexthops(namespace: str | None) -> tuple[dict[str, str], str | None, dict[str, str]]:
        if namespace not in nexthops_by_namespace:
            by_deployment, shared = _resolve_context_nexthops(firewall_contexts, namespace)
            nexthops_by_namespace[namespace] = (
                by_deployment,
                shared,
                _serving_context_nexthops(firewall_contexts, namespace),
            )
        return nexthops_by_namespace[namespace]

    peer_zones = exchange_peer_zones()
    rules: list[dict[str, Any]] = []
    seen_vlans: set[int] = set()
    for act in activations:
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in seen_vlans:
            continue
        seg = act.get("segment") or {}
        if "security_policy" not in seg:
            continue
        seen_vlans.add(vlan_id)

        # A segment living in the MANAGEMENT VRF is a service network: tenants reach
        # it through their context, it is never itself redirected to one.
        if _get_segment_namespace(seg).get("namespace_type") == MANAGEMENT_TYPE:
            continue

        deployment_ids = _segment_deployment_ids(seg)

        context_nexthop_by_deployment, shared_nexthop, serving = _nexthops(_get_segment_namespace(seg).get("name"))
        fw_nexthop = _redirect_nexthop(deployment_ids, context_nexthop_by_deployment, shared_nexthop)
        if fw_nexthop is None:
            continue

        # The flow is inspected only if this segment's own context is the
        # one its traffic is redirected to.
        own_nexthop = next((serving[d] for d in deployment_ids if d in serving), None)
        inspected = own_nexthop is not None and own_nexthop == fw_nexthop

        bypass: set[str] = set()
        via_firewall: set[str] = set()

        own_namespace = _get_segment_namespace(seg).get("name")

        def _classify(rule: dict[str, Any], side: str) -> None:
            peer = rule.get(f"{side}_segment")
            prefix = segment_prefix(peer) if peer else None
            if not prefix:
                # A pure selector (cloud/partner CIDR): bypassed only when it lives in
                # this segment's own VRF, where the leaf can route it. Without a
                # namespace, or in another one, it stays on the firewall default.
                selector = rule_selector(rule, side)
                if not selector or selector[1] != own_namespace:
                    return
                prefix = selector[0]
            if inspected and _needs_firewall(rule, fw_nexthop, peer, serving):
                via_firewall.add(prefix)
            elif ((peer or {}).get("security_zone") or {}).get("name") not in peer_zones:
                # An exchange peer's zone (INTERNET, MANAGEMENT) is never bypassed:
                # the leaf routes a bypassed prefix in the tenant VRF, where it does not exist.
                bypass.add(prefix)

        egress, ingress = segment_flows(seg)
        for rule in egress["rules"]:
            if rule.get("action") == "permit":
                _classify(rule, "destination")
        # Reply leg of every permit INTO this segment: a forward packet that
        # bypassed the firewall on the source's leaf must be answered past it
        # too, or the firewall drops the reply with no session. The return
        # ACL (get_acls) still restricts it to the rule's port.
        for rule in ingress:
            _classify(rule, "source")

        # The bypass is per prefix: one inspected flow to a peer sends all
        # traffic to it through the firewall, the same on both leaves.
        bypass_prefixes = bypass - via_firewall

        rules.append(
            {
                "vlan_id": vlan_id,
                "segment_name": seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}",
                "bypass_prefixes": sorted(bypass_prefixes),
                "fw_nexthop": fw_nexthop,
                "fw_namespace": own_namespace,
                "customer_name": seg.get("customer_name"),
                "environment": seg.get("environment"),
            }
        )

    rules.sort(key=lambda r: r["vlan_id"])
    return rules
