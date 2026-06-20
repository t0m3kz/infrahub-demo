"""Routing planner for BGP/OSPF configuration.

Pure helper — no database/client access. Plans routing objects as flat dicts
with HFID names for upsert-based creation.

All objects (except AS) use deterministic HFID names and are saved with
allow_upsert=True, eliminating the need for new/existing tracking.

AS objects are special: new devices get from_pool allocation, existing
devices get their known AS ID for re-save.

Save order: AS -> BGP + OSPF -> Peerings + OSPF Interfaces.

Strategies:
    - ebgp-ebgp: eBGP underlay + eBGP overlay (per-device ASN)
    - ebgp-ibgp: eBGP underlay + iBGP overlay (shared ASN)
    - ospf-ibgp: OSPF underlay + iBGP overlay (shared ASN)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NamedTuple


@dataclass
class RoutingPlanInput:
    """Input for routing plan builder.

    All objects come pre-queried by the generator as SDK objects:
      - bottom_devices / top_devices: device name strings
      - underlay: ManagedBGP underlay processes (SDK objects with device + local_as)
      - overlay: ManagedBGP overlay or ManagedOSPF (existing) SDK objects
      - interfaces: DcimPhysicalInterface fabric-p2p SDK objects (device, cable, name)
      - loopback_interfaces: DcimVirtualInterface loopback SDK objects (device + ip_address)
      - options: RoutingOptions dict (design, asn_pool, overlay_as_id, ospf_area_id)
    """

    bottom_devices: list[str] = field(default_factory=list)
    top_devices: list[str] = field(default_factory=list)
    underlay: list[Any] = field(default_factory=list)
    overlay: list[Any] = field(default_factory=list)
    interfaces: list[Any] = field(default_factory=list)
    loopback_interfaces: list[Any] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    routing_strategy: str = "ebgp-ebgp"
    deployment_name: str = ""


@dataclass
class RoutingPlan:
    """Flat routing plan — all dicts, saved with allow_upsert=True.

    Save order: autonomous_systems -> bgp_processes + ospf_processes
                -> bgp_peerings + ospf_interfaces.

    AS dicts have either:
        - "_existing_id": known AS ID (re-save for group tracking)
        - "asn": {"from_pool": ...} (new allocation)
    Both carry "_for_device": device_name for generator resolution.

    BGP dicts have "local_as" as either:
        - {"id": "known-id"} (existing AS)
        - {"_for_device": "device-name"} (resolved after AS creation)
    """

    autonomous_systems: list[dict] = field(default_factory=list)
    bgp_processes: list[Any] = field(default_factory=list)
    ospf_processes: list[Any] = field(default_factory=list)
    ospf_interfaces: list[Any] = field(default_factory=list)
    bgp_peerings: list[Any] = field(default_factory=list)


class BGPSession(NamedTuple):
    dev1_name: str
    dev1_id: str
    dev2_name: str
    dev2_id: str
    session_type: str
    af_types: list[str]


class _BGPDevice(NamedTuple):
    name: str
    id: str
    role: str


class RoutingStrategy(str, Enum):
    """Supported routing strategies for fabric underlay + overlay."""

    EBGP_EBGP = "ebgp-ebgp"
    EBGP_IBGP = "ebgp-ibgp"
    OSPF_IBGP = "ospf-ibgp"


def _safe_device_name(bgp: Any) -> str | None:
    """Extract device name from a prefetched ManagedBGP/ManagedOSPF object."""
    try:
        peers = bgp.device_capabilities.peers
        if peers:
            return peers[0].name.value
        return None
    except (AttributeError, ValueError, IndexError):
        return None


def _safe_as_id(bgp: Any) -> str | None:
    """Extract AS ID from a ManagedBGP object's local_as relationship."""
    return bgp.local_as.id or None


