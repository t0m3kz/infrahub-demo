"""BGP configuration helpers for device transforms."""

import logging
from ipaddress import ip_address
from typing import Any, cast

_log = logging.getLogger(__name__)

# Roles that relay EVPN routes between VTEPs rather than only originating their
# own. Spellings are the hyphenated ones from the role dropdown in
# schemas/base/dcim.yml; `super_spine` is tolerated because older data uses it.
#
# `border-spine` is included even though it is itself a VTEP (a collapsed
# spine + border-leaf for micro-fabrics): it relays other VTEPs' routes too,
# and both knobs gated on this set only affect re-advertised routes, never the
# device's own originated ones.
_EVPN_RELAY_ROLES = frozenset({"spine", "super-spine", "super_spine", "hyper-spine", "border-spine"})


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
        proc_devices = proc.get("capabilities") or []
        dev_names = {d.get("name") for d in proc_devices if isinstance(d, dict)}
        if remote_device_name in dev_names:
            local_as = proc.get("local_as")
            if isinstance(local_as, dict):
                return local_as.get("asn")
    return None


def _build_session_from_peering(
    peering_node: dict[str, Any],
    device_name: str,
    local_as: dict[str, Any],
    interfaces: list[dict[str, Any]] | None,
    warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build a BGP session dict from a peering node using interfaces.

    Determines local vs remote by matching device name in interfaces.
    Returns None only for valid "skip" conditions (for example no physical
    underlay path, or unresolved remote ASN on eBGP session).

    Raises:
        ValueError: When peering data shape is malformed.
    """
    # Get interfaces (2 entries: local + remote)
    peering_ifaces = peering_node.get("interface_capabilities", [])
    if not isinstance(peering_ifaces, list) or len(peering_ifaces) != 2:
        raise ValueError(
            f"Overlay peering '{peering_node.get('name')}' expected 2 interface_capabilities, "
            f"got {len(peering_ifaces) if isinstance(peering_ifaces, list) else 'invalid'}"
        )

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
        raise ValueError(
            f"Overlay peering '{peering_node.get('name')}' missing local/remote interface mapping for {device_name}"
        )

    password_rel = peering_node.get("password") or {}
    session: dict[str, Any] = {
        "name": peering_node.get("name"),
        "session_type": peering_node.get("session_type"),
        "bfd_enabled": peering_node.get("bfd_enabled"),
        "send_community": peering_node.get("send_community"),
        "send_extended_community": peering_node.get("send_extended_community"),
        "maximum_routes": peering_node.get("maximum_routes"),
        "local_pref": peering_node.get("local_pref"),
        "med": peering_node.get("med"),
        "remove_private_as": peering_node.get("remove_private_as"),
        "password": password_rel.get("password"),
        "ttl": peering_node.get("ttl"),
        "peering_role": peering_node.get("peering_role"),
        "route_reflector_client": peering_node.get("route_reflector_client", False),
        "enabled": True,
    }

    ttl = peering_node.get("ttl", 255)
    remote_device_name = remote_iface.get("device", {}).get("name", "")

    # A local address in a tenant namespace (IpamNamespace is this project's
    # VRF) makes this a VRF session — e.g. the colocation edge's PROD handoff to
    # the SD-WAN gateway. Its addresses are always the inline ones on the
    # peering's own handoff sub-interfaces: a cable or circuit on the parent
    # port would resolve the parent's default-namespace address instead.
    local_ip_obj = local_iface.get("ip_address") or {}
    vrf = (local_ip_obj.get("ip_namespace") or {}).get("name")

    if vrf and vrf != "default":
        session["vrf"] = vrf
        session["local_ip"] = local_ip_obj
        # The handoff sub-interface — the VCO payload (transforms/config/
        # controller.py) reads the dot1q tag off its name.
        session["local_interface"] = local_iface.get("name")
        if remote_iface.get("ip_address"):
            session["remote_ip"] = remote_iface["ip_address"]
        else:
            if warnings is not None:
                warnings.append(
                    f"VRF peering '{peering_node.get('name')}' has no address on {remote_device_name}'s side — skipped"
                )
            return None
    elif ttl == 1 and interfaces:
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
            # Inter-site circuit with no DcimCable: a TopologyVirtualCircuit is a
            # ManagedGenericInterfaces, so it shows up in its terminating port's
            # interface_capabilities, and its own interface_capabilities are the
            # circuit endpoints (local + remote). Only virtual circuits appear
            # here — TopologyPhysicalCircuit deliberately does not inherit
            # ManagedGenericInterfaces, keeping the customer/provider split that
            # carries an endpoint role the flat capability edge cannot express.
            # This path also needs the caller's query to select circuit fields
            # under interface_capabilities; queries/fragments/*.gql do not yet,
            # so today the fallback below resolves inter-site underlay IPs.
            for iface in interfaces:
                for svc in iface.get("interface_capabilities") or []:
                    if svc.get("typename") != "TopologyVirtualCircuit":
                        continue
                    for other_iface in svc.get("interface_capabilities") or []:
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
            # No cable or circuit resolved an IP — fall back to the IP already set
            # directly on the peering's own interface_capabilities (e.g. a VTI/
            # sub-interface addressed inline, with no DcimCable and no separate
            # TopologyCircuit object to traverse).
            local_interface_ip = local_iface.get("ip_address")
            remote_interface_ip = remote_iface.get("ip_address")
            if local_interface_ip:
                local_iface_name = local_iface.get("name")

        if not local_interface_ip and not local_iface_name:
            return None  # No cable, circuit, or direct IP connects this device to the remote — skip session

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
    if session_type == "IBGP":
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
    # Overlay (TTL != 1) → EVPN. Underlay (TTL == 1) → IPv4 or IPv6 unicast, based on
    # the neighbor address family — never empty, so templates can rely on membership
    # checks ('ipv4' in / 'evpn' in) instead of truthiness to decide what to activate.
    schema_afs = peering_node.get("address_families") or []
    if schema_afs:
        session["address_families"] = _normalize_afs(schema_afs)
    elif ttl != 1 and "vrf" not in session:
        session["address_families"] = ["evpn"]
    else:
        remote_ip = session.get("remote_ip") or {}
        remote_addr = remote_ip.get("address", "") if isinstance(remote_ip, dict) else ""
        session["address_families"] = ["ipv6"] if ":" in remote_addr else ["ipv4"]

    return session


def _build_peer_groups(sessions: list[dict[str, Any]], device_role: str = "") -> list[dict[str, Any]]:
    """Assign sessions to peer groups and return group definitions.

    Always creates a peer group when there is at least one session of the type:
    - UNDERLAY-PEERS: eBGP sessions with TTL=1 (P2P underlay), per-neighbor remote-as
    - EVPN-PEERS: iBGP sessions with TTL!=1 (EVPN overlay), shared remote-as from peer group
    - EVPN-OVERLAY: eBGP sessions with TTL!=1 (eBGP EVPN overlay), per-neighbor remote-as
    - DCI-PEERS: peering_role=dci sessions between EVPN Multi-Site border gateways

    VRF sessions (``session["vrf"]``) join no group: they are rendered inside
    their VRF, where the global templates' policy does not apply.

    Mutates sessions in-place by adding 'peer_group' (and 'remote_as_from_peer_group' for iBGP)
    keys. Returns list of peer group definitions.
    """
    # A DCI session is directly connected (ttl 1) like the underlay, but it
    # carries EVPN between sites and the neighbour is in another fabric — it
    # must not inherit UNDERLAY-PEERS' unicast-only policy.
    sessions = [s for s in sessions if not s.get("vrf")]
    dci = [s for s in sessions if s.get("peering_role") == "dci"]
    sessions = [s for s in sessions if s.get("peering_role") != "dci"]
    underlay = [s for s in sessions if s.get("ttl") == 1]
    overlay_ibgp = [s for s in sessions if s.get("ttl") != 1 and str(s.get("session_type", "")).upper() == "IBGP"]
    overlay_ebgp = [
        s for s in sessions if s.get("ttl") != 1 and str(s.get("session_type", "")).upper() in ("EBGP", "EBGP_MULTIHOP")
    ]

    peer_groups: list[dict[str, Any]] = []

    if underlay:
        pg_name = "UNDERLAY-PEERS"
        underlay_afs: list[str] = []
        for af in ("ipv4", "ipv6"):
            if any(af in (s.get("address_families") or []) for s in underlay) and af not in underlay_afs:
                underlay_afs.append(af)
        peer_groups.append(
            {
                "name": pg_name,
                "type": "underlay",
                "session_type": "EBGP",
                "bfd_enabled": any(bool(s.get("bfd_enabled")) for s in underlay),
                "send_community": any(bool(s.get("send_community")) for s in underlay),
                "send_extended_community": any(bool(s.get("send_extended_community")) for s in underlay),
                "remove_private_as": any(bool(s.get("remove_private_as")) for s in underlay),
                "address_families": underlay_afs or ["ipv4"],
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
        _RR_ROLES = ("spine", "super-spine", "super_spine", "border-spine")
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
                "send_community": any(bool(s.get("send_community")) for s in overlay_ibgp),
                "send_extended_community": any(bool(s.get("send_extended_community")) for s in overlay_ibgp),
                "remove_private_as": any(bool(s.get("remove_private_as")) for s in overlay_ibgp),
                "route_reflector_client": rr_client,
                "next_hop_unchanged": rr_client,  # RRs must not change next-hop for EVPN clients
                "address_families": ["evpn"],
            }
        )
        for session in overlay_ibgp:
            session["peer_group"] = pg_name
            session["remote_as_from_peer_group"] = True

    if overlay_ebgp:
        pg_name = "EVPN-OVERLAY"
        # Under ebgp-ebgp the spine tier does not reflect EVPN routes, it
        # *re-advertises* them — and normal eBGP behaviour is to rewrite the BGP
        # next-hop to the advertising router's own address. A spine is not a
        # VTEP and has no `interface nve1`, so without next-hop-unchanged every
        # remote VTEP learns the spine's loopback as the tunnel endpoint and
        # builds a VXLAN tunnel to a device that cannot terminate it: the
        # sessions come up, the routes are present, and traffic is silently
        # blackholed. RFC 8365 §5.1.2.1.
        #
        # Only the relaying tier needs it. A leaf/border-leaf originates its own
        # routes with itself as the next-hop, so setting it there would be
        # wrong (it would preserve a next-hop the leaf must own).
        is_evpn_relay = device_role in _EVPN_RELAY_ROLES
        peer_groups.append(
            {
                "name": pg_name,
                "type": "overlay",
                "session_type": "EBGP",
                "send_community": any(bool(s.get("send_community")) for s in overlay_ebgp),
                "send_extended_community": any(bool(s.get("send_extended_community")) for s in overlay_ebgp),
                "remove_private_as": any(bool(s.get("remove_private_as")) for s in overlay_ebgp),
                "next_hop_unchanged": is_evpn_relay,
                # A spine carries no VRF and no L2VNI, so it has no import
                # route-target and would discard the very EVPN NLRI it exists to
                # relay. `retain route-target all` keeps them.
                "retain_route_target_all": is_evpn_relay,
                "ebgp_multihop": 255,
                "address_families": ["evpn"],
            }
        )
        for session in overlay_ebgp:
            session["peer_group"] = pg_name

    if dci:
        pg_name = "DCI-PEERS"
        dci_afs = [af for af in ("ipv4", "ipv6", "evpn") if any(af in (s.get("address_families") or []) for s in dci)]
        peer_groups.append(
            {
                "name": pg_name,
                "type": "dci",
                "session_type": "EBGP",
                "bfd_enabled": any(bool(s.get("bfd_enabled")) for s in dci),
                "send_community": any(bool(s.get("send_community")) for s in dci),
                "send_extended_community": any(bool(s.get("send_extended_community")) for s in dci),
                "remove_private_as": any(bool(s.get("remove_private_as")) for s in dci),
                "address_families": dci_afs or ["evpn"],
            }
        )
        for session in dci:
            session["peer_group"] = pg_name
            # NX-OS Multi-Site: the BGW re-originates EVPN routes towards a
            # fabric-external peer with itself (the site VIP) as next-hop.
            session["peer_type"] = "fabric-external"

    peer_groups.sort(key=lambda pg: pg.get("name", ""))
    return peer_groups


def _collapse_to_single_instance(configs: list[dict[str, Any]], device_name: str = "") -> list[dict[str, Any]]:
    """Reduce a device's BGP configs to the one instance a router can actually run.

    Every platform this project targets allows exactly ONE BGP ASN per routing
    instance. Under ebgp-ebgp the merge by ASN above already leaves one config,
    because the underlay and overlay processes share the device's ASN. Under
    ebgp-ibgp they do not: the underlay is eBGP on a per-device ASN (65001) and
    the overlay is iBGP on the fabric-wide ASN (65000), so two configs survive
    and the templates would emit two `router bgp` stanzas. The second one is
    rejected by the device, which means the whole push fails — or worse, on a
    platform that accepts the first and ignores the rest, half the routing
    silently disappears.

    The instance ASN has to be the OVERLAY one. It is the shared ASN, so overlay
    sessions stay natively iBGP and the route-reflector semantics the EVPN design
    depends on keep working. The underlay's per-device ASN then moves onto its own
    sessions as ``local_as_override``, which templates render as
    ``local-as <asn> no-prepend replace-as``: the eBGP neighbour still sees 65001
    exactly as its own ``remote-as 65001`` expects, and 65000 never leaks into the
    underlay AS-path. Doing it the other way round — instance on the underlay ASN,
    override on the overlay sessions — would turn the EVPN sessions into eBGP and
    break route reflection.

    Returns a single-element list, or the input unchanged when there is nothing to
    collapse.
    """
    if len(configs) <= 1:
        return configs

    def carries_evpn(cfg: dict[str, Any]) -> bool:
        return any("evpn" in (s.get("address_families") or []) for s in cfg.get("sessions", []))

    evpn_configs = [c for c in configs if carries_evpn(c)]
    if len(evpn_configs) == 1:
        anchor = evpn_configs[0]
    else:
        # No process carries EVPN, or several do (neither should happen for the
        # strategies this project generates). Anchoring on the lowest ASN is
        # arbitrary but still produces ONE valid instance instead of N invalid
        # ones — say so, because the choice may not be the intended one.
        anchor = configs[0]
        _log.error(
            "%s: %d BGP processes with different ASNs and %s carries the EVPN address-family, "
            "so the BGP instance ASN cannot be determined from the overlay. Anchoring on the "
            "lowest ASN %s and rendering the others as local-as overrides; verify the result.",
            device_name or "device",
            len(configs),
            "none" if not evpn_configs else f"{len(evpn_configs)} of them",
            cast(dict[str, Any], anchor["local_as"])["asn"],
        )

    anchor_asn = cast(dict[str, Any], anchor["local_as"])["asn"]
    for cfg in configs:
        if cfg is anchor:
            continue
        override = cast(dict[str, Any], cfg["local_as"])
        for session in cfg.get("sessions", []):
            # The neighbour's `remote-as` was built from this process's ASN, so
            # the session has to keep presenting it even though the instance now
            # runs under anchor_asn.
            session["local_as_override"] = override
            anchor["sessions"].append(session)
        _log.info(
            "%s: folded BGP process %s (AS %s) into the AS %s instance; its %d session(s) "
            "render with local-as %s no-prepend replace-as.",
            device_name or "device",
            cfg.get("name") or "?",
            override.get("asn"),
            anchor_asn,
            len(cfg.get("sessions", [])),
            override.get("asn"),
        )

    return [anchor]


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

    bgp_services = [svc for svc in device_capabilities if svc.get("typename") == "ManagedBGP"]
    if not bgp_services:
        return []

    bgp_configs = []

    for service in bgp_services:
        service_name = service.get("name")

        local_as = cast(dict[str, Any], service["local_as"])

        bgp_config = {
            "name": service_name,
            "status": service.get("status"),
            "multipath": service.get("multipath"),
            "graceful_restart": service.get("graceful_restart"),
            "confederation_identifier": service.get("confederation_identifier"),
            "local_as": local_as,
            "router_id": cast(dict[str, Any], service["router_id"]),
        }

        sessions = []
        dropped_warnings: list[str] = []
        peerings = service.get("peerings", [])

        if isinstance(peerings, list):
            for peering_node in peerings:
                session = _build_session_from_peering(
                    peering_node, device_name, local_as, interfaces, warnings=dropped_warnings
                )
                if session:
                    # Peering templates expect local_as on each session object.
                    session["local_as"] = local_as
                    sessions.append(session)

        for msg in dropped_warnings:
            _log.warning(msg)

        bgp_config["sessions"] = sessions
        bgp_configs.append(bgp_config)

    # Merge BGP processes that share the same local ASN into a single config block.
    # This handles eBGP-eBGP where underlay and overlay processes reuse the same per-device ASN.
    by_asn: dict[Any, dict] = {}
    for bgp_config in bgp_configs:
        asn = cast(dict[str, Any], bgp_config["local_as"])["asn"]
        if asn not in by_asn:
            by_asn[asn] = bgp_config
        else:
            existing = by_asn[asn]
            existing["sessions"].extend(bgp_config.get("sessions", []))

    merged = list(by_asn.values())

    # Sort BGP configs by local ASN for deterministic output
    merged.sort(key=lambda c: cast(dict[str, Any], c["local_as"])["asn"])

    # Collapse distinct ASNs onto one BGP instance (ebgp-ibgp).
    merged = _collapse_to_single_instance(merged, device_name=device_name)

    # Assign peer groups to sessions with common attributes
    for bgp_config in merged:
        # Sort sessions (neighbors) by (ttl group, IP address) for deterministic config output
        bgp_config["sessions"].sort(key=lambda s: (s.get("ttl") or 255, _sort_key_ip(s.get("remote_ip"))))
        bgp_config["peer_groups"] = _build_peer_groups(bgp_config["sessions"], device_role=device_role)

        # RR devices get an explicit cluster-id equal to their router-id for loop prevention.
        # Cisco/Arista/FRR default cluster-id to router-id when unset, but being explicit
        # makes the intent clear and is considered best practice.
        is_rr = any(pg.get("route_reflector_client") for pg in bgp_config["peer_groups"])
        if is_rr:
            bgp_config["cluster_id"] = cast(dict[str, Any], bgp_config["router_id"])["address"].split("/")[0]

    return merged
