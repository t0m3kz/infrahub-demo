"""BGP configuration helpers for device transforms."""

import logging
from ipaddress import ip_address
from typing import Any, cast

from transforms.helpers.addressing import host_ip
from transforms.helpers.bgp_policy import compose_policy
from transforms.helpers.routing_policy import IN, OUT, normalize_policies
from utils.circuit_underlay import NETWORK_APPLIANCE_KIND, OVERLAY_ROLE
from utils.exchange_transit import EXCHANGE_PEERS, INTERNET_TYPE
from utils.fabric_roles import RR_ROLES
from utils.interface_encapsulation import vlan_of
from utils.limits import DEFAULT_NAMESPACE
from utils.routing_domain import interface_security_zone

_log = logging.getLogger(__name__)

# The overlay route reflectors (utils/fabric_roles.py RR_ROLES) relay EVPN routes between VTEPs rather than only
# originating their own.


def _sort_key_ip(ip_obj: Any) -> tuple:
    """Return a sort key for an IP address object (dict or string)."""
    addr_str = ""
    if isinstance(ip_obj, dict):
        addr_str = ip_obj.get("address", "")
    elif isinstance(ip_obj, str):
        addr_str = ip_obj

    addr_str = host_ip(addr_str) or ""

    try:
        return (0, ip_address(addr_str).packed)
    except (ValueError, AttributeError):
        return (1, addr_str.encode())


def _normalize_afs(afs: list[dict[str, Any]]) -> list[str]:
    """Convert RoutingBGPAddressFamily objects to simple label strings for templates."""
    distinctive_safis = {"evpn", "vpn", "flowspec", "labeled_unicast", "multicast"}
    return [
        safi if safi in distinctive_safis else (af.get("afi") or "") for af in afs for safi in [af.get("safi") or ""]
    ]


def _extract_remote_asn_from_peering(peering_node: dict, remote_device_name: str) -> int | None:
    """Extract remote device's ASN from the peering's bgp_processes."""
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