def _make_bgp_proc(
    name: str,
    suffix: str,
    description: str,
    local_as: dict,
    router_id: dict,
    device_id: str,
) -> dict[str, Any]:
    return {
        "name": f"{name}-bgp-{suffix}",
        "description": description,
        "status": "active",
        "local_as": local_as,
        "router_id": router_id,
        "device_capabilities": [{"id": device_id}],
    }


class RoutingPlanner:
    """Plans routing configuration from pre-queried SDK objects.

    The planner builds a device_map from loopback interfaces, then uses it
    across all planning methods. The device_map is keyed by device name:

        {"spine-1": {"id": "uuid", "role": "spine",
                      "router_id": {"id": "ip-uuid"},
                      "loopback_ip": "10.0.0.1"}, ...}
    """

    def __init__(self, deployment_id: str, logger: Any = None, strict: bool = False):
        self.deployment_id = deployment_id
        self.logger = logger
        self.strict = strict

    # ================================================================
    # Main Entry Point
    # ================================================================

    def build_routing_plan(self, inp: RoutingPlanInput) -> RoutingPlan:
        """Build complete routing plan from pre-queried SDK objects."""
        underlay_type, overlay_type = inp.routing_strategy.split("-")
        plan = RoutingPlan()

        all_device_names = set(inp.bottom_devices + inp.top_devices)
        if not all_device_names:
            if self.logger:
                self.logger.warning("No routing devices provided")
            return plan

        # Build device map from loopback interfaces
        device_map = self._build_device_map(inp.loopback_interfaces)

        # Extract existing AS IDs from underlay BGP so existing devices reuse their
        # ASN instead of drawing a new one from the pool (Number-pool allocation is
        # not idempotent across create()). BGP processes themselves are always
        # re-saved with allow_upsert=True — local_as is cardinality-one and upserts
        # cleanly (verified on Infrahub 1.9.6), so no new/existing split is needed.
        existing_as_by_device: dict[str, str] = {}
        for bgp in inp.underlay:
            dev_name = _safe_device_name(bgp)
            as_id = _safe_as_id(bgp)
            if dev_name and as_id:
                existing_as_by_device[dev_name] = as_id

        design = inp.options.get("design")
        asn_pool = inp.options.get("asn_pool")
        overlay_as_id = inp.options.get("overlay_as_id")
        existing_ospf_area = inp.options.get("ospf_area_id")

        # ---- Underlay ----
        if not inp.options.get("skip_underlay"):
            if underlay_type == "ebgp":
                self._plan_ebgp_underlay(
                    plan,
                    device_map,
                    inp.interfaces,
                    existing_as_by_device,
                    asn_pool,
                    set(inp.top_devices),
                )
            elif underlay_type == "ospf":
                if not inp.deployment_name:
                    raise ValueError("deployment_name is required for OSPF underlay")
                if not existing_ospf_area:
                    raise ValueError(
                        "existing_ospf_area is required for OSPF underlay. "
                        "The DC generator must create the shared OSPF area first."
                    )
                self._plan_ospf_underlay(
                    plan,
                    device_map,
                    inp.interfaces,
                    inp.deployment_name,
                    existing_ospf_area,
                )
            else:
                raise ValueError(f"Unknown underlay: {underlay_type}")

        # ---- Overlay BGP processes ----
        if overlay_type == "ibgp" and not overlay_as_id:
            raise ValueError(
                "overlay_as_id is required for iBGP overlay. The DC generator must create the shared overlay AS first."
            )
        if overlay_type in ("ebgp", "ibgp"):
            self._plan_overlay_processes(
                plan,
                device_map,
                overlay_as_id if overlay_type == "ibgp" else None,
                set(inp.top_devices),
            )

        # ---- Overlay peerings ----
        if design:
            overlay_bgp = [b for b in plan.bgp_processes if b["name"].endswith("-bgp-overlay")]
            planned_device_ids = {b["device_capabilities"][0]["id"] for b in overlay_bgp}

            # Include remote devices with existing overlay BGP not yet in plan
            existing_overlay_names: set[str] = set()
            for obj in inp.overlay:
                dev_name = _safe_device_name(obj)
                if dev_name:
                    existing_overlay_names.add(dev_name)

            for name, info in device_map.items():
                if info["id"] in planned_device_ids:
                    continue
                if name in existing_overlay_names and info.get("router_id"):
                    overlay_bgp.append(
                        {
                            "name": f"{name}-bgp-overlay",
                            "device_capabilities": [{"id": info["id"]}],
                        }
                    )

            if overlay_bgp:
                self._plan_overlay_peerings(
                    plan,
                    overlay_type=overlay_type,
                    bgp_processes=overlay_bgp,
                    device_map=device_map,
                    bottom_device_names=set(inp.bottom_devices),
                    top_device_names=set(inp.top_devices),
                )

        return plan

    # ================================================================
    # Device Map Builder
    # ================================================================

    @staticmethod
    def _build_device_map(loopback_interfaces: list[Any]) -> dict[str, dict[str, Any]]:
        """Build device info from loopback interfaces.

        Returns dict keyed by device name::

            {"spine-1": {"id": "uuid", "role": "spine",
                         "router_id": {"id": "ip-uuid"},
                         "loopback_ip": "10.0.0.1"}}

        Loopback interfaces must be queried with:
            include=["device", "ip_address"], prefetch_relationships=True
        """
        device_map: dict[str, dict[str, Any]] = {}

        # Sort by interface id so router_id selection is deterministic regardless of
        # query-return order: the lowest-id loopback with a valid IP wins per device.
        for lb in sorted(loopback_interfaces, key=lambda lb: lb.id):
            dev = lb.device.peer
            name = dev.name.value
            dev_id = dev.id
            role = dev.role.value

            if name not in device_map:
                device_map[name] = {"id": dev_id, "role": role}

            # First loopback (by sorted id) with a valid IP wins for router_id
            if "router_id" not in device_map[name]:
                ip_id = lb.ip_address.id
                if ip_id:
                    device_map[name]["router_id"] = {"id": ip_id}
                    # display_label is "10.0.0.1/32" — strip prefix
                    device_map[name]["loopback_ip"] = str(lb.ip_address.display_label).split("/")[0]
                    device_map[name]["loopback_interface_id"] = lb.id

        return device_map

    # ================================================================
    # eBGP Underlay (interface-driven)
    # ================================================================

    def _plan_ebgp_underlay(
        self,
        plan: RoutingPlan,
        device_map: dict[str, dict],
        interfaces: list[Any],
        existing_as_by_device: dict[str, str],
        asn_pool: Any,
        top_device_names: set[str] | None = None,
    ) -> None:
        """Build eBGP underlay by iterating cable pairs.

        For each cable pair: ensure AS + BGP process for bottom devices only,
        then create the eBGP peering. Top devices (spines when called from rack)
        already have their BGP processes created by the pod generator — skip
        AS/BGP creation for them but still create peerings via HFID reference.
        Deduplicates by device name (AS/BGP) and by device pair (peerings).

        Every BGP process is emitted for upsert; existing processes re-save
        cleanly (local_as is cardinality-one), so no new/existing tracking.
        """
        id_to_name = {info["id"]: name for name, info in device_map.items()}
        _top = top_device_names or set()

        # Phase 1: AS + BGP process for all bottom devices (device_map, not cable-driven).
        # Top devices are skipped — their BGP is owned by an upper generator layer.
        # Decoupled from cables so processes exist even before cabling is complete.
        bgp_planned: set[str] = set()
        for name in sorted(device_map.keys()):
            if name in _top or name in bgp_planned:
                continue

            info = device_map[name]
            existing_as_id = existing_as_by_device.get(name)

            if existing_as_id:
                plan.autonomous_systems.append({"_existing_id": existing_as_id, "_for_device": name})
            elif asn_pool is not None:
                plan.autonomous_systems.append(
                    {
                        "asn": {"from_pool": {"id": asn_pool}},
                        "description": f"{name} underlay ASN",
                        "status": "active",
                        "_for_device": name,
                    }
                )
            else:
                if self.logger:
                    self.logger.warning(f"No ASN pool for {name}")
                continue

            router_id = info.get("router_id")
            if not router_id:
                if self.strict:
                    raise ValueError(f"No router-id for {name}")
                if self.logger:
                    self.logger.warning(f"No router-id for {name}, skipping BGP")
                continue

            local_as = {"id": existing_as_id} if existing_as_id else {"_for_device": name}
            proc = _make_bgp_proc(
                name, "underlay", f"eBGP process for {name} underlay", local_as, router_id, info["id"]
            )
            plan.bgp_processes.append(proc)
            bgp_planned.add(name)

        # Phase 2: Peerings — cable-driven, requires both ends to have BGP.
        cable_map: dict[str, list] = defaultdict(list)
        for iface in interfaces:
            if iface.cable and iface.cable.id:
                cable_map[iface.cable.id].append(iface)

        cable_pairs: list[tuple] = []
        for ifaces in cable_map.values():
            if len(ifaces) != 2:
                continue
            a, b = ifaces
            a_name = id_to_name.get(a.device.id)
            b_name = id_to_name.get(b.device.id)
            if not a_name or not b_name:
                continue
            if a_name > b_name:
                a, b = b, a
                a_name, b_name = b_name, a_name
            cable_pairs.append((a, b, a_name, b_name))

        cable_pairs.sort(key=lambda x: (x[2], x[3]))
        seen_pairs: set[tuple[str, str]] = set()

        for a, b, a_name, b_name in cable_pairs:
            pair = (a_name, b_name)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)

            if (a_name not in bgp_planned and a_name not in _top) or (b_name not in bgp_planned and b_name not in _top):
                continue

            ia = a.name.value
            ib = b.name.value
            ia_h = ia.replace("/", "_")
            ib_h = ib.replace("/", "_")

            plan.bgp_peerings.append(
                {
                    "name": f"underlay--{a_name}--{ia_h}--{b_name}--{ib_h}",
                    "description": f"eBGP underlay: {a_name} ({ia}) <-> {b_name} ({ib})",
                    "session_type": "EBGP",
                    "bfd_enabled": True,
                    "send_community": True,
                    "ttl": 1,
                    "interfaces": [{"id": a.id}, {"id": b.id}],
                    "bgp_processes": [
                        {"hfid": f"{a_name}-bgp-underlay"},
                        {"hfid": f"{b_name}-bgp-underlay"},
                    ],
                }
            )

    # ================================================================
    # Overlay BGP Processes
    # ================================================================

    def _plan_overlay_processes(
        self,
        plan: RoutingPlan,
        device_map: dict[str, dict],
        overlay_as_id: str | None,
        top_device_names: set[str] | None = None,
    ) -> None:
        """Build overlay BGP processes. If overlay_as_id is set → iBGP (shared ASN); otherwise → eBGP (per-device ASN).

        Devices in top_device_names are skipped — they already have overlay BGP created by
        an upper generator layer. They will be picked up via existing_overlay_names fallback
        in build_routing_plan so peerings are still generated correctly.

        Every process is emitted for upsert; existing ones re-save cleanly.
        """
        is_ibgp = bool(overlay_as_id)
        desc_prefix = "iBGP process for" if is_ibgp else "eBGP process for"
        _top = top_device_names or set()

        device_as_refs: dict[str, dict] = {}
        if not is_ibgp:
            for as_dict in plan.autonomous_systems:
                dev_name = as_dict["_for_device"]
                if "_existing_id" in as_dict:
                    device_as_refs[dev_name] = {"id": as_dict["_existing_id"]}
                else:
                    device_as_refs[dev_name] = {"_for_device": dev_name}

        for name in sorted(device_map.keys()):
            if name in _top:
                continue
            info = device_map[name]

            if is_ibgp:
                as_ref: dict = {"id": overlay_as_id}
            else:
                maybe_as_ref = device_as_refs.get(name)
                if not maybe_as_ref:
                    continue
                as_ref = maybe_as_ref

            router_id = info.get("router_id")
            if not router_id:
                if is_ibgp:
                    if self.strict:
                        raise ValueError(f"No router-id for {name}")
                    if self.logger:
                        self.logger.warning(f"No router-id for {name}, skipping BGP")
                continue

            proc = _make_bgp_proc(
                name,
                "overlay",
                f"{desc_prefix} {name} overlay/EVPN",
                as_ref,
                router_id,
                info["id"],
            )
            plan.bgp_processes.append(proc)

    # ================================================================
    # OSPF Underlay
    # ================================================================

    def _plan_ospf_underlay(
        self,
        plan: RoutingPlan,
        device_map: dict[str, dict],
        interfaces: list[Any],
        deployment_name: str,
        existing_area_id: str,
    ) -> None:
        """Build OSPF underlay: processes and P2P interface bindings."""
        area_ref: dict[str, Any] = {"id": existing_area_id}
        id_to_name = {info["id"]: name for name, info in device_map.items()}
        # Group interfaces by device name
        device_interfaces: dict[str, list] = defaultdict(list)
        for iface in interfaces:
            dev_name = id_to_name.get(iface.device.id)
            if dev_name:
                device_interfaces[dev_name].append(iface)

        for name in sorted(device_map.keys()):
            info = device_map[name]
            router_id = info.get("router_id")
            if not router_id:
                if self.strict:
                    raise ValueError(f"No router-id for {name}")
                if self.logger:
                    self.logger.warning(f"No router-id for {name}, skipping OSPF")
                continue

            ospf_name = f"{name}-ospf-underlay"
            plan.ospf_processes.append(
                {
                    "name": ospf_name,
                    "description": f"OSPF process for {name} underlay",
                    "status": "active",
                    "process_id": "1",
                    "version": "ospf",
                    "router_type": "internal",
                    "device_capabilities": [{"id": info["id"]}],
                    "router_id": router_id,
                }
            )

            for iface in device_interfaces.get(name, []):
                if not (iface.cable and iface.cable.id):
                    continue
                iname = iface.name.value
                plan.ospf_interfaces.append(
                    {
                        "name": f"{name}-{iname}-ospf-underlay",
                        "description": f"OSPF config for {name}:{iname}",
                        "mode": "peer_to_peer",
                        "ospf_process": {"hfid": ospf_name},
                        "area": area_ref,
                        "interfaces": [{"id": iface.id}],
                    }
                )

    # ================================================================
    # Overlay Peerings (loopback-based)
    # ================================================================

    def _plan_overlay_peerings(
        self,
        plan: RoutingPlan,
        overlay_type: str,
        bgp_processes: list[dict],
        device_map: dict[str, dict],
        bottom_device_names: set[str] | None = None,
        top_device_names: set[str] | None = None,
    ) -> None:
        """Build overlay peerings using device loopback IPs from device_map."""
        id_to_name = {info["id"]: name for name, info in device_map.items()}
        device_bgp_map: dict[str, dict] = {}

        for bgp in bgp_processes:
            did = bgp["device_capabilities"][0]["id"]
            device_bgp_map[did] = bgp

        device_data: list[_BGPDevice] = []
        for bgp in bgp_processes:
            did = bgp["device_capabilities"][0]["id"]
            name = id_to_name.get(did)
            if not name:
                continue

            info = device_map[name]
            loopback_ip = info.get("loopback_ip")

            if not loopback_ip:
                if self.strict:
                    raise ValueError(f"No loopback IP for {name}")
                if self.logger:
                    self.logger.warning(f"No loopback IP for {name}, skipping overlay")
                continue

            device_data.append(_BGPDevice(name=name, id=did, role=info["role"]))

        if not device_data:
            return

        session_planner = _BGPSessionPlanner(devices=device_data)
        session_plan = session_planner.build_session_plan(session_type=overlay_type)

        if bottom_device_names is not None and top_device_names is not None:
            if bottom_device_names and top_device_names:
                # Cross-layer: one device in bottom, the other in top
                session_plan = [
                    s
                    for s in session_plan
                    if (s[0] in bottom_device_names and s[2] in top_device_names)
                    or (s[0] in top_device_names and s[2] in bottom_device_names)
                ]
            else:
                # Single-set: both devices must be in the combined set
                all_scoped = bottom_device_names | top_device_names
                session_plan = [s for s in session_plan if s[0] in all_scoped and s[2] in all_scoped]

        if not session_plan:
            return

        for d1_name, d1_id, d2_name, d2_id, stype, af_types in session_plan:
            if d1_id not in device_bgp_map or d2_id not in device_bgp_map:
                continue

            bgp1, bgp2 = device_bgp_map[d1_id], device_bgp_map[d2_id]
            left_name, right_name = sorted([d1_name, d2_name])
            is_rr = overlay_type == "ibgp"

            # Overlay peers via loopback interfaces
            peering_interfaces = []
            for dname in [d1_name, d2_name]:
                lb_id = device_map.get(dname, {}).get("loopback_interface_id")
                if lb_id:
                    peering_interfaces.append({"id": lb_id})

            peering: dict = {
                "name": f"overlay-evpn--{left_name}--{right_name}",
                "description": f"{stype.upper()} EVPN overlay: {d1_name} <-> {d2_name}",
                "session_type": "EBGP_MULTIHOP" if stype == "ebgp" else "IBGP",
                "ttl": 2 if stype == "ebgp" else 255,
                "bfd_enabled": True,
                "send_community": True,
                "send_extended_community": True,
                "route_reflector_client": bool(stype == "ibgp" and is_rr),
                "bgp_processes": [{"hfid": bgp1["name"]}, {"hfid": bgp2["name"]}],
            }
            if peering_interfaces:
                peering["interfaces"] = peering_interfaces

            plan.bgp_peerings.append(peering)


