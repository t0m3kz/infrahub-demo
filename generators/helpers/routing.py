"""Routing planner for BGP/OSPF configuration (pure, no database access): flat dicts saved with allow_upsert=True.
Save order: AS -> BGP + OSPF -> peerings + OSPF interfaces. Strategies: ebgp-ebgp, ebgp-ibgp, ospf-ibgp."""

from __future__ import annotations

from collections import defaultdict
from enum import Enum
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

from generators.mixins.types import RoutingOptions
from utils.fabric_roles import OVERLAY_ROLES, RR_CLIENT_ROLES, RR_ROLES

_SPINE_ROLES = frozenset({"spine", "border-spine"})


class RoutingPlanInput(BaseModel):
    """Input for the routing plan builder; all objects are pre-queried SDK objects. ``bottom_role``/``top_role`` are
    authoritative over the queried role; ``mlag_pairs`` maps device -> MLAG domain (one underlay ASN per pair).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bottom_devices: list[str] = Field(default_factory=list)
    top_devices: list[str] = Field(default_factory=list)
    bottom_role: str = ""
    top_role: str = ""
    underlay: list[Any] = Field(default_factory=list)
    overlay: list[Any] = Field(default_factory=list)
    interfaces: list[Any] = Field(default_factory=list)
    loopback_interfaces: list[Any] = Field(default_factory=list)
    options: RoutingOptions = Field(default_factory=RoutingOptions)
    routing_strategy: str = "ebgp-ebgp"
    evpn_af_id: str = ""
    underlay_password_id: str = ""
    overlay_password_id: str = ""
    mlag_pairs: dict[str, str] = Field(default_factory=dict)


class PendingASRef(BaseModel):
    """Placeholder for a BGP process's ``local_as`` before its AS is created; the generator swaps in a real ``{"id":
    ...}`` (``RoutingMixin.create_routing``).
    """

    model_config = ConfigDict(frozen=True)

    device: str


class RoutingPlan(BaseModel):
    """Flat routing plan: all dicts, saved with allow_upsert=True. Save order: autonomous_systems -> bgp_processes +
    ospf_processes -> bgp_peerings + ospf_interfaces -> ospf_peerings. AS dicts carry "_existing_id" (re-save) or "asn":
    {"from_pool": ...}, plus "_for_device"; BGP "local_as" is {"id": ...} or PendingASRef(device=...).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    autonomous_systems: list[dict] = Field(default_factory=list)
    bgp_processes: list[Any] = Field(default_factory=list)
    ospf_processes: list[Any] = Field(default_factory=list)
    ospf_interfaces: list[Any] = Field(default_factory=list)
    bgp_peerings: list[Any] = Field(default_factory=list)
    ospf_peerings: list[Any] = Field(default_factory=list)


class _BGPDevice(NamedTuple):
    name: str
    id: str
    role: str


class RoutingStrategy(str, Enum):
    """Supported routing strategies for fabric underlay + overlay."""

    EBGP_EBGP = "ebgp-ebgp"
    EBGP_IBGP = "ebgp-ibgp"
    OSPF_IBGP = "ospf-ibgp"


def underlay_pool_is_ipv6(underlay_protocol: str, pool: str) -> bool:
    """Whether a fabric's ``pool`` ("management", "technical" or "loopback") is IPv6 space; the single source of the
    family per pool. ipv4: all IPv4; ipv6: technical and loopback IPv6; dual_stack: technical IPv6, loopback IPv4.
    Management is always IPv4.
    """
    if pool == "management":
        return False
    if underlay_protocol == "dual_stack":
        return pool == "technical"
    return underlay_protocol == "ipv6"


def p2p_prefix_length(underlay_protocol: str) -> int:
    """P2P link prefix length of the technical pool's family: /31 (RFC 3021) or /127 (RFC 6164)."""
    return 127 if underlay_pool_is_ipv6(underlay_protocol, "technical") else 31


def loopback_prefix_length(underlay_protocol: str) -> int:
    """Loopback host-route prefix length of the loopback pool's family: /32 or /128."""
    return 128 if underlay_pool_is_ipv6(underlay_protocol, "loopback") else 32


def _safe_device_name(bgp: Any) -> str | None:
    """Device name of a ManagedBGP/ManagedOSPF object via its `capabilities` (queried with include=["capabilities"]).
    A many relationship cannot be capped at 1, so anything other than exactly one peer is unresolved (None)."""
    try:
        peers = bgp.capabilities.peers
    except AttributeError:
        return None
    if len(peers) != 1:
        return None
    return peers[0].display_label


