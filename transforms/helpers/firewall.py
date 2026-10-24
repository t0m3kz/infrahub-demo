"""Firewall zone and policy helpers for device transforms."""

import hashlib
from ipaddress import ip_interface, ip_network
from typing import Any

from transforms.helpers.acl import _PROTO_MAP, _port_match
from transforms.helpers.policy import active_rules, enabled_policies, rule_endpoint, rule_zone
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    INTERNET_TYPE,
    TRANSIT_SLOT,
    transit_addresses,
    transit_vlan,
    transit_vni,
)
from utils.limits import ACL_CLOSING_SEQ, VLAN_NAME_MAX
from utils.segment_addressing import segment_prefix


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
            prefix = segment_prefix(seg)
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
            "zone":        "internal",          # its zone
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


FIREWALL_CONTEXT_KIND = "ManagedFirewallContext"
LOADBALANCER_CONTEXT_KIND = "ManagedLoadbalancerContext"

SERVICE_CONTEXT_ROLES: dict[str, str] = {FIREWALL_CONTEXT_KIND: "firewall", LOADBALANCER_CONTEXT_KIND: "load-balancer"}
"""Context kind -> the role of the border service port it is tagged on, which is also the role of the HA
members holding its legs (generators/connections.py BORDER_ROLE_FOR_SERVICES)."""


def _transit_names(ctx: dict[str, Any], namespace: str) -> tuple[str, str]:
    """(full name, VLAN name) of a context leg's pseudo segment.

    A firewall leg is ``XCHG-<context>-<VRF>`` (both names). A load-balancer context has one leg and a
    shared context's name already ends in its namespace, so its leg is ``LB-<context>``; past
    VLAN_NAME_MAX the VLAN name is cut to fit with a 4-hex digest of the full name (deterministic, and
    two long names sharing a stem still differ), and the full name stays in the SVI description. A
    leaf's contexts come from the firewall HA traversal (FirewallContextLegFields, no typename): they
    are firewall contexts.
    """
    if (ctx.get("typename") or FIREWALL_CONTEXT_KIND) == FIREWALL_CONTEXT_KIND:
        name = f"XCHG-{ctx.get('name')}-{namespace}"
        return name, name
    name = f"LB-{ctx.get('name')}"
    if len(name) <= VLAN_NAME_MAX:
        return name, name
    digest = hashlib.sha1(name.encode(), usedforsecurity=False).hexdigest()[:4]
    return name, f"{name[: VLAN_NAME_MAX - len(digest) - 1]}-{digest}"


def _service_context_caps(interfaces: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The service-context capabilities on this device's service ports (SERVICE_CONTEXT_ROLES).

    On a border leaf these are the ports facing the HA members: a firewall-role
    port is tagged with the firewall contexts, a load-balancer-role port with the
    load-balancer contexts whose legs the leaf has to carry. De-duplicated by id.
    """
    found: dict[str, dict[str, Any]] = {}
    for iface in interfaces or []:
        for cap in iface.get("capabilities") or []:
            role = SERVICE_CONTEXT_ROLES.get(cap.get("typename") or "")
            if role is None or role != iface.get("role"):
                continue
            key = cap.get("id") or cap.get("name")
            if key:
                found.setdefault(key, cap)
    return list(found.values())


def transit_legs(ctx: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The context's legs in a non-default VRF namespace, keyed by namespace name.

    Each carries the namespace type/l3_vni, the transit /29, the transit VLAN and
    VNI, and the fixed addresses (anycast .1, VIP .4, members .5/.6), all by the
    utils.exchange_transit rules. A leg is a sub-interface of an HA member (a
    firewall or a load balancer); the border ports tagged with the context and
    legs in the default namespace are skipped.
    """
    member_roles = SERVICE_CONTEXT_ROLES.values()
    legs: dict[str, dict[str, Any]] = {}
    context_vlan = ctx.get("vlan_id")
    for leg in ctx.get("capabilities") or []:
        if (leg.get("device") or {}).get("role") not in member_roles:
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
                "vlan": (vlan := transit_vlan(context_vlan, ns_type)),
                "vni": transit_vni(vlan),
                "anycast": addresses["anycast"],
                "vip": addresses["vip"],
                "members": addresses["members"],
            },
        )
    return legs


