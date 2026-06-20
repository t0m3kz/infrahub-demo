"""BGP configuration helpers for device transforms."""

from ipaddress import ip_address
from typing import Any


def _sort_key_ip(ip_obj: Any) -> tuple:
    """Return a sort key for an IP address object (dict or string).

    Parses the address into a numeric tuple for proper ordering
    (e.g., 10.0.0.2 before 10.0.0.10). Falls back to string comparison.
    """
    addr_str = ""
    if isinstance(ip_obj, dict):
        addr_str = ip_obj.get("address", "")
    elif isinstance(ip_obj, str):
        addr_str = ip_obj

    # Strip prefix length if present (e.g., "10.0.0.1/31" → "10.0.0.1")
    addr_str = addr_str.split("/")[0] if addr_str else ""

    try:
        return (0, ip_address(addr_str).packed)
    except (ValueError, AttributeError):
        return (1, addr_str.encode())


def _normalize_afs(afs: list[dict[str, Any]]) -> list[str]:
    """Convert RoutingBGPAddressFamily objects to simple label strings for templates.

    Templates check membership like ``'evpn' in session.address_families``, so we
    reduce each AFI/SAFI pair to a single string:
      l2vpn / evpn  → "evpn"
      ipv4  / *     → "ipv4"
      ipv6  / *     → "ipv6"
      vpnv4 / *     → "vpnv4"
    For distinctive SAFIs (evpn, vpn, flowspec, labeled_unicast) the SAFI wins;
    otherwise the AFI is used.
    """
    distinctive_safis = {"evpn", "vpn", "flowspec", "labeled_unicast", "multicast"}
    return [
        safi if safi in distinctive_safis else (af.get("afi") or "") for af in afs for safi in [af.get("safi") or ""]
    ]


def _extract_remote_asn_from_peering(peering_node: dict, remote_device_name: str) -> int | None:
    """Extract remote device's ASN from the peering's bgp_processes.

    Each peering has 2 bgp_processes (local + remote). Find the one
    belonging to the remote device and return its ASN.
    """
    bgp_procs = peering_node.get("bgp_processes", [])
    if not isinstance(bgp_procs, list):
        return None

    for proc in bgp_procs:
        dev_name = (proc.get("device") or {}).get("name", "")
        if dev_name == remote_device_name:
            local_as = proc.get("local_as")
            if isinstance(local_as, dict):
                return local_as.get("asn")
    return None


