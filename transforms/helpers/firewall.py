"""Firewall zone and policy helpers for device transforms."""

from ipaddress import ip_interface, ip_network
from typing import Any

from transforms.helpers.segments import _get_segment_prefix_str

# Border-leaf platforms with a native hardware SGT/security-group matching
# primitive — Cisco CTS ("match cts sgt") is proprietary VXLAN-GBP encoding,
# Arista MSS-G ("match security-group") is a separate, Arista-only
# implementation. SONiC/Nokia border-leaf have no equivalent, so they always
# fall back to prefix-based matching — the same fallback
# .dev/scenariusze.txt's own SONiC-BORDER-LEAF section uses (ip access-list
# ACL_KLIENT_A_INTER_CLIENT ... permit ip 10.10.1.0/24 ... instead of
# match cts sgt / match security-group).
#
# Known limitation: this only checks the BORDER-LEAF's own platform. A
# segment's VLAN can be provisioned on leaf devices of several platforms at
# once (DC-wide), and this query doesn't track which leaf platform(s) host
# any given segment — so a Cisco border-leaf tag-matches even if the
# specific leaf a packet actually came from was a non-tagging SONiC leaf for
# that VLAN. Precisely tracking per-leaf-platform tag fidelity would need
# additional data this project doesn't collect today; documented here
# rather than silently assumed.
_TAG_CAPABLE_BORDER_LEAF_PLATFORMS = frozenset({"cisco_nxos", "arista_eos"})


def get_firewall_zones(zones_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build a zone list from SecurityZone nodes (global query).

    Args:
        zones_data: List of cleaned SecurityZone dicts.

    Returns:
        List of zone dicts sorted by trust_level descending (most trusted first):
        [
          {
            "name": "internal",
            "trust_level": 100,
            "zone_type": "internal",
            "description": "...",
            "member_cidrs": ["10.0.1.0/24", "10.0.2.0/24"],
            "namespace_name": "VRF-INTERNAL",
            "namespace_l3vni": 10099,
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
                "zone_type": zone.get("zone_type") or "internal",
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

        # Derive the leaf nexthop: leaf is .2 in the /30, FW is .1.
        # More precisely: the OTHER host in the /30 (the leaf SVI).
        try:
            net = ip_network(ip_addr, strict=False)
            hosts = list(net.hosts())
            fw_ip = ip_interface(ip_addr).ip
            leaf_ip = next((h for h in hosts if h != fw_ip), None)
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
                }
            )

    return sorted(routes, key=lambda r: (r["vrf"], r["destination"]))


def _iface_ip_and_namespace(iface: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (ip_without_prefixlen, namespace_name) for one interface_capabilities leg."""
    ip_obj = iface.get("ip_address") or {}
    address = ip_obj.get("address")
    if not address:
        return None, None
    ns_name = (ip_obj.get("ip_namespace") or {}).get("name")
    return address.split("/")[0], ns_name


def get_vrf_default_gateways(
    interfaces: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """Build {vrf_name: nexthop_ip} from TopologyRoutedExchange capabilities on this device.

    RoutedExchange models one device (border-leaf or firewall) routing between two
    VRFs via two local SVIs/sub-interfaces — no fabric-wide route leak. Both legs
    are on THIS device, so both are already present in `interfaces`: the exchange
    capability's own `interface_capabilities` list (a reverse read of the same
    ManagedGenericInterfaces relation) returns both legs regardless of which one
    we started from. For each pair, the leg in VRF A becomes the nexthop for VRF
    Z's default route and vice versa — no second device/query needed.

    Args:
        interfaces: Device's own interfaces (raw `data["interfaces"]`, each with
                    interface_capabilities already populated by the query).

    Returns:
        {vrf_name: nexthop_ip}, one entry per VRF that has a routed exchange leg
        on this device. Empty if this device has no RoutedExchange capability.
    """
    if not interfaces:
        return {}

    gateways: dict[str, str] = {}
    seen_exchange_ids: set[str] = set()
    for iface in interfaces:
        for cap in iface.get("interface_capabilities") or []:
            if cap.get("typename") != "TopologyRoutedExchange":
                continue
            exchange_id = cap.get("id")
            if not exchange_id or exchange_id in seen_exchange_ids:
                continue
            seen_exchange_ids.add(exchange_id)

            legs = cap.get("interface_capabilities") or []
            by_namespace: dict[str, str] = {}
            for leg in legs:
                ip_addr, ns_name = _iface_ip_and_namespace(leg)
                if ip_addr and ns_name:
                    by_namespace[ns_name] = ip_addr

            for ns_name, ip_addr in by_namespace.items():
                for other_ns, other_ip in by_namespace.items():
                    if other_ns != ns_name:
                        gateways[ns_name] = other_ip

    return gateways


def get_firewall_contexts(interfaces: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Build the per-VDOM/vsys/context list from this firewall's own sub-interfaces.

    A ManagedFirewallContext shows up as an interface_capabilities entry on
    whichever DcimVirtualInterface generators/topology/customer_dc.py's
    _ensure_context_subinterface created for it (always on the cluster's
    "uplink"-role trunk — see that function's docstring). One context can
    only have one sub-interface per firewall device, so this is a plain
    one-pass collection, no cross-interface pairing needed (unlike
    get_vrf_default_gateways, which pairs two legs of the SAME exchange).
    """
    if not interfaces:
        return []

    contexts: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for iface in interfaces:
        ip_obj = iface.get("ip_address") or {}
        parent_iface = iface.get("parent_interface") or {}
        for cap in iface.get("interface_capabilities") or []:
            if cap.get("typename") != "ManagedFirewallContext":
                continue
            context_id = cap.get("id")
            if not context_id or context_id in seen_ids:
                continue
            seen_ids.add(context_id)
            tenant = cap.get("tenant") or {}
            contexts.append(
                {
                    "name": cap.get("name"),
                    "context_id": cap.get("context_id"),
                    "vlan_id": cap.get("vlan_id"),
                    "tenant_name": tenant.get("name"),
                    "sub_interface": iface.get("name"),
                    "parent_interface": parent_iface,
                    "ip_address": ip_obj.get("address"),
                }
            )
    contexts.sort(key=lambda c: c.get("name") or "")
    return contexts


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

    contexts = _contexts_from_device_hosting(deployment)
    parent = deployment.get("parent")
    if parent:
        contexts.extend(_contexts_from_device_hosting(parent))

    deduped: dict[str, dict[str, Any]] = {}
    for ctx in contexts:
        ctx_id = ctx.get("id")
        if ctx_id and ctx_id not in deduped:
            deduped[ctx_id] = ctx
    return list(deduped.values())