def _context_leg_ip(ctx: dict[str, Any], namespace: str | None = None) -> str | None:
    """The PBR nexthop toward a context: the VIP of its leg in ``namespace``.

    A transit leg (non-default VRF namespace) is a /29 whose firewall VIP sits at a
    fixed offset (utils.exchange_transit); the members' own addresses are never the
    nexthop. None without a transit leg in ``namespace``.
    """
    for leg in ctx.get("capabilities") or []:
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
    return None


def get_exchange_transits(
    contexts: list[dict[str, Any]], *, legs: set[tuple[str, str]] | None = None
) -> list[dict[str, Any]]:
    """Pseudo segment activations for the transit legs of ``contexts``.

    Every leg of a context becomes a VLAN + L2 VNI + anycast gateway (.1 of the
    leg's /29) in the leg's VRF, so a VLAN sub-interface of the firewall or load
    balancer (border leaf, edge) or a PBR next hop (leaf) has something to attach
    to. Fed ONLY to get_vlans / get_vxlan_config / get_interfaces (never ACL, SGT
    or PBR input). ``transit_context`` names the context whose trunk ports must
    allow the VLAN. A firewall leg is ``XCHG-<context>-<VRF>``, the one leg of a
    load-balancer context ``LB-<context>`` (see _transit_names).

    ``legs`` restricts the result to the (namespace, VIP) pairs a leaf redirects
    to (the key of _context_leg_ip); None keeps every leg, as a border device does
    for the contexts tagged on its service ports.
    """
    transits: dict[int, dict[str, Any]] = {}
    for ctx in contexts:
        for leg in transit_legs(ctx).values():
            if legs is not None and (leg["namespace"], leg["vip"]) not in legs:
                continue
            name, vlan_name = _transit_names(ctx, leg["namespace"])
            transits.setdefault(
                leg["vlan"],
                {
                    "vlan_id": leg["vlan"],
                    "vni": leg["vni"],
                    "transit_context": ctx.get("name"),
                    "segment": {
                        "id": name,
                        "name": name,
                        "customer_name": vlan_name,
                        "vlan_description": name,
                        "gateway": {
                            "address": f"{leg['anycast']}/{leg['prefixlen']}",
                            "ip_prefix": {
                                "prefix": leg["prefix"],
                                "ip_namespace": {
                                    "name": leg["namespace"],
                                    "l3_vni": leg["l3_vni"],
                                    "namespace_type": leg["ns_type"],
                                },
                            },
                        },
                        "arp_suppression": True,
                        "terminate_inline": False,
                        "stretch_scope": "local",
                    },
                },
            )
    return [transits[vlan] for vlan in sorted(transits)]


def get_leaf_firewall_transits(
    pbr_rules: list[dict[str, Any]], firewall_contexts: list[dict[str, Any]] | None, *, device_name: str
) -> list[dict[str, Any]]:
    """The transit legs a leaf must hold locally for its firewall PBR next hops.

    A PBR next hop is a context's leg VIP in the segment's own VRF; the leaf bridges
    to it over the transit L2VNI, so the VIP has to be a connected next hop. Only
    the (context, VRF) legs a rule redirects to are rendered (get_exchange_transits).
    Raises when a next hop has no derivable leg (a missing context name, vlan_id or
    namespace l3_vni): the leaf would otherwise redirect into a VIP it cannot reach
    and the firewall would be bypassed silently.
    """
    wanted = {
        (rule["fw_namespace"], rule["fw_nexthop"])
        for rule in pbr_rules
        if rule.get("fw_nexthop") and rule.get("fw_namespace")
    }
    transits = get_exchange_transits(firewall_contexts or [], legs=wanted)
    held = {
        (ns["name"], transit_addresses(t["segment"]["gateway"]["ip_prefix"]["prefix"])["vip"])
        for t in transits
        for ns in [t["segment"]["gateway"]["ip_prefix"]["ip_namespace"]]
        if t["transit_context"] and ns["l3_vni"]
    }
    for rule in pbr_rules:
        nexthop = rule.get("fw_nexthop")
        if nexthop and (rule.get("fw_namespace"), nexthop) not in held:
            context = next(
                (
                    ctx.get("name")
                    for ctx in firewall_contexts or []
                    if _context_leg_ip(ctx, rule.get("fw_namespace")) == nexthop
                ),
                None,
            )
            raise ValueError(
                f"leaf '{device_name}' has no local transit leg for firewall next hop {nexthop} "
                f"(segment '{rule.get('segment_name')}', context '{context}', VRF '{rule.get('fw_namespace')}'): "
                "the context needs a name, a vlan_id and a namespace l3_vni; refusing config that would bypass the firewall"
            )
    return transits