def _safe_as_id(bgp: Any) -> str | None:
    """AS ID from a ManagedBGP object's local_as relationship."""
    return bgp.local_as.id or None


def _bgp_process_ref(bgp_process: dict) -> dict:
    """Reference a planned BGP process by direct id when known, else by HFID.
    A direct id avoids a search-index lookup racing a concurrent write to the same process."""
    process_id = bgp_process.get("id")
    return {"id": process_id} if process_id else {"hfid": bgp_process["name"]}


def _make_bgp_proc(
    name: str,
    suffix: str,
    description: str,
    local_as: dict | PendingASRef,
    router_id: dict,
    device_id: str,
    process_role: str,
    multipath: bool = False,
) -> dict[str, Any]:
    proc: dict[str, Any] = {
        "name": f"{name}-bgp-{suffix}",
        "description": description,
        "status": "active",
        "local_as": local_as,
        "router_id": router_id,
        "capabilities": [{"id": device_id}],
        "process_role": process_role,
    }
    if multipath:
        proc["multipath"] = True
    return proc


class RoutingPlanner:
    """Plans routing configuration from pre-queried SDK objects. Builds a device_map keyed by device name ({"spine-1":
    {"id", "role", "router_id": {"id"}, "loopback_ip"}}) used by all planning methods.
    """

    def __init__(self, deployment_id: str, logger: Any = None, strict: bool = False):
        self.deployment_id = deployment_id
        self.logger = logger
        self.strict = strict

    # Main entry point

    def build_routing_plan(self, inp: RoutingPlanInput) -> RoutingPlan:
        """Build complete routing plan from pre-queried SDK objects."""
        underlay_type, overlay_type = inp.routing_strategy.split("-")
        if inp.options.get("no_overlay"):
            overlay_type = "none"
        plan = RoutingPlan()

        all_device_names = set(inp.bottom_devices + inp.top_devices)
        if not all_device_names:
            if self.logger:
                self.logger.warning("No routing devices provided")
            return plan

        # Known roles for bottom/top devices override the queried role (RoutingPlanInput)
        known_roles: dict[str, str] = {}
        if inp.bottom_role:
            known_roles.update({name: inp.bottom_role for name in inp.bottom_devices})
        if inp.top_role:
            known_roles.update({name: inp.top_role for name in inp.top_devices})
        device_map = self._build_device_map(inp.loopback_interfaces, known_roles=known_roles)

        # Reuse existing AS IDs from the underlay BGP instead of drawing from the pool (pool allocation is not
        # idempotent across create()); processes are always re-saved (local_as upserts cleanly). eBGP underlay only
        existing_as_by_device: dict[str, str] = {}
        if underlay_type == "ebgp":
            for bgp in inp.underlay:
                dev_name = _safe_device_name(bgp)
                as_id = _safe_as_id(bgp)
                if dev_name and as_id:
                    existing_as_by_device[dev_name] = as_id

        design = inp.options.get("design")
        asn_pool = inp.options.get("asn_pool")
        overlay_as_id = inp.options.get("overlay_as_id")
        existing_ospf_area = inp.options.get("ospf_area_id")
        shared_underlay_as_id = inp.options.get("shared_underlay_as_id")

        # Underlay
        if not inp.options.get("skip_underlay"):
            if underlay_type == "ebgp":
                self._plan_ebgp_underlay(
                    plan,
                    device_map,
                    inp.interfaces,
                    existing_as_by_device,
                    asn_pool,
                    set(inp.top_devices),
                    password_id=inp.underlay_password_id,
                    mlag_pairs=inp.mlag_pairs,
                    shared_as_id=shared_underlay_as_id,
                )
            elif underlay_type == "ospf":
                if not existing_ospf_area:
                    raise ValueError(
                        "existing_ospf_area is required for OSPF underlay. "
                        "The DC generator must create the shared OSPF area first."
                    )
                self._plan_ospf_underlay(
                    plan,
                    device_map,
                    inp.interfaces,
                    existing_ospf_area,
                    password_id=inp.underlay_password_id,
                )
            else:
                raise ValueError(f"Unknown underlay: {underlay_type}")

        # Overlay BGP processes
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
                mlag_pairs=inp.mlag_pairs,
            )

        # Overlay peerings
        if design and overlay_type != "none":
            overlay_bgp = [b for b in plan.bgp_processes if b.get("process_role") == "overlay"]
            planned_device_ids = {b["capabilities"][0]["id"] for b in overlay_bgp}

            # Include remote devices with existing overlay BGP not yet in plan, carrying the real id: an HFID-only ref
            # can hit NODE_NOT_FOUND if a concurrent call's write is not indexed yet
            existing_overlay_id_by_name: dict[str, str] = {}
            for obj in inp.overlay:
                dev_name = _safe_device_name(obj)
                if dev_name:
                    existing_overlay_id_by_name[dev_name] = obj.id

            for name, info in device_map.items():
                if info["id"] in planned_device_ids:
                    continue
                overlay_process_id = existing_overlay_id_by_name.get(name)
                if overlay_process_id and info.get("router_id"):
                    overlay_bgp.append(
                        {
                            "id": overlay_process_id,
                            "name": f"{name}-bgp-overlay",
                            "capabilities": [{"id": info["id"]}],
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
                    evpn_af_id=inp.evpn_af_id,
                    password_id=inp.overlay_password_id,
                )

        return plan

    # Device map builder

    @staticmethod
    def _build_device_map(
        loopback_interfaces: list[Any], known_roles: dict[str, str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Device info from loopback interfaces, keyed by device name ({"id", "role", "router_id", "loopback_ip"}).
        Loopbacks must be queried with include=["device", "ip_address"], prefetch_relationships=True. ``known_roles``
        (name -> role) takes priority over the queried role, which can intermittently resolve to None.
        """
        device_map: dict[str, dict[str, Any]] = {}
        known_roles = known_roles or {}

        # Sort by interface id so router_id is deterministic: the lowest-id loopback with a valid IP wins per device
        for lb in sorted(loopback_interfaces, key=lambda lb: lb.id):
            dev = lb.device.peer
            name = dev.name.value
            dev_id = dev.id
            role = known_roles.get(name) or dev.role.value

            if name not in device_map:
                device_map[name] = {"id": dev_id, "role": role}

            if "router_id" not in device_map[name]:
                ip_id = lb.ip_address.id
                if ip_id:
                    device_map[name]["router_id"] = {"id": ip_id}
                    # display_label is "10.0.0.1/32": strip the prefix
                    device_map[name]["loopback_ip"] = str(lb.ip_address.display_label).split("/")[0]
                    device_map[name]["loopback_interface_id"] = lb.id

        return device_map

    # eBGP underlay (interface-driven)

    def _plan_ebgp_underlay(
        self,
        plan: RoutingPlan,
        device_map: dict[str, dict],
        interfaces: list[Any],
        existing_as_by_device: dict[str, str],
        asn_pool: Any,
        top_device_names: set[str] | None = None,
        password_id: str = "",
        mlag_pairs: dict[str, str] | None = None,
        shared_as_id: str | None = None,
    ) -> None:
        """Build the eBGP underlay by iterating cable pairs; top devices already have AS + BGP, so only their peerings
        are created. ASN is per group (an MLAG pair shares one); ``shared_as_id`` bypasses grouping and the pool.
        """
        id_to_name = {info["id"]: name for name, info in device_map.items()}
        _top = top_device_names or set()
        _mlag_pairs = mlag_pairs or {}

        def _group_key(name: str) -> str:
            return _mlag_pairs.get(name, name)

        # An MLAG pair with different existing underlay AS ids converges onto the lowest-sorted one (self-healing)
        existing_as_by_group: dict[str, str] = {}
        for name, as_id in existing_as_by_device.items():
            group = _group_key(name)
            current = existing_as_by_group.get(group)
            if current is None or as_id < current:
                existing_as_by_group[group] = as_id

        bottom_names = [name for name in sorted(device_map.keys()) if name not in _top]

        # Phase 0: one local_as per group, at most one autonomous_systems entry per group
        local_as_by_group: dict[str, dict | PendingASRef] = {}
        if shared_as_id is not None:
            shared_ref = {"id": shared_as_id}
            seen_shared_groups: set[str] = set()
            for name in bottom_names:
                group = _group_key(name)
                local_as_by_group[group] = shared_ref
                # _plan_overlay_processes (eBGP overlay) reads AS refs from plan.autonomous_systems, so a shared
                # underlay AS must be recorded there or the overlay BGP is silently never created
                if group not in seen_shared_groups:
                    seen_shared_groups.add(group)
                    plan.autonomous_systems.append({"_existing_id": shared_as_id, "_for_device": group})
        else:
            for name in bottom_names:
                group = _group_key(name)
                if group in local_as_by_group:
                    continue
                existing_as_id = existing_as_by_group.get(group)
                if existing_as_id:
                    plan.autonomous_systems.append({"_existing_id": existing_as_id, "_for_device": group})
                    local_as_by_group[group] = {"id": existing_as_id}
                elif asn_pool is not None:
                    plan.autonomous_systems.append(
                        {
                            "asn": {"from_pool": {"id": asn_pool}},
                            "description": f"{group} underlay ASN",
                            "status": "active",
                            "_for_device": group,
                        }
                    )
                    local_as_by_group[group] = PendingASRef(device=group)
                elif self.logger:
                    # No pool and no existing AS means no local_as and no BGP process for the group: an error, not a
                    # note
                    self.logger.error(f"No ASN pool for {group} — its device(s) will have no BGP process")

        # Phase 1: BGP process for every bottom device (device_map, not cable-driven)
        bgp_planned: set[str] = set()
        for name in bottom_names:
            if name in bgp_planned:
                continue

            info = device_map[name]
            local_as = local_as_by_group.get(_group_key(name))
            if local_as is None:
                continue

            router_id = self._router_id_or_skip(name, info, "BGP")
            if not router_id:
                continue

            proc = _make_bgp_proc(
                name,
                "underlay",
                f"eBGP process for {name} underlay",
                local_as,
                router_id,
                info["id"],
                process_role="underlay",
                multipath=True,
            )
            plan.bgp_processes.append(proc)
            bgp_planned.add(name)

        # Phase 2: peerings, cable-driven; both ends need BGP
        for a, b, a_name, b_name in self._cable_pairs(interfaces, id_to_name):
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
                    "peering_role": "underlay",
                    "capabilities": [{"id": a.id}, {"id": b.id}],
                    "bgp_processes": [
                        {"hfid": f"{a_name}-bgp-underlay"},
                        {"hfid": f"{b_name}-bgp-underlay"},
                    ],
                    **({"password": {"id": password_id}} if password_id else {}),
                }
            )

    def _router_id_or_skip(self, name: str, info: dict, protocol: str) -> Any:
        """The device's router-id; when missing, raise in strict mode, else warn that ``protocol`` is skipped."""
        router_id = info.get("router_id")
        if not router_id:
            if self.strict:
                raise ValueError(f"No router-id for {name}")
            if self.logger:
                self.logger.warning(f"No router-id for {name}, skipping {protocol}")
        return router_id

    def _cable_pairs(self, interfaces: list[Any], id_to_name: dict[str, str]) -> list[tuple[Any, Any, str, str]]:
        """One (a, b, a_name, b_name) per cabled device pair, a_name < b_name, sorted by names. Drops cables not joining
        two known devices and self-loops (a race between parallel rack generators); parallel cables keep only the first.
        """
        cable_map: dict[str, list] = defaultdict(list)
        for iface in interfaces:
            if iface.cable and iface.cable.id:
                cable_map[iface.cable.id].append(iface)

        cable_pairs: list[tuple[Any, Any, str, str]] = []
        for ifaces in cable_map.values():
            if len(ifaces) != 2:
                continue
            a, b = ifaces
            a_name = id_to_name.get(a.device.id)
            b_name = id_to_name.get(b.device.id)
            if not a_name or not b_name:
                continue
            if a_name == b_name:
                if self.logger:
                    self.logger.warning(
                        f"Self-loop cable detected: both endpoints on '{a_name}' "
                        f"({a.name.value} / {b.name.value}) — skipping"
                    )
                continue
            if a_name > b_name:
                a, b = b, a
                a_name, b_name = b_name, a_name
            cable_pairs.append((a, b, a_name, b_name))

        cable_pairs.sort(key=lambda x: (x[2], x[3]))
        first_by_names: dict[tuple[str, str], tuple[Any, Any, str, str]] = {}
        for pair in cable_pairs:
            first_by_names.setdefault((pair[2], pair[3]), pair)
        return list(first_by_names.values())

    # Overlay BGP processes

    def _plan_overlay_processes(
        self,
        plan: RoutingPlan,
        device_map: dict[str, dict],
        overlay_as_id: str | None,
        top_device_names: set[str] | None = None,
        mlag_pairs: dict[str, str] | None = None,
    ) -> None:
        """Build overlay BGP processes: iBGP (shared ASN) if overlay_as_id is set, else eBGP (per-device ASN). Devices
        in top_device_names are skipped (an upper layer created them; existing_overlay_names picks them up for
        peerings). ``mlag_pairs`` must be the SAME mapping given to ``_plan_ebgp_underlay``: under eBGP overlay local_as
        is keyed by GROUP, and a miss by device name silently leaves a leaf with no overlay BGP.
        """
        is_ibgp = bool(overlay_as_id)
        desc_prefix = "iBGP process for" if is_ibgp else "eBGP process for"
        _top = top_device_names or set()
        _mlag_pairs = mlag_pairs or {}

        group_as_refs: dict[str, dict | PendingASRef] = {}
        if not is_ibgp:
            for as_dict in plan.autonomous_systems:
                group = as_dict["_for_device"]
                if "_existing_id" in as_dict:
                    group_as_refs[group] = {"id": as_dict["_existing_id"]}
                else:
                    # PendingASRef resolves against the group key the underlay allocated under, so keep the group name
                    group_as_refs[group] = PendingASRef(device=group)

        for name in sorted(device_map.keys()):
            if name in _top:
                continue
            info = device_map[name]
            if info.get("role") not in OVERLAY_ROLES:
                continue

            as_ref: dict | PendingASRef
            if is_ibgp:
                as_ref = {"id": overlay_as_id}
            else:
                maybe_as_ref = group_as_refs.get(_mlag_pairs.get(name, name))
                if not maybe_as_ref:
                    continue
                as_ref = maybe_as_ref

            router_id = self._router_id_or_skip(name, info, "BGP") if is_ibgp else info.get("router_id")
            if not router_id:
                continue

            proc = _make_bgp_proc(
                name,
                "overlay",
                f"{desc_prefix} {name} overlay/EVPN",
                as_ref,
                router_id,
                info["id"],
                process_role="overlay",
            )
            plan.bgp_processes.append(proc)

    # OSPF underlay

    def _plan_ospf_underlay(
        self,
        plan: RoutingPlan,
        device_map: dict[str, dict],
        interfaces: list[Any],
        existing_area_id: str,
        password_id: str = "",
    ) -> None:
        """Build the OSPF underlay: one process per device, one RoutingOSPFInterface per device-interface, and ONE
        ManagedOSPFPeering per cable pair referencing both devices' process + interface config.
        """
        area_ref: dict[str, Any] = {"id": existing_area_id}
        id_to_name = {info["id"]: name for name, info in device_map.items()}

        for name in sorted(device_map.keys()):
            info = device_map[name]
            router_id = self._router_id_or_skip(name, info, "OSPF")
            if not router_id:
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
                    "capabilities": [{"id": info["id"]}],
                    "router_id": router_id,
                }
            )

        # Interface configs, one per device-interface
        for iface in interfaces:
            dev_name = id_to_name.get(iface.device.id)
            if not dev_name or not (iface.cable and iface.cable.id):
                continue
            iname = iface.name.value
            ospf_iface_name = f"{dev_name}-{iname}-ospf-underlay"
            plan.ospf_interfaces.append(
                {
                    "name": ospf_iface_name,
                    "description": f"OSPF config for {dev_name}:{iname}",
                    "mode": "peer_to_peer",
                    "capabilities": [{"id": iface.id}],
                    **({"password": {"id": password_id}} if password_id else {}),
                }
            )

        # Peerings, one ManagedOSPFPeering per physical link
        for a, b, a_name, b_name in self._cable_pairs(interfaces, id_to_name):
            ia = a.name.value
            ib = b.name.value
            ia_h = ia.replace("/", "_")
            ib_h = ib.replace("/", "_")

            plan.ospf_peerings.append(
                {
                    "name": f"{a_name}--{ia_h}--{b_name}--{ib_h}-ospf-peering",
                    "description": f"OSPF peering: {a_name} ({ia}) <-> {b_name} ({ib})",
                    "network_type": "point-to-point",
                    "ospf_area": area_ref,
                    "ospf_process": [
                        {"hfid": f"{a_name}-ospf-underlay"},
                        {"hfid": f"{b_name}-ospf-underlay"},
                    ],
                    "ospf_interface": [
                        {"hfid": f"{a_name}-{ia}-ospf-underlay"},
                        {"hfid": f"{b_name}-{ib}-ospf-underlay"},
                    ],
                }
            )

    # Overlay peerings (loopback-based)

    def _plan_overlay_peerings(
        self,
        plan: RoutingPlan,
        overlay_type: str,
        bgp_processes: list[dict],
        device_map: dict[str, dict],
        bottom_device_names: set[str] | None = None,
        top_device_names: set[str] | None = None,
        evpn_af_id: str = "",
        password_id: str = "",
    ) -> None:
        """Build overlay peerings using device loopback IPs from device_map."""
        id_to_name = {info["id"]: name for name, info in device_map.items()}
        device_bgp_map: dict[str, dict] = {}

        for bgp in bgp_processes:
            did = bgp["capabilities"][0]["id"]
            device_bgp_map[did] = bgp

        device_data: list[_BGPDevice] = []
        for bgp in bgp_processes:
            did = bgp["capabilities"][0]["id"]
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

        for d1_name, d1_id, d2_name, d2_id, stype in session_plan:
            if d1_id not in device_bgp_map or d2_id not in device_bgp_map:
                continue

            bgp1, bgp2 = device_bgp_map[d1_id], device_bgp_map[d2_id]
            left_name, right_name = sorted([d1_name, d2_name])

            d1_role = device_map.get(d1_name, {}).get("role", "")
            d2_role = device_map.get(d2_name, {}).get("role", "")

            # An RR-client session needs a genuine client->RR relationship: leaf/border-leaf/tor under spines, spines
            # under super-spines, super-spines under hyper-spines
            rr_client_session = stype == "ibgp" and (
                (d1_role in RR_CLIENT_ROLES and (d2_role in _SPINE_ROLES or d2_role == "super-spine"))
                or ((d1_role in _SPINE_ROLES or d1_role == "super-spine") and d2_role in RR_CLIENT_ROLES)
                or (d1_role in _SPINE_ROLES and d2_role == "super-spine")
                or (d1_role == "super-spine" and d2_role in _SPINE_ROLES)
                or (d1_role == "super-spine" and d2_role == "hyper-spine")
                or (d1_role == "hyper-spine" and d2_role == "super-spine")
            )

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
                "route_reflector_client": rr_client_session,
                "peering_role": "overlay",
                **({"address_families": [{"id": evpn_af_id}]} if evpn_af_id else {}),
                **({"password": {"id": password_id}} if password_id else {}),
                "bgp_processes": [_bgp_process_ref(bgp1), _bgp_process_ref(bgp2)],
            }
            if peering_interfaces:
                peering["capabilities"] = peering_interfaces

            plan.bgp_peerings.append(peering)


