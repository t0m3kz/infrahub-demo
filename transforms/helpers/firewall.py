"""Firewall zone and policy helpers for device transforms."""

from ipaddress import ip_interface, ip_network
from typing import Any

from transforms.helpers.acl import _PROTO_MAP, _port_match
from transforms.helpers.ha import inline_addresses
from transforms.helpers.policy import (
    active_rules,
    enabled_policies,
    inbound_permits,
    rule_endpoint,
    rule_zone,
    segment_policies,
    segment_rule_policies,
)
from transforms.helpers.segments import (
    _get_segment_namespace,
    _get_segment_prefix_str,
    segment_hosting_candidates,
)
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    TRANSIT_SLOT,
    ZONE_BY_NS_TYPE,
    transit_addresses,
    transit_vlan,
    transit_vni,
)


def get_firewall_zones(zones_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build the zone list (address objects, static routes) from the SecurityZone root.

    Args:
        zones_data: List of cleaned SecurityZone dicts.

    Returns:
        List of zone dicts sorted by trust_level descending (most trusted first):
        [
          {
            "name": "internal",
            "trust_level": 100,
            "description": "...",
            "member_cidrs": ["10.0.1.0/24", "10.0.2.0/24"],
          }
        ]
    """
    if not zones_data:
        return []

    zones: list[dict[str, Any]] = []
    for zone in zones_data:
        name = zone.get("name")
        if not name:
            continue
        member_cidrs: list[str] = []
        for seg in zone.get("network_segments") or []:
            prefix = _get_segment_prefix_str(seg)
            if prefix:
                member_cidrs.append(prefix)
        zones.append(
            {
                "name": name,
                "trust_level": zone.get("trust_level") or 0,
                "description": zone.get("description") or "",
                "member_cidrs": sorted(member_cidrs),
            }
        )
    return sorted(zones, key=lambda z: z.get("trust_level") or 0, reverse=True)


def get_firewall_static_routes(
    fw_interfaces: list[dict[str, Any]],
    zones: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build static routes for the firewall — one default route per zone interface.

    Each FW sub-interface terminates in one zone/namespace. The return path to the
    leaf is via a static route: destination = zone member CIDRs, nexthop = leaf /30 IP
    (conventionally the .2 in the /30, i.e. network_address + 2).

    Args:
        fw_interfaces: List of DcimFirewallInterface dicts (already cleaned, with
                       security_zone.namespace and ip_address populated).
        zones:         Output of get_firewall_zones() — used to look up member_cidrs
                       per zone name.

    Returns:
        List of static route dicts:
        [
          {
            "vrf":         "VRF-INTERNAL",     # namespace name on the FW
            "destination": "10.99.0.0/24",     # zone member CIDR
            "nexthop":     "10.99.99.2",        # leaf /30 IP (.2 in the /30 link)
            "interface":   "eth0.99",           # FW sub-interface name
            "zone":        "internal",          # its zone (ASA routes by nameif)
          }
        ]
    """
    # Build zone-name → member_cidrs lookup from already-processed zones
    zone_cidrs: dict[str, list[str]] = {z["name"]: z["member_cidrs"] for z in zones}

    routes: list[dict[str, Any]] = []
    for iface in fw_interfaces:
        zone_ref = iface.get("security_zone") or {}
        zone_name = zone_ref.get("name")
        ip_obj = iface.get("ip_address") or {}
        ip_addr = ip_obj.get("address")
        ns_name = (ip_obj.get("ip_namespace") or {}).get("name")
        iface_name = iface.get("name")

        if not (zone_name and ns_name and ip_addr and iface_name):
            continue
        # A segment the firewall terminates inline is directly connected:
        # there is no leaf behind this interface to route the zone through.
        if iface.get("virtual_ip"):
            continue

        # Derive the leaf nexthop: leaf is .2 in the /30, FW is .1.
        # More precisely: the OTHER host in the /30 (the leaf SVI).
        try:
            net = ip_network(ip_addr, strict=False)
            fw_ip = ip_interface(ip_addr).ip
            # Lazy: an IPv6 /64 has 2^64 hosts — never materialize the list.
            leaf_ip = next((h for h in net.hosts() if h != fw_ip), None)
            if leaf_ip is None:
                continue
            nexthop = str(leaf_ip)
        except ValueError:
            continue

        for cidr in zone_cidrs.get(zone_name) or []:
            routes.append(
                {
                    "vrf": ns_name,
                    "destination": cidr,
                    "nexthop": nexthop,
                    "interface": iface_name,
                    "zone": zone_name,
                }
            )

    return sorted(routes, key=lambda r: (r["vrf"], r["destination"]))


def _firewall_context_caps(interfaces: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The ManagedFirewallContext capabilities on this device's firewall-role ports.

    On a border leaf these are the service ports facing the HA members; each is
    tagged with the contexts whose legs the leaf has to carry. De-duplicated by id.
    """
    found: dict[str, dict[str, Any]] = {}
    for iface in interfaces or []:
        if iface.get("role") != "firewall":
            continue
        for cap in iface.get("interface_capabilities") or []:
            if cap.get("typename") != "ManagedFirewallContext":
                continue
            key = cap.get("id") or cap.get("name")
            if key:
                found.setdefault(key, cap)
    return list(found.values())


def _transit_legs(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The context's legs in a non-default VRF namespace, keyed by namespace name.

    Each carries the namespace type/l3_vni, the transit /29 and the transit VLAN
    (utils.exchange_transit rules). Legacy legs in the default namespace (colocation)
    are skipped.
    """
    legs: dict[str, dict[str, Any]] = {}
    context_vlan = ctx.get("vlan_id")
    for leg in ctx.get("interface_capabilities") or []:
        if (leg.get("device") or {}).get("role") != "firewall":
            continue
        address = (leg.get("ip_address") or {}).get("address")
        namespace = (leg.get("ip_address") or {}).get("ip_namespace") or {}
        ns_name, ns_type = namespace.get("name"), namespace.get("namespace_type")
        if not address or not context_vlan or not ns_name or ns_name == "default" or ns_type not in TRANSIT_SLOT:
            continue
        network = ip_interface(address).network
        addresses = transit_addresses(str(network))
        legs.setdefault(
            ns_name,
            {
                "namespace": ns_name,
                "ns_type": ns_type,
                "l3_vni": namespace.get("l3_vni"),
                "prefix": str(network),
                "prefixlen": network.prefixlen,
                "vlan": transit_vlan(context_vlan, ns_type),
                "anycast": addresses["anycast"],
                "vip": addresses["vip"],
            },
        )
    return legs


def get_exchange_transits(interfaces: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Pseudo segment activations for the transit legs of this border leaf's contexts.

    Every leg of a context tagged on one of this device's firewall ports becomes a
    VLAN + L2 VNI + anycast gateway (.1 of the leg's /29) in the leg's VRF, so the
    firewall's VLAN sub-interface has something to attach to. Fed ONLY to
    get_vlans / get_vxlan_config / get_interfaces (never ACL, SGT or PBR input).
    ``transit_context`` names the context whose trunk ports must allow the VLAN.
    """
    transits: dict[int, dict[str, Any]] = {}
    for ctx in _firewall_context_caps(interfaces):
        for leg in _transit_legs(ctx).values():
            name = f"XCHG-{ctx.get('name')}-{leg['namespace']}"
            transits.setdefault(
                leg["vlan"],
                {
                    "vlan_id": leg["vlan"],
                    "vni": transit_vni(leg["vlan"]),
                    "transit_context": ctx.get("name"),
                    "segment": {
                        "id": name,
                        "name": name,
                        "customer_name": name,
                        "gateway": {
                            "address": f"{leg['anycast']}/{leg['prefixlen']}",
                            "ip_prefix": {
                                "prefix": leg["prefix"],
                                "ip_namespace": {"name": leg["namespace"], "l3_vni": leg["l3_vni"]},
                            },
                        },
                        "arp_suppression": True,
                        "terminate_inline": False,
                        "stretch_scope": "local",
                    },
                },
            )
    return [transits[vlan] for vlan in sorted(transits)]


def get_exchange_routes(
    interfaces: list[dict[str, Any]] | None,
    deployment_activations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Static routes of the inter-VRF exchange through each firewall context on this device.

    Per exchange (tenant VRF A -> INTERNET Z) of a context tagged on a firewall port:

    - VRF A: ``0.0.0.0/0`` -> the context's A-leg VIP, only when the context is the
      shared one (no tenant). A dedicated context steers its tenant by leaf PBR.
    - VRF Z (INTERNET): the gateway prefix of every segment in VRF A that the context
      serves (its tenant or served_deployments) -> the context's Z-leg VIP.
      No default route in INTERNET: its upstream is not modelled here.

    ``deployment_activations`` are the DC's segment activations
    (_flatten_deployment_segment_activations). Returns [{vrf, l3_vni, prefix, nexthop}] sorted.
    """
    routes: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _add(leg: dict[str, Any], prefix: str, nexthop: str) -> None:
        routes[(leg["namespace"], prefix, nexthop)] = {
            "vrf": leg["namespace"],
            "l3_vni": leg["l3_vni"],
            "prefix": prefix,
            "nexthop": nexthop,
        }

    for ctx in _firewall_context_caps(interfaces):
        legs = _transit_legs(ctx)
        deployments = {
            d
            for d in [(ctx.get("tenant") or {}).get("id")] + [x.get("id") for x in ctx.get("served_deployments") or []]
            if d
        }
        # An exchange's sides are the namespaces of the legs carrying it: its own
        # namespace_a / namespace_z come back null through interface_capabilities.
        exchanges: dict[str, dict[str, Any]] = {}
        sides: dict[str, set[str]] = {}
        for leg in ctx.get("interface_capabilities") or []:
            leg_ns = ((leg.get("ip_address") or {}).get("ip_namespace") or {}).get("name")
            for exchange in leg.get("interface_capabilities") or []:
                if exchange.get("id") and leg_ns in legs:
                    exchanges.setdefault(exchange["id"], exchange)
                    sides.setdefault(exchange["id"], set()).add(leg_ns)
        for exchange_id, exchange in exchanges.items():
            if len(sides[exchange_id]) != 2:
                continue
            # the tenant VRF is the side that has EXCHANGE_PEERS
            a_name, z_name = sorted(sides[exchange_id], key=lambda n: legs[n]["ns_type"] not in EXCHANGE_PEERS)
            leg_a, leg_z = legs[a_name], legs[z_name]
            if leg_z["ns_type"] not in EXCHANGE_PEERS.get(leg_a["ns_type"], ()):
                continue
            gateway = exchange.get("gateway") or {}
            if not (gateway.get("tenant") or ctx.get("tenant")):
                _add(leg_a, "0.0.0.0/0", leg_a["vip"])
            for act in deployment_activations or []:
                seg = act.get("segment") or {}
                prefix = _get_segment_prefix_str(seg)
                if (
                    prefix
                    and not seg.get("terminate_inline")
                    and _get_segment_namespace(seg).get("name") == a_name
                    and deployments.intersection(_segment_deployment_ids(seg))
                ):
                    _add(leg_z, prefix, leg_z["vip"])
    return sorted(routes.values(), key=lambda r: (r["vrf"], r["prefix"], r["nexthop"]))


def get_firewall_contexts(
    interfaces: list[dict[str, Any]] | None, ha: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Build the per-VDOM/vsys/context list from this firewall's own sub-interfaces.

    A ManagedFirewallContext shows up as an interface_capabilities entry on
    the DcimVirtualInterface(s) generators/topology/customer_dc.py created for
    it on the cluster's "uplink"-role trunk. The context-level fields
    (sub_interface, ip_address, ...) come from the first one; a context
    without namespaced legs (colocation, see below) has exactly one.

    ``legs`` are the context's exchange transit legs on this device: the
    sub-interfaces whose address is in a VRF namespace of a known type
    (utils/exchange_transit.py ZONE_BY_NS_TYPE), ordered prod, non_prod,
    internet. A leg is {interface, parent_interface, vlan (the transit VLAN),
    own_ip, virtual_ip / standby_ip (the HA pair's VIP and standby, None
    without ``ha``), anycast (the border leaves' gateway in the /29), vrf,
    ns_type, zone, nameif}. A context holding none keeps the legacy single
    P2P rendering.

    ``segments`` are the segments terminating on the context: every segment
    of a deployment it serves (served_deployments, written by each
    deployment's own boarding run), de-duplicated by id.
    """
    if not interfaces:
        return []

    contexts: dict[str, dict[str, Any]] = {}
    legs_by_context: dict[str, list[dict[str, Any]]] = {}
    for iface in interfaces:
        ip_obj = iface.get("ip_address") or {}
        parent_iface = iface.get("parent_interface") or {}
        for cap in iface.get("interface_capabilities") or []:
            if cap.get("typename") != "ManagedFirewallContext":
                continue
            context_id = cap.get("id")
            if not context_id:
                continue
            context = contexts.get(context_id)
            if context is None:
                segments: dict[str, dict[str, Any]] = {}
                for deployment in cap.get("served_deployments") or []:
                    for segment in deployment.get("network_segments") or []:
                        if segment.get("id"):
                            segments.setdefault(segment["id"], segment)
                tenant = cap.get("tenant") or {}
                context = contexts[context_id] = {
                    "id": context_id,
                    "name": cap.get("name"),
                    "context_id": cap.get("context_id"),
                    "vlan_id": cap.get("vlan_id"),
                    "tenant_name": tenant.get("name"),
                    "sub_interface": iface.get("name"),
                    "parent_interface": parent_iface,
                    "ip_address": ip_obj.get("address"),
                    "segments": list(segments.values()),
                    "legs": legs_by_context.setdefault(context_id, []),
                }
            namespace = ip_obj.get("ip_namespace") or {}
            ns_type = namespace.get("namespace_type")
            address = ip_obj.get("address")
            if ns_type not in ZONE_BY_NS_TYPE or not address or not cap.get("vlan_id"):
                continue
            network = ip_interface(address).network
            if network.version != 4:
                continue
            zone = ZONE_BY_NS_TYPE[ns_type]
            legs_by_context[context_id].append(
                {
                    "interface": iface.get("name"),
                    "parent_interface": parent_iface,
                    "vlan": transit_vlan(cap["vlan_id"], ns_type),
                    "own_ip": address,
                    **inline_addresses(None, ha, address),
                    "anycast": transit_addresses(str(network))["anycast"],
                    "vrf": namespace.get("name"),
                    "ns_type": ns_type,
                    "zone": zone,
                    "nameif": zone.removesuffix("-ZONE").lower(),
                }
            )
    for legs in legs_by_context.values():
        legs.sort(key=lambda leg: TRANSIT_SLOT[leg["ns_type"]])
    return sorted(contexts.values(), key=lambda c: c.get("name") or "")


def _zone_member_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every segment in a zone one of `segments` (those on this firewall's
    interfaces) belongs to: the firewall routes that zone's CIDRs through the
    leg (get_firewall_static_routes), so it is where their rules hold."""
    return [
        member for segment in segments for member in (segment.get("security_zone") or {}).get("network_segments") or []
    ]


def place_policies_in_contexts(
    contexts: list[dict[str, Any]],
    segments: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """This firewall's policies: the rules of the segments it serves, per table.

    Each context's table holds its segments' own policies (their egress
    rules) and the rules into them (the ingress leg: traffic between two
    tenants crosses both contexts and each one denies by default), see
    segment_rule_policies. The root table holds the rules of the segments
    the firewall carries on its interfaces (``segments``) that no context
    already holds; a firewall with no contexts also takes the rules of every
    segment in a zone bound on one of its interfaces. A rule of a segment
    served nowhere here is on the firewall serving it, not on this one.

    Returns (root policies, context id -> policies), each policy holding
    only the rules placed in that table.
    """
    by_context = {ctx["id"]: segment_rule_policies(ctx.get("segments") or []) for ctx in contexts}
    placed = {
        str(rule.get("id"))
        for policies in by_context.values()
        for policy in policies
        for rule in policy["rules"]
        if rule.get("id")
    }
    own = list(segments or [])
    if not contexts:
        own += _zone_member_segments(own)
    return segment_rule_policies(own, exclude=placed), by_context


def _flatten_deployment_firewall_contexts(deployment: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten the device-scoped FirewallContext traversal into the flat
    list get_customer_pbr_rules() expects.

    `deployment` is this device's own `deployment` field (already cleaned),
    shaped by queries/fragments/firewall_contexts.gql's
    FirewallContextsOnDeploymentFields fragment: deployment.devices
    (pre-filtered to role="firewall") -> each device's capabilities (keep
    typename == "ManagedFirewallHA") -> contexts -> [ManagedFirewallContext, ...].
    Pod-tier devices (leaf/tor/access-leaf) have their deployment set to the
    POD, not the DC — deployment_id=self.data["pod"]["id"] in
    generators/topology/rack.py vs deployment_id=dc_id in
    generators/topology/dc.py — so the fragment also nests one `parent` hop
    for that case; both paths are flattened here, deduped by context id.
    """
    if not deployment:
        return []

    def _contexts_from_device_hosting(hosting: dict[str, Any]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for device in hosting.get("devices") or []:
            for cap in device.get("capabilities") or []:
                if cap.get("typename") != "ManagedFirewallHA":
                    continue
                found.extend(cap.get("contexts") or [])
        return found

    contexts = [
        ctx for hosting in segment_hosting_candidates(deployment) for ctx in _contexts_from_device_hosting(hosting)
    ]

    deduped: dict[str, dict[str, Any]] = {}
    for ctx in contexts:
        ctx_id = ctx.get("id")
        if ctx_id and ctx_id not in deduped:
            deduped[ctx_id] = ctx
    return list(deduped.values())


def _segment_deployment_ids(segment: dict[str, Any]) -> list[str]:
    """TopologyCustomer ids a segment serves: customer_deployment on a
    VlanSegment, customer_deployments on a VxlanSegment (one relationship,
    split by cardinality)."""
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
        for leg in ctx.get("interface_capabilities") or []
    )


def _context_leg_ip(ctx: dict[str, Any], namespace: str | None = None) -> str | None:
    """The PBR nexthop toward a context: the VIP of its leg in ``namespace``.

    A transit leg (non-default VRF namespace) is a /29 whose firewall VIP sits at a
    fixed offset (utils.exchange_transit); the members' own addresses are never the
    nexthop. Without a matching transit leg (colocation's legacy default-namespace
    P2P, or no ``namespace``) the first firewall leg's own address is used.
    """
    legacy: str | None = None
    for leg in ctx.get("interface_capabilities") or []:
        if (leg.get("device") or {}).get("role") != "firewall":
            continue
        ip_obj = leg.get("ip_address") or {}
        address = ip_obj.get("address")
        if not address:
            continue
        try:
            interface = ip_interface(address)
        except ValueError:
            continue
        ns_name = (ip_obj.get("ip_namespace") or {}).get("name")
        if namespace and ns_name == namespace and ns_name != "default":
            return transit_addresses(str(interface.network))["vip"]
        if legacy is None and (not ns_name or ns_name == "default"):
            legacy = str(interface.ip)
    return legacy


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
    get_acls() already reads for its ACL rendering (_get_segment_prefix_str
    per rule.destination_segment), just consumed here for a different
    purpose. The bypass is symmetric: a permit A -> B bypasses B on A's
    VLAN and, via B's inbound_rules, A on B's VLAN, so the reply never
    reaches a firewall that saw no forward packet.

    The one exception is an inspected flow: a permit with a security
    profile between two segments served by the SAME context is not
    bypassed on either side, so both directions cross that context
    (_needs_firewall). Between contexts every permit stays on the fabric. Not filtered by owner: a cross-owner permit bypasses PBR the
    same as a same-owner one, since intra- and inter-customer traffic use
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
    border-leaf do). The nexthop is the FIREWALL's OWN leg IP on the /30 —
    reachable from any leaf via normal fabric underlay routing, since the
    border-leaf already redistributes that directly-connected subnet.
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

        def _classify(rule: dict[str, Any], peer: dict[str, Any] | None) -> None:
            prefix = _get_segment_prefix_str(peer) if peer else None
            if not prefix:
                return
            if inspected and _needs_firewall(rule, fw_nexthop, peer, serving):
                via_firewall.add(prefix)
            else:
                bypass.add(prefix)

        for policy in enabled_policies(segment_policies(seg)):
            for rule in active_rules(policy):
                if rule.get("action") == "permit":
                    _classify(rule, rule.get("destination_segment"))
        # Reply leg of every permit INTO this segment: a forward packet that
        # bypassed the firewall on the source's leaf must be answered past it
        # too, or the firewall drops the reply with no session. The return
        # ACL (get_acls) still restricts it to the rule's port.
        for rule in inbound_permits(seg):
            _classify(rule, rule.get("source_segment"))

        # The bypass is per prefix: one inspected flow to a peer sends all
        # traffic to it through the firewall, the same on both leaves.
        bypass_prefixes = bypass - via_firewall

        rules.append(
            {
                "vlan_id": vlan_id,
                "segment_name": seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}",
                "bypass_prefixes": sorted(bypass_prefixes),
                "fw_nexthop": fw_nexthop,
                "customer_name": seg.get("customer_name"),
                "environment": seg.get("environment"),
            }
        )

    rules.sort(key=lambda r: r["vlan_id"])
    return rules


def get_zone_policies(policies_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build a rule table from SecurityPolicy dicts (place_policies_in_contexts).

    Disabled policies and disabled rules are skipped. Every template renders
    the policies as one flat rule table (a context's, or the root one), so
    rules are numbered 10, 20, ... across the whole table in policy order,
    then rule index order: per-policy indexes collide (each policy starts
    at the same index) and FortiOS `edit <seq>` / Check Point `position`
    would overwrite or reorder them. One implicit deny-all closes the table,
    on the last policy; one per policy would shadow every later policy.

    Args:
        policies_data: List of cleaned SecurityPolicy dicts.

    Returns:
        List of policy dicts:
        [
          {
            "name": "east-west",
            "default_action": "deny",
            "rules": [
              {
                "seq": 10, "name": "allow-https",
                "action": "permit", "protocol": "tcp", "raw_protocol": "tcp",
                "port_start": 443, "port_end": None,
                "src_zone": "dmz", "dst_zone": "internal",
                "src": None, "dst": None,
                "dst_port": "eq 443", "log": True,
                "description": "", "security_profile": "strict-av",
              }
            ],
          }
        ]
    """
    if not policies_data:
        return []

    policies: list[dict[str, Any]] = []
    seq = 0
    for policy in enabled_policies(policies_data):
        rules: list[dict[str, Any]] = []
        for rule in active_rules(policy):
            seq += 10

            protocol = rule.get("protocol") or "any"
            acl_proto = _PROTO_MAP.get(protocol, "ip")

            src_zone = rule_zone(rule, "source")
            dst_zone = rule_zone(rule, "destination")

            src = rule_endpoint(rule, "source")
            dst = rule_endpoint(rule, "destination")

            port_start = rule.get("port_start")
            port_end = rule.get("port_end")
            dst_port = _port_match(rule, acl_proto)

            profile = (rule.get("security_profile") or {}).get("name")

            rules.append(
                {
                    "seq": seq,
                    "name": rule.get("name") or "",
                    "action": rule.get("action", "deny"),
                    "protocol": acl_proto,
                    # Unmapped protocol ("tcp"/"udp"/"icmp"/"any") plus raw
                    # numeric ports, for templates that build their own
                    # service/application object (PAN-OS, Junos) instead of
                    # consuming the pre-formatted Cisco-ACL-style dst_port
                    # string above ("eq 443" / "range 8080 8090").
                    "raw_protocol": protocol,
                    "port_start": port_start,
                    "port_end": port_end,
                    "src_zone": src_zone,
                    "dst_zone": dst_zone,
                    "src": src,
                    "dst": dst,
                    "dst_port": dst_port,
                    "log": bool(rule.get("log")),
                    "description": rule.get("description") or "",
                    "security_profile": profile,
                }
            )

        if policy.get("default_action") == "permit":
            # Default permit closes the policy's own segment, not the table:
            # unmatched traffic FROM that segment passes, everything else still
            # falls through to the implicit deny below.
            seq += 10
            segment = policy.get("segment") or {}
            rules.append(
                {
                    "seq": seq,
                    "name": f"{policy.get('name') or 'policy'}-default-permit",
                    "action": "permit",
                    "protocol": "ip",
                    "raw_protocol": "any",
                    "port_start": None,
                    "port_end": None,
                    "src_zone": (segment.get("security_zone") or {}).get("name") or None,
                    "dst_zone": None,
                    "src": _get_segment_prefix_str(segment) if segment else None,
                    "dst": None,
                    "dst_port": None,
                    "log": False,
                    "description": f"Default action of {policy.get('name') or 'policy'}",
                    "security_profile": None,
                }
            )

        policies.append(
            {
                "name": policy.get("name") or "",
                "default_action": policy.get("default_action", "deny"),
                "rules": rules,
            }
        )

    if policies:
        # Implicit deny-all (mirrors get_acls() behaviour)
        policies[-1]["rules"].append(
            {
                "seq": max(seq + 10, 9990),
                "name": "implicit-deny-all",
                "action": "deny",
                "protocol": "ip",
                "raw_protocol": "any",
                "port_start": None,
                "port_end": None,
                "src_zone": None,
                "dst_zone": None,
                "src": None,
                "dst": None,
                "dst_port": None,
                "log": True,
                "description": "Implicit deny all",
                "security_profile": None,
            }
        )
    return policies