def _snat_host(cap: dict[str, Any]) -> str | None:
    """The context's SNAT address without its mask (``snat_address`` is a /32), or None."""
    address = (cap.get("snat_address") or {}).get("address")
    return str(ip_interface(address).ip) if address else None


def get_exchange_routes(
    interfaces: list[dict[str, Any]] | None, *, export_tenant_default: bool = True
) -> list[dict[str, Any]]:
    """Static routes of the inter-VRF exchange through each firewall context on this device.

    Per exchange (tenant VRF A -> exchange peer Z) of a context tagged on a firewall port:

    - VRF A: ``0.0.0.0/0`` -> the context's A-leg VIP, only when the context is the
      shared one (no tenant). A dedicated context steers its tenant by leaf PBR.
    - VRF Z (INTERNET only): the context's SNAT address ``/32`` -> the context's
      Z-leg VIP, the return path of its translated flows. Another peer
      (MANAGEMENT) is source-NATed to its own leg's VIP, a /29 address the border
      leaves already connect, so it needs no route and no tenant prefix. INTERNET carries only public and
      transit space, so no tenant prefix goes in; a context without a SNAT address
      emits nothing there. No default route in INTERNET: its upstream is the
      edge's EVPN type-5 default.

    With ``export_tenant_default=False`` the tenant-VRF default route is marked
    ``local_only``: it stays a static inside the VRF but is not redistributed
    into EVPN (a colocation edge resolves the VIP over its own transit L2VNI;
    exporting would make a DC import another site's PROD default).

    A load-balancer context tagged on a load-balancer port adds its routed VIP
    range: ``vip_prefix`` -> the floating .4 of its one leg, in the leg's
    (tenant) VRF, always exported, so every VTEP of the VRF reaches the VIPs
    through this border. Raises when the context has legs in more than one VRF:
    the one-arm LB lives in one routing table.

    Returns [{vrf, l3_vni, prefix, nexthop}] sorted (+ ``local_only: True`` when not exported).
    """
    routes: dict[tuple[str, str, str], dict[str, Any]] = {}

    def _add(leg: dict[str, Any], prefix: str, nexthop: str, *, local_only: bool = False) -> None:
        routes[(leg["namespace"], prefix, nexthop)] = {
            "vrf": leg["namespace"],
            "l3_vni": leg["l3_vni"],
            "prefix": prefix,
            "nexthop": nexthop,
            **({"local_only": True} if local_only else {}),
        }

    for ctx in _service_context_caps(interfaces):
        legs = transit_legs(ctx)
        if ctx.get("typename") == LOADBALANCER_CONTEXT_KIND:
            vip_prefix = (ctx.get("vip_prefix") or {}).get("prefix")
            if not vip_prefix:
                continue
            if len(legs) > 1:
                raise ValueError(
                    f"load-balancer context '{ctx.get('name')}' has legs in {len(legs)} VRFs ({', '.join(sorted(legs))}): "
                    f"a one-arm context lives in one tenant VRF; refusing to route its VIP range {vip_prefix} into each"
                )
            for leg in legs.values():
                _add(leg, vip_prefix, leg["vip"])
            continue
        snat = _snat_host(ctx)
        # An exchange's sides are the namespaces of the legs carrying it: its own
        # namespace_a / namespace_z come back null through capabilities.
        exchanges: dict[str, dict[str, Any]] = {}
        sides: dict[str, set[str]] = {}
        for leg in ctx.get("capabilities") or []:
            leg_ns = ((leg.get("ip_address") or {}).get("ip_namespace") or {}).get("name")
            for exchange in leg.get("capabilities") or []:
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
                _add(leg_a, "0.0.0.0/0", leg_a["vip"], local_only=not export_tenant_default)
            if snat and leg_z["ns_type"] == INTERNET_TYPE:
                _add(leg_z, f"{snat}/32", leg_z["vip"])
    return sorted(routes.values(), key=lambda r: (r["vrf"], r["prefix"], r["nexthop"]))


def get_zone_policies(policies_data: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Build a rule table from SecurityPolicy dicts (place_policies_in_contexts).

    Disabled policies and disabled rules are skipped. Every template renders
    the policies as one flat rule table (a context's, or the root one), so
    rules are numbered 10, 20, ... across the whole table in policy order,
    then rule index order: per-policy indexes collide (each policy starts
    at the same index) and Check Point `position`
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
                    "src": segment_prefix(segment) if segment else None,
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
                "seq": max(seq + 10, ACL_CLOSING_SEQ),
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