# BGP session planner (internal)


class _BGPSessionPlanner:
    """Plans BGP session topology (route reflector, spine-leaf, etc.)."""

    def __init__(self, devices: list[_BGPDevice]):
        self.devices = devices

    def build_session_plan(self, session_type: str) -> list[tuple[str, str, str, str, str]]:
        """Route-reflector session plan, one (dev1_name, dev1_id, dev2_name, dev2_id, session_type) per session: spines,
        super-spines and hyper-spines as RR, leafs/border-leafs/tors/access-leafs as clients (access-leaf peers EVPN
        with spines though it cables to leafs; l2-leaf never appears). With no clients: super-spines, hyper-spines or
        (back-to-back) spines full-mesh among themselves.
        """
        roles = {d.role for d in self.devices}
        rrs = [d for d in self.devices if d.role in RR_ROLES]
        clients = [d for d in self.devices if d.role in RR_CLIENT_ROLES]
        has_super_spine = "super-spine" in roles
        has_hyper_spine = "hyper-spine" in roles
        # Back-to-back: spines (or border-spines) peer as equals
        has_only_spines = roles in ({"spine"}, {"border-spine"})
        if rrs and not clients and (has_super_spine or has_hyper_spine or has_only_spines):
            return [
                (rrs[i].name, rrs[i].id, rrs[j].name, rrs[j].id, session_type)
                for i in range(len(rrs))
                for j in range(i + 1, len(rrs))
            ]
        return [(c.name, c.id, rr.name, rr.id, session_type) for rr in rrs for c in clients]