def _peering_ends(peering_node: dict[str, Any], device_name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The peering's (local, remote) capabilities entries, split by device name."""
    peering_ifaces = peering_node.get("capabilities", [])
    if not isinstance(peering_ifaces, list) or len(peering_ifaces) != 2:
        raise ValueError(
            f"Overlay peering '{peering_node.get('name')}' expected 2 capabilities, "
            f"got {len(peering_ifaces) if isinstance(peering_ifaces, list) else 'invalid'}"
        )

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
    return local_iface, remote_iface


def _far_end_is_ours(peering_node: dict[str, Any], remote_device: str, own_devices: frozenset[str]) -> bool:
    """Whether the far end of a peering is one of OUR devices, so its session is internal."""
    if not remote_device:
        return False
    if remote_device in own_devices:
        return True
    hosted = any(
        isinstance(end, dict)
        and (end.get("device") or {}).get("name") == remote_device
        and (end.get("device") or {}).get("typename") == NETWORK_APPLIANCE_KIND
        for end in peering_node.get("capabilities") or []
    )
    return not hosted and any(
        process.get("process_role") == OVERLAY_ROLE
        and remote_device in {d.get("name") for d in process.get("capabilities") or [] if isinstance(d, dict)}
        for process in peering_node.get("bgp_processes") or []
        if isinstance(process, dict)
    )


def _build_session_from_peering(
    peering_node: dict[str, Any],
    device_name: str,
    local_as: dict[str, Any],
    interfaces: list[dict[str, Any]] | None,
    warnings: list[str] | None = None,
    *,
    own_devices: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    """Build a BGP session dict from a peering node using interfaces."""
    local_iface, remote_iface = _peering_ends(peering_node, device_name)

    password_rel = peering_node.get("password") or {}
    session: dict[str, Any] = {
        "name": peering_node.get("name"),
        "session_type": peering_node.get("session_type"),
        "bfd_enabled": peering_node.get("bfd_enabled"),
        "send_community": peering_node.get("send_community"),
        "send_extended_community": peering_node.get("send_extended_community"),
        "local_pref": peering_node.get("local_pref"),
        "med": peering_node.get("med"),
        "remove_private_as": peering_node.get("remove_private_as"),
        "password": password_rel.get("password"),
        "ttl": peering_node.get("ttl"),
        "peering_role": peering_node.get("peering_role"),
        # The far end's role: a relaying far end (a spine) passes on routes (utils/bgp_expectation.py).
        "remote_role": (remote_iface.get("device") or {}).get("role"),
        "route_reflector_client": peering_node.get("route_reflector_client", False),
        # The routing policies attached to the peering, per direction (transforms/helpers/routing_policy.py).
        "routing_policies": {
            IN: normalize_policies(peering_node.get("import_policies")),
            OUT: normalize_policies(peering_node.get("export_policies")),
        },
        "enabled": True,
    }

    ttl = peering_node.get("ttl", 255)
    remote_device_name = remote_iface.get("device", {}).get("name", "")

    # A local address in a tenant namespace (IpamNamespace is this project's VRF) makes this a VRF session — e.g. the
    # colocation edge's PROD handoff to the SD-WAN gateway.
    local_ip_obj = local_iface.get("ip_address") or {}
    full_iface = next((iface for iface in interfaces or [] if iface.get("name") == local_iface.get("name")), {})
    local_ns = interface_security_zone({**full_iface, **local_iface})
    vrf = local_ns.get("name")

    if vrf and vrf != DEFAULT_NAMESPACE:
        session["vrf"] = vrf
        # Upstream transit (namespace_type internet): the session gets the
        # default-only inbound / deny-all outbound route-maps (edges template).
        ns_type = local_ns.get("vrf_type")
        if ns_type == INTERNET_TYPE:
            session["transit"] = True
        # INTERNAL vs EXTERNAL. A regular session in a prod/non_prod namespace is external.
        elif (
            ns_type in EXCHANGE_PEERS
            and peering_node.get("peering_role") == "regular"
            and not _far_end_is_ours(peering_node, remote_device_name, own_devices)
        ):
            session["external"] = True
        session["local_ip"] = local_ip_obj
        # The handoff sub-interface and its dot1q tag (its encapsulation vlan_id, None: untagged) — the VCO payload
        # (transforms/config/controller.py) reads the tag from the field, never from the name.
        session["local_interface"] = local_iface.get("name")
        session["local_vlan"] = vlan_of(local_iface)
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

        if not local_interface_ip and not local_iface_name:
            # No cable resolved an IP — fall back to the IP already set directly on the peering's own capabilities.
            local_interface_ip = local_iface.get("ip_address")
            remote_interface_ip = remote_iface.get("ip_address")
            if local_interface_ip:
                local_iface_name = local_iface.get("name")

        if not local_interface_ip and not local_iface_name:
            return None  # No cable or direct IP connects this device to the remote — skip session

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
        # A DCI EVPN session is sourced from the loopback the peering names, not
        # the device's default one (rendered per neighbour, cisco_nxos_bgp.j2).
        if peering_node.get("peering_role") == "dci":
            session["update_source"] = local_iface.get("name")

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

    # Address families: use explicit schema config if set, otherwise derive from TTL. Overlay (TTL != 1) → EVPN.
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


_POLICY_FLAGS = ("send_community", "send_extended_community", "remove_private_as")


def _any_flags(sessions: list[dict[str, Any]], *flags: str) -> dict[str, bool]:
    """{flag: whether any session sets it}, for a peer group's shared policy."""
    return {flag: any(bool(s.get(flag)) for s in sessions) for flag in flags}


def _build_peer_groups(sessions: list[dict[str, Any]], device_role: str = "") -> list[dict[str, Any]]:
    """Assign sessions to peer groups and return group definitions."""
    sessions = [s for s in sessions if not s.get("vrf")]
    # A DCI underlay session is directly connected (ttl 1) like the underlay, but the neighbour is in another fabric —
    # it must not inherit UNDERLAY-PEERS' policy.
    dci_underlay = [s for s in sessions if s.get("peering_role") == "dci-underlay"]
    dci = [s for s in sessions if s.get("peering_role") == "dci"]
    # A fabric session is iBGP over a cabled link (ttl 1): without its own group
    # it would fall into UNDERLAY-PEERS and render as an eBGP unicast underlay.
    fabric = [s for s in sessions if s.get("peering_role") == "fabric"]
    sessions = [s for s in sessions if s.get("peering_role") not in ("dci-underlay", "dci", "fabric")]
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
                **_any_flags(underlay, "bfd_enabled", *_POLICY_FLAGS),
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
        has_rr_flag = any(bool(s.get("route_reflector_client")) for s in overlay_ibgp)
        # Every overlay RR role (RR_ROLES) is an RR when peerings have the RR flag.
        is_spine_rr = has_rr_flag and device_role in RR_ROLES
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
                **_any_flags(overlay_ibgp, *_POLICY_FLAGS),
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
        # Under ebgp-ebgp the spine tier does not reflect EVPN routes, it *re-advertises* them — and normal eBGP
        # behaviour is to rewrite the BGP next-hop to the advertising router's own address.
        is_evpn_relay = device_role in RR_ROLES
        peer_groups.append(
            {
                "name": pg_name,
                "type": "overlay",
                "session_type": "EBGP",
                **_any_flags(overlay_ebgp, *_POLICY_FLAGS),
                "next_hop_unchanged": is_evpn_relay,
                # A spine carries no VRF and no L2VNI, so it has no import route-target and would discard the very EVPN
                # NLRI it exists to relay.
                "retain_route_target_all": is_evpn_relay,
                "ebgp_multihop": 255,
                "address_families": ["evpn"],
            }
        )
        for session in overlay_ebgp:
            session["peer_group"] = pg_name

    if dci_underlay:
        pg_name = "DCI-UNDERLAY-PEERS"
        dci_underlay_afs = [
            af for af in ("ipv4", "ipv6") if any(af in (s.get("address_families") or []) for s in dci_underlay)
        ]
        peer_groups.append(
            {
                "name": pg_name,
                "type": "dci-underlay",
                "session_type": "EBGP",
                **_any_flags(dci_underlay, "bfd_enabled", *_POLICY_FLAGS),
                "address_families": dci_underlay_afs or ["ipv4"],
            }
        )
        for session in dci_underlay:
            session["peer_group"] = pg_name
            # Announce only this box's own loopbacks (RM-FABRIC-OUT) and tag what the other site sends no-export
            # (RM-DCI-IN): a DCI port is a transit for nobody's routes.
            session["export_local_only"] = True
            session["dci_inbound"] = True

    if dci:
        pg_name = "DCI-PEERS"
        peer_groups.append(
            {
                "name": pg_name,
                "type": "dci",
                "session_type": "EBGP",
                **_any_flags(dci, "bfd_enabled", *_POLICY_FLAGS),
                "ebgp_multihop": max(s.get("ttl") or 255 for s in dci),
                "address_families": ["evpn"],
            }
        )
        for session in dci:
            session["peer_group"] = pg_name
            # NX-OS Multi-Site: the BGW re-originates EVPN routes towards a
            # fabric-external peer with itself (the site VIP) as next-hop.
            session["peer_type"] = "fabric-external"

    if fabric:
        pg_name = "FABRIC-PEERS"
        fabric_afs = [
            af for af in ("ipv4", "ipv6", "evpn") if any(af in (s.get("address_families") or []) for s in fabric)
        ]
        peer_groups.append(
            {
                "name": pg_name,
                "type": "fabric",
                "session_type": "IBGP",
                **_any_flags(fabric, "bfd_enabled", *_POLICY_FLAGS),
                "address_families": fabric_afs or ["evpn"],
            }
        )
        for session in fabric:
            session["peer_group"] = pg_name
            # Both ends share the site AS (the session is iBGP, so remote_as is already local_as).
            session["export_local_only"] = True

    peer_groups.sort(key=lambda pg: pg.get("name", ""))
    return peer_groups


def _collapse_to_single_instance(configs: list[dict[str, Any]], device_name: str = "") -> list[dict[str, Any]]:
    """Reduce a device's BGP configs to the one instance a router can actually run."""
    if len(configs) <= 1:
        return configs

    def carries_evpn(cfg: dict[str, Any]) -> bool:
        return any("evpn" in (s.get("address_families") or []) for s in cfg.get("sessions", []))

    evpn_configs = [c for c in configs if carries_evpn(c)]
    if len(evpn_configs) == 1:
        anchor = evpn_configs[0]
    else:
        # No process carries EVPN, or several do (neither should happen for the strategies this project generates).
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
            # The neighbour's `remote-as` was built from this process's ASN, so the session has to keep presenting it
            # even though the instance now runs under anchor_asn.
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
    *,
    own_devices: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Extract BGP configuration from ManagedBGP services with peerings."""
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
                    peering_node, device_name, local_as, interfaces, warnings=dropped_warnings, own_devices=own_devices
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
        is_rr = any(pg.get("route_reflector_client") for pg in bgp_config["peer_groups"])
        if is_rr:
            bgp_config["cluster_id"] = host_ip(cast(dict[str, Any], bgp_config["router_id"])["address"])
        add_template_fields(bgp_config)

    return merged


def add_template_fields(bgp_config: dict[str, Any]) -> None:
    """Add the values every vendor BGP template renders from."""
    bgp_config["router_ip"] = host_ip((bgp_config.get("router_id") or {}).get("address"))
    sessions = bgp_config.get("sessions") or []
    for session in sessions:
        unnumbered = session.get("interface_name") and not session.get("remote_ip")
        session["neighbor"] = (
            session["interface_name"] if unnumbered else host_ip((session.get("remote_ip") or {}).get("address"))
        )
        session["policy"] = compose_policy(session)
    global_families = {
        af
        for item in [*(bgp_config.get("peer_groups") or []), *(s for s in sessions if not s.get("vrf"))]
        for af in item.get("address_families") or []
    }
    bgp_config["has_ipv6"] = "ipv6" in global_families
    bgp_config["has_evpn"] = "evpn" in global_families
    vrf_sessions: dict[str, list[dict[str, Any]]] = {}
    for session in sessions:
        if session.get("vrf"):
            vrf_sessions.setdefault(session["vrf"], []).append(session)
    bgp_config["vrf_sessions"] = vrf_sessions
    bgp_config["vrf_af_sessions"] = {
        vrf: {
            af: by_af
            for af in ("ipv4", "ipv6")
            for by_af in [[s for s in members if af in (s.get("address_families") or [])]]
            if by_af
        }
        for vrf, members in vrf_sessions.items()
    }