def _build_session_from_peering(
    peering_node: dict[str, Any],
    device_name: str,
    local_as: dict | None,
    interfaces: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Build a BGP session dict from a peering node using interfaces.

    Determines local vs remote by matching device name in interfaces.
    Returns None if the peering cannot be processed.
    """
    # Get interfaces (2 entries: local + remote)
    peering_ifaces = peering_node.get("interfaces", [])
    if not isinstance(peering_ifaces, list) or len(peering_ifaces) != 2:
        return None

    # Determine local vs remote interface by device name
    local_iface = None
    remote_iface = None
    for iface in peering_ifaces:
        iface_device = iface.get("device", {}).get("name", "")
        if iface_device == device_name:
            local_iface = iface
        else:
            remote_iface = iface

    if not local_iface or not remote_iface:
        return None

    session: dict[str, Any] = {
        "name": peering_node.get("name"),
        "session_type": peering_node.get("session_type"),
        "bfd_enabled": peering_node.get("bfd_enabled"),
        "send_community": peering_node.get("send_community"),
        "ttl": peering_node.get("ttl"),
        "route_reflector_client": peering_node.get("route_reflector_client", False),
        "enabled": True,
    }

    ttl = peering_node.get("ttl", 255)
    remote_device_name = remote_iface.get("device", {}).get("name", "")

    if ttl == 1 and interfaces:
        # Underlay (TTL=1): prefer IPs from cable endpoints for exact interface match
        local_interface_ip = None
        remote_interface_ip = None
        local_iface_name = None

        for iface in interfaces:
            if not iface.get("cable"):
                continue
            cable = iface.get("cable", {})
            for endpoint in cable.get("endpoints", []):
                if endpoint.get("device", {}).get("name") == remote_device_name:
                    local_interface_ip = iface.get("ip_address")
                    remote_interface_ip = endpoint.get("ip_address")
                    local_iface_name = iface.get("name")
                    break
            if local_iface_name:
                break

        if not local_iface_name:
            # Inter-site circuit: circuit appears in interface_capabilities (ManagedGeneric pattern)
            for iface in interfaces:
                for svc in iface.get("interface_capabilities") or []:
                    typename = svc.get("__typename", "")
                    if typename not in ("TopologyPhysicalCircuit", "TopologyVirtualCircuit"):
                        continue
                    # circuits use cardinality-many `interfaces` list (2 entries: local + remote)
                    for other_iface in svc.get("interfaces") or []:
                        if (other_iface.get("device") or {}).get("name") == remote_device_name:
                            local_interface_ip = iface.get("ip_address")
                            remote_interface_ip = other_iface.get("ip_address")
                            local_iface_name = iface.get("name")
                            break
                    if local_iface_name:
                        break
                if local_iface_name:
                    break

        if not local_interface_ip and not local_iface_name:
            return None  # No cable or circuit connects this device to the remote — skip session

        if local_interface_ip:
            session["local_ip"] = local_interface_ip
        elif local_iface_name:
            session["interface_name"] = local_iface_name

        if remote_interface_ip:
            session["remote_ip"] = remote_interface_ip
    else:
        # Overlay (TTL!=1): use peering_interfaces IPs (loopbacks)
        local_ip = local_iface.get("ip_address")
        if local_ip:
            session["local_ip"] = local_ip
        remote_ip = remote_iface.get("ip_address")
        if remote_ip:
            session["remote_ip"] = remote_ip

    # Remote ASN resolution
    session_type = str(peering_node.get("session_type", "")).upper()
    if session_type == "IBGP" and local_as:
        session["remote_as"] = local_as
    else:
        remote_asn = _extract_remote_asn_from_peering(peering_node, remote_device_name)
        if remote_asn:
            session["remote_as"] = {"asn": remote_asn}

    # Remote device name
    if remote_device_name:
        session["remote_device"] = remote_device_name

    # eBGP sessions require remote_as to be useful — skip if not resolved
    if session_type in ("EBGP", "EBGP_MULTIHOP", "EBGP_UNNUMBERED") and "remote_as" not in session:
        return None

    # Address families: use explicit schema config if set, otherwise derive from TTL.
    # Overlay (TTL != 1) → EVPN; underlay (TTL == 1) → IPv4 (empty = template default).
    schema_afs = peering_node.get("address_families") or []
    if schema_afs:
        session["address_families"] = _normalize_afs(schema_afs)
    elif ttl != 1:
        session["address_families"] = ["evpn"]
    else:
        session["address_families"] = []

    return session


def _build_peer_groups(sessions: list[dict[str, Any]], device_role: str = "") -> list[dict[str, Any]]:
    """Assign sessions to peer groups and return group definitions.

    Always creates a peer group when there is at least one session of the type:
    - UNDERLAY-PEERS: eBGP sessions with TTL=1 (P2P underlay), per-neighbor remote-as
    - EVPN-PEERS: iBGP sessions with TTL!=1 (EVPN overlay), shared remote-as from peer group
    - EVPN-OVERLAY: eBGP sessions with TTL!=1 (eBGP EVPN overlay), per-neighbor remote-as

    Mutates sessions in-place by adding 'peer_group' (and 'remote_as_from_peer_group' for iBGP)
    keys. Returns list of peer group definitions.
    """
    underlay = [s for s in sessions if s.get("ttl") == 1]
    overlay_ibgp = [s for s in sessions if s.get("ttl") != 1 and str(s.get("session_type", "")).upper() == "IBGP"]
    overlay_ebgp = [
        s for s in sessions if s.get("ttl") != 1 and str(s.get("session_type", "")).upper() in ("EBGP", "EBGP_MULTIHOP")
    ]

    peer_groups: list[dict[str, Any]] = []

    if underlay:
        pg_name = "UNDERLAY-PEERS"
        peer_groups.append(
            {
                "name": pg_name,
                "type": "underlay",
                "session_type": "EBGP",
                "bfd_enabled": any(bool(s.get("bfd_enabled")) for s in underlay),
                "send_community_extended": True,
                "address_families": ["ipv4"],
            }
        )
        for session in underlay:
            session["peer_group"] = pg_name

    if overlay_ibgp:
        pg_name = "EVPN-PEERS"
        remote_as = None
        for s in overlay_ibgp:
            ra = s.get("remote_as")
            if isinstance(ra, dict) and ra.get("asn"):
                remote_as = ra["asn"]
                break
        _RR_ROLES = ("spine", "super-spine", "super_spine")
        has_rr_flag = any(bool(s.get("route_reflector_client")) for s in overlay_ibgp)
        # Spines/super-spines are always RRs when peerings have the RR flag.
        # Leafs become intermediate RRs in middle_rack/mixed deployments
        # when they have overlay sessions with tors (2-level RR hierarchy).
        is_spine_rr = has_rr_flag and device_role in _RR_ROLES
        is_leaf_rr = (
            has_rr_flag
            and device_role in ("leaf", "border-leaf")
            and any("tor" in (s.get("remote_device") or "") for s in overlay_ibgp)
        )
        rr_client = is_spine_rr or is_leaf_rr
        peer_groups.append(
            {
                "name": pg_name,
                "type": "overlay",
                "session_type": "IBGP",
                "remote_as": remote_as,
                "send_community_extended": True,
                "route_reflector_client": rr_client,
                "address_families": ["evpn"],
            }
        )
        for session in overlay_ibgp:
            session["peer_group"] = pg_name
            session["remote_as_from_peer_group"] = True

    if overlay_ebgp:
        pg_name = "EVPN-OVERLAY"
        peer_groups.append(
            {
                "name": pg_name,
                "type": "overlay",
                "session_type": "EBGP",
                "send_community_extended": True,
                "ebgp_multihop": 255,
                "address_families": ["evpn"],
            }
        )
        for session in overlay_ebgp:
            session["peer_group"] = pg_name

    peer_groups.sort(key=lambda pg: pg.get("name", ""))
    return peer_groups


def get_bgp_profile(
    device_capabilities: list[dict[str, Any]],
    interfaces: list[dict[str, Any]] | None = None,
    device_name: str = "",
    device_role: str = "",
) -> list[dict[str, Any]]:
    """
    Extract BGP configuration from ManagedBGP services with peerings.

    Uses interfaces to determine local vs remote peer (by device name)
    and to get peer IP addresses.

    For underlay peerings (TTL=1), uses physical interface IPs from interfaces.
    For overlay peerings (TTL!=1), uses loopback IPs from interfaces.

    Remote ASN for eBGP: resolved from bgp_processes in the peering data.
    Remote ASN for iBGP: equals local_as (same AS by definition).
    """
    if not device_capabilities:
        return []

    bgp_configs = []

    for service in device_capabilities:
        if service.get("typename") != "ManagedBGP":
            continue

        bgp_config = {
            "name": service.get("name"),
            "status": service.get("status"),
            "multipath": service.get("multipath"),
            "graceful_restart": service.get("graceful_restart"),
            "confederation_identifier": service.get("confederation_identifier"),
        }

        local_as = service.get("local_as")
        if local_as:
            bgp_config["local_as"] = local_as

        router_id = service.get("router_id")
        if router_id:
            bgp_config["router_id"] = router_id

        sessions = []
        peerings = service.get("peerings", [])

        if isinstance(peerings, list):
            for peering_node in peerings:
                session = _build_session_from_peering(peering_node, device_name, local_as, interfaces)
                if session:
                    sessions.append(session)

        bgp_config["sessions"] = sessions
        bgp_configs.append(bgp_config)

    # Merge BGP processes that share the same local ASN into a single config block.
    # This handles eBGP-eBGP where underlay and overlay processes reuse the same per-device ASN.
    by_asn: dict[int, dict] = {}
    ungrouped: list[dict] = []
    for bgp_config in bgp_configs:
        local_as = bgp_config.get("local_as") or {}
        asn = local_as.get("asn") if isinstance(local_as, dict) else None
        if asn is None:
            ungrouped.append(bgp_config)
            continue
        if asn not in by_asn:
            by_asn[asn] = bgp_config
        else:
            existing = by_asn[asn]
            existing["sessions"].extend(bgp_config.get("sessions", []))
            if not existing.get("router_id") and bgp_config.get("router_id"):
                existing["router_id"] = bgp_config["router_id"]

    merged = list(by_asn.values()) + ungrouped

    # Sort BGP configs by local ASN for deterministic output
    merged.sort(key=lambda c: (c.get("local_as") or {}).get("asn") or 0)

    # Assign peer groups to sessions with common attributes
    for bgp_config in merged:
        # Sort sessions (neighbors) by (ttl group, IP address) for deterministic config output
        bgp_config["sessions"].sort(key=lambda s: (s.get("ttl") or 255, _sort_key_ip(s.get("remote_ip"))))
        bgp_config["peer_groups"] = _build_peer_groups(bgp_config["sessions"], device_role=device_role)

    return merged