# ================================================================
# BGP Session Planner (internal)
# ================================================================


class _BGPSessionPlanner:
    """Plans BGP session topology (route reflector, spine-leaf, etc.)."""

    def __init__(self, devices: list[_BGPDevice]):
        self.devices = devices

    def build_session_plan(self, session_type: str) -> list[BGPSession]:
        """Build BGP session plan using route-reflector topology."""
        sessions = self._build_route_reflector(session_type)
        return [s if isinstance(s, BGPSession) else BGPSession(*s) for s in sessions]

    def _build_route_reflector(self, session_type: str) -> list[tuple]:
        """Spines + super-spines as RR, leafs/tors as clients."""
        roles = {d.role for d in self.devices}
        rrs = [d for d in self.devices if d.role in ("super-spine", "super_spine", "spine")]
        clients = [d for d in self.devices if d.role in ("leaf", "border-leaf", "tor")]
        if not rrs:
            rrs = [d for d in self.devices if d.role in ("leaf", "border-leaf")]
            clients = [d for d in self.devices if d.role == "tor"]
        af = ["evpn"]
        has_super_spine = "super-spine" in roles or "super_spine" in roles
        if rrs and not clients and has_super_spine:
            return [
                (rrs[i].name, rrs[i].id, rrs[j].name, rrs[j].id, session_type, af)
                for i in range(len(rrs))
                for j in range(i + 1, len(rrs))
            ]
        return [(c.name, c.id, rr.name, rr.id, session_type, af) for rr in rrs for c in clients]