def _resolve_context_nexthops(
    firewall_contexts: list[dict[str, Any]] | None,
) -> tuple[dict[str, str], str | None]:
    """Shared by get_customer_pbr_rules and get_border_leaf_pbr_rules: resolve
    each FirewallContext's own firewall-leg IP, keyed by dedicated tenant
    deployment id, plus the one shared (tenant-less) context's nexthop if any.
    """
    context_nexthop_by_deployment: dict[str, str] = {}
    shared_nexthop: str | None = None
    for ctx in firewall_contexts or []:
        fw_ip: str | None = None
        for leg in ctx.get("interface_capabilities") or []:
            device = leg.get("device") or {}
            if device.get("role") != "firewall":
                continue
            ip_obj = leg.get("ip_address") or {}
            address = ip_obj.get("address")
            if not address:
                continue
            try:
                fw_ip = str(ip_interface(address).ip)
            except ValueError:
                continue
            break
        if fw_ip is None:
            continue
        tenant = ctx.get("tenant") or {}
        tenant_id = tenant.get("id")
        if tenant_id:
            context_nexthop_by_deployment[tenant_id] = fw_ip
        else:
            shared_nexthop = fw_ip
    return context_nexthop_by_deployment, shared_nexthop


def get_customer_pbr_rules(
    activations: list[dict[str, Any]] | None,
    firewall_contexts: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Build PBR rules that redirect all traffic to the firewall by default.

    All inter-segment traffic defaults to the firewall (stateful inspection);
    a SecurityPolicyRule permit is the only bypass — the SAME rule data
    get_acls() already reads for its ACL rendering (_get_segment_prefix_str
    per rule.destination_segment), just consumed here for a different
    purpose. Not filtered by owner: a cross-owner permit bypasses PBR the
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

    context_nexthop_by_deployment, shared_nexthop = _resolve_context_nexthops(firewall_contexts)
    if not context_nexthop_by_deployment and shared_nexthop is None:
        return []

    rules: list[dict[str, Any]] = []
    seen_vlans: set[int] = set()
    for act in activations:
        vlan_id = act.get("vlan_id")
        if not vlan_id or vlan_id in seen_vlans:
            continue
        seg = act.get("segment") or {}
        if "security_policies" not in seg:
            continue
        seen_vlans.add(vlan_id)

        deployment_ids: list[str] = []
        single_deployment = seg.get("customer_deployment")
        if single_deployment and single_deployment.get("id"):
            deployment_ids.append(single_deployment["id"])
        for dep in seg.get("customer_deployments") or []:
            if dep.get("id"):
                deployment_ids.append(dep["id"])

        fw_nexthop = next(
            (context_nexthop_by_deployment[d] for d in deployment_ids if d in context_nexthop_by_deployment),
            None,
        )
        if fw_nexthop is None:
            fw_nexthop = shared_nexthop
        if fw_nexthop is None:
            continue

        bypass_prefixes: list[str] = []
        for policy in seg.get("security_policies") or []:
            if not policy.get("enabled", True):
                continue
            for rule in policy.get("rules") or []:
                if rule.get("disabled") or rule.get("action") != "permit":
                    continue
                dst_seg = rule.get("destination_segment") or {}
                dst_prefix = _get_segment_prefix_str(dst_seg) if dst_seg else None
                if dst_prefix:
                    bypass_prefixes.append(dst_prefix)

        rules.append(
            {
                "vlan_id": vlan_id,
                "segment_name": seg.get("customer_name") or seg.get("name") or f"VLAN_{vlan_id}",
                "bypass_prefixes": sorted(set(bypass_prefixes)),
                "fw_nexthop": fw_nexthop,
                "customer_name": seg.get("customer_name"),
                "environment": seg.get("environment"),
            }
        )

    rules.sort(key=lambda r: r["vlan_id"])
    return rules


def get_border_leaf_pbr_rules(
    activations: list[dict[str, Any]] | None,
    firewall_contexts: list[dict[str, Any]] | None,
    border_leaf_platform: str,
) -> list[dict[str, Any]]:
    """Build border-leaf PBR rules per .dev/scenariusze.txt's border-leaf
    sections ("CTS PBR + SINGLE VRF"): default-redirect every segment's
    traffic to its firewall context's nexthop, matched by security tag when
    the border-leaf platform can hardware-match one (Cisco CTS "match cts
    sgt N" / Arista MSS-G "match security-group"), else falling back to a
    source-prefix ACL match — the same fallback scenariusze.txt's own
    SONiC-BORDER-LEAF section uses.

    Rules are grouped by (customer_name, fw_nexthop) to cut TCAM usage:
    prefix-fallback members of a group merge into ONE ACL with multiple
    permit lines and ONE route-map sequence; tag-match members each keep
    their own sequence (no confirmed multi-value "match cts sgt"/"match
    security-group" syntax to merge them), but sequences for the same
    customer are emitted together, adjacent, under one comment block.

    activations here come from _flatten_deployment_segment_activations(),
    NOT _collect_activations_from_interfaces() — border-leaf has no segment
    capability on its own interfaces (SGT/tag travels in-band inside the
    VXLAN header from the originating leaf), so it needs every activation in
    its own DC instead of a per-interface traversal. Dedup key is `vni` (or
    the segment's own id as fallback), NOT `vlan_id` — local VLAN ID is
    per-VLAN-domain now, not DC-wide-unique, so it can't identify a segment
    across leaves. Same fw_nexthop resolution as get_customer_pbr_rules
    (dedicated context by deployment id, else the one shared context).
    """
    if not activations:
        return []

    context_nexthop_by_deployment, shared_nexthop = _resolve_context_nexthops(firewall_contexts)
    if not context_nexthop_by_deployment and shared_nexthop is None:
        return []

    tag_capable = border_leaf_platform in _TAG_CAPABLE_BORDER_LEAF_PLATFORMS

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    seen_keys: set[str] = set()
    for act in activations:
        seg = act.get("segment") or {}
        dedup_key = act.get("vni") or seg.get("id")
        if not dedup_key or dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)

        deployment_ids: list[str] = []
        single_deployment = seg.get("customer_deployment")
        if single_deployment and single_deployment.get("id"):
            deployment_ids.append(single_deployment["id"])
        for dep in seg.get("customer_deployments") or []:
            if dep.get("id"):
                deployment_ids.append(dep["id"])

        fw_nexthop = next(
            (context_nexthop_by_deployment[d] for d in deployment_ids if d in context_nexthop_by_deployment),
            None,
        )
        if fw_nexthop is None:
            fw_nexthop = shared_nexthop
        if fw_nexthop is None:
            continue

        tag = seg.get("security_tag") or {}
        sgt = tag.get("group_id")
        sgt_name = tag.get("name")
        source_prefix = _get_segment_prefix_str(seg)
        match_by_tag = bool(tag_capable and sgt)
        if not match_by_tag and not source_prefix:
            # Neither a usable tag nor a resolvable source prefix — nothing
            # to match this segment's traffic by, skip rather than emit a
            # rule that can never hit.
            continue

        customer_name = seg.get("customer_name") or seg.get("name") or f"SEG_{dedup_key}"
        group_key = (customer_name, fw_nexthop)
        group = groups.setdefault(
            group_key,
            {
                "customer_name": customer_name,
                "environment": seg.get("environment"),
                "fw_nexthop": fw_nexthop,
                "acl_name": f"PBR-REDIRECT-{customer_name}",
                "source_prefixes": [],
                "tag_members": [],
            },
        )
        if match_by_tag:
            group["tag_members"].append({"sgt": sgt, "sgt_name": sgt_name})
        else:
            group["source_prefixes"].append(source_prefix)

    rules: list[dict[str, Any]] = []
    for group in sorted(groups.values(), key=lambda g: g["customer_name"]):
        common = {
            "customer_name": group["customer_name"],
            "environment": group["environment"],
            "fw_nexthop": group["fw_nexthop"],
        }
        if group["source_prefixes"]:
            rules.append(
                {
                    **common,
                    "match_by_tag": False,
                    "sgt": None,
                    "sgt_name": None,
                    "acl_name": group["acl_name"],
                    "source_prefixes": group["source_prefixes"],
                }
            )
        for member in sorted(group["tag_members"], key=lambda m: m["sgt"]):
            rules.append(
                {
                    **common,
                    "match_by_tag": True,
                    "sgt": member["sgt"],
                    # sgt_name is Arista's own match key (MSS-G "match security-group
                    # <name>" — .dev/scenariusze.txt's ARISTA-BORDER-LEAF section
                    # matches by group NAME, not the numeric id Cisco CTS uses).
                    "sgt_name": member["sgt_name"],
                    "acl_name": None,
                    "source_prefixes": [],
                }
            )

    return rules


