"""Firewall zone and policy helpers for device transforms."""

from ipaddress import ip_interface, ip_network
from typing import Any

from transforms.helpers.segments import _get_segment_prefix_str


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


def get_vrf_default_gateways(
    activations: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """Placeholder — VRF default gateway nexthops are not yet derived from segment data.

    Returns an empty dict; leaf templates skip the default-route block when empty.
    """
    return {}


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