def get_zone_policies(policies_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build a zone policy list from SecurityPolicy nodes (global query).

    Disabled policies and disabled rules are skipped. An implicit deny-all rule
    is appended as the last entry in every policy's rule list.

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
                "action": "permit", "protocol": "tcp",
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

    proto_map = {"any": "ip", "tcp": "tcp", "udp": "udp", "icmp": "icmp"}

    policies: list[dict[str, Any]] = []
    for policy in policies_data:
        if not policy.get("enabled", True):
            continue

        rules: list[dict[str, Any]] = []
        for rule in sorted(policy.get("rules") or [], key=lambda r: r.get("index") or 0):
            if rule.get("disabled"):
                continue

            protocol = rule.get("protocol") or "any"
            acl_proto = proto_map.get(protocol, "ip")

            src_zone = (rule.get("source_zone") or {}).get("name")
            dst_zone = (rule.get("destination_zone") or {}).get("name")

            src_seg = rule.get("source_segment") or {}
            src = _get_segment_prefix_str(src_seg) if src_seg else None
            dst_seg = rule.get("destination_segment") or {}
            dst = _get_segment_prefix_str(dst_seg) if dst_seg else None

            port_start = rule.get("port_start")
            port_end = rule.get("port_end")
            dst_port: str | None = None
            if port_start and acl_proto in ("tcp", "udp"):
                if port_end and port_end != port_start:
                    dst_port = f"range {port_start} {port_end}"
                else:
                    dst_port = f"eq {port_start}"

            profile = (rule.get("security_profile") or {}).get("name")

            rules.append(
                {
                    "seq": rule.get("index"),
                    "name": rule.get("name") or "",
                    "action": rule.get("action", "deny"),
                    "protocol": acl_proto,
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

        # Implicit deny-all (mirrors get_acls() behaviour)
        last_seq = max((r["seq"] or 0 for r in rules), default=0)
        implicit_seq = max(last_seq + 10, 9990)
        rules.append(
            {
                "seq": implicit_seq,
                "name": "implicit-deny-all",
                "action": "deny",
                "protocol": "ip",
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

        policies.append(
            {
                "name": policy.get("name") or "",
                "default_action": policy.get("default_action", "deny"),
                "rules": rules,
            }
        )
    return policies
