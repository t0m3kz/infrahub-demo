"""The routing of a layer pair as plan specs: a pure function of the devices, their links and what exists before the run
(generators/plan.py). It is ``RoutingPlanner`` (generators/helpers/routing.py) for a plan that references objects by
HFID: no write, no read of anything this run creates, no SDK object.

A device is a ``RoutingDevice``: its role, router id (the id of its loopback address, or a ``NodeRef`` to the address the
plan allocates) and loopback port. Processes, peerings and OSPF interfaces are ``NodeSpec`` saved owned; an autonomous
system the run draws from a pool is a sequential node, one it finds is tracked (``TrackSpec``) and referenced by id.
Peerings point at their processes and ports by HFID, so the reconciler writes them after both.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from generators.capabilities.common import Ref, RoutingPlanError, id_ref, pair_ends, password_ref
from generators.capabilities.mlag import mlag_domain_name
from generators.capabilities.ospf import OSPF_KIND, ospf_area_name, plan_ospf_area, plan_ospf_underlay
from generators.dcim.cabling_plan import Link
from generators.dcim.device_plan import LOOPBACK_KIND, DeviceReads, device_hfid
from generators.helpers.pairing import pair_device_names
from generators.helpers.routing import RoutingStrategy, _BGPDevice, _BGPSessionPlanner
from generators.model.errors import GeneratorError
from generators.plan import AddressAllocationSpec, Hfid, NodeRef, NodeSpec, TopologyPlan, TrackSpec
from generators.protocols import DcimVirtualInterface, ManagedBGP, ManagedMLAG
from utils.fabric_roles import OVERLAY_ROLES, RR_CLIENT_ROLES

BGP_KIND = "ManagedBGP"
AS_KIND = "RoutingAutonomousSystem"
_SPINE_ROLES = frozenset({"spine", "border-spine"})


@dataclass(frozen=True)
class RoutingDevice:
    """A device of the layer pair as routing sees it. ``router_id``: the id of its loopback address (``NodeRef`` to the
    allocation of this plan), None when it has none. ``loopback``: its loopback port (None: it has no loopback, so it is
    left out of the routing).
    """

    name: str
    role: str
    loopback: Hfid | None
    router_id: Ref | None


@dataclass(frozen=True)
class RoutingShared:
    """The shared objects and switches of one routing call (``RoutingOptions`` as they are known at plan time): objects
    the plan saves are ``NodeRef``, objects that exist already ids. ``strategy`` is the design's routing strategy.
    """

    strategy: str
    asn_pool: Ref | None = None
    overlay_as: Ref | None = None
    ospf_area: Ref | None = None
    underlay_password: Ref | None = None
    overlay_password: Ref | None = None
    evpn_address_family: Ref | None = None
    shared_underlay_as: Ref | None = None
    skip_underlay: bool = False
    no_overlay: bool = False


@dataclass(frozen=True)
class RoutingReads:
    """What exists before the run and routing needs, by device name: the loopback of each device that has one (its port
    HFID and the id of its address), the local AS of its underlay process, whether it has an overlay process, and the
    MLAG domain of each member of a complete pair.
    """

    loopbacks: Mapping[str, tuple[Hfid, str | None]] = field(default_factory=dict)
    underlay_as: Mapping[str, str] = field(default_factory=dict)
    overlay: frozenset[str] = frozenset()
    mlag_pairs: Mapping[str, str] = field(default_factory=dict)


async def read_routing_reads(client: Any, names: Sequence[str]) -> RoutingReads:
    """Read the loopbacks, BGP processes and MLAG domains of ``names``: three reads, none of what this run creates."""
    if not names:
        return RoutingReads()
    loopbacks, processes, domains = (
        await client.filters(
            kind=DcimVirtualInterface,
            device__name__values=list(names),
            role__value="loopback",
            include=["device", "ip_address"],
            prefetch_relationships=True,
        ),
        await client.filters(
            kind=ManagedBGP,
            capabilities__name__values=list(names),
            include=["local_as", "capabilities"],
            prefetch_relationships=True,
        ),
        await client.filters(
            kind=ManagedMLAG,
            capabilities__name__values=list(names),
            include=["capabilities"],
            prefetch_relationships=True,
        ),
    )
    found: dict[str, tuple[Hfid, str | None]] = {}
    # The lowest id with an address is the device's router id, so the choice is the same on every run
    for loopback in sorted(loopbacks, key=lambda item: item.id):
        device_name = loopback.device.peer.name.value
        address_id = loopback.ip_address.id or None
        if device_name not in found or (found[device_name][1] is None and address_id):
            found[device_name] = (Hfid(LOOPBACK_KIND, (device_name, loopback.name.value)), address_id)
    underlay_as: dict[str, str] = {}
    overlay: set[str] = set()
    for process in processes:
        peers = process.capabilities.peers
        if len(peers) != 1:
            continue
        device_name = peers[0].display_label
        if process.process_role.value == "overlay":
            overlay.add(device_name)
        elif process.local_as.id:
            underlay_as[device_name] = process.local_as.id
    mlag_pairs = {
        member: domain.name.value
        for domain in domains
        if len(members := [peer.display_label for peer in domain.capabilities.peers]) == 2
        for member in members
    }
    return RoutingReads(loopbacks=found, underlay_as=underlay_as, overlay=frozenset(overlay), mlag_pairs=mlag_pairs)


def routing_devices(
    plan: TopologyPlan,
    *,
    reads: RoutingReads,
    device_reads: DeviceReads,
    roles: Mapping[str, str],
) -> dict[str, RoutingDevice]:
    """The devices of ``roles`` (name -> role) that have a loopback: the ones this plan writes (their loopback node and
    address allocation, or the address of an earlier run), else the ones that exist already (``reads``).
    """
    loopbacks = {
        spec.key: spec for spec in plan.specs if isinstance(spec, NodeSpec) and spec.key.startswith("loopback:")
    }
    allocated = {spec.key for spec in plan.specs if isinstance(spec, AddressAllocationSpec)}
    devices: dict[str, RoutingDevice] = {}
    for name, role in roles.items():
        if (spec := loopbacks.get(f"loopback:{name}")) is not None and spec.hfid is not None:
            address = f"loopback-address:{name}"
            router_id: Ref | None = (
                NodeRef(address) if address in allocated else device_reads.loopback_address_ids.get(name)
            )
            devices[name] = RoutingDevice(name, role, spec.hfid, router_id)
        elif name in reads.loopbacks:
            devices[name] = RoutingDevice(name, role, reads.loopbacks[name][0], reads.loopbacks[name][1])
    return devices


def mlag_pairs_of(groups: Collection[Sequence[str]]) -> dict[str, str]:
    """Device -> MLAG domain for the devices of each group, paired two at a time as ``plan_mlag_domains`` names the
    domains (``mlag_domain_name``).
    """
    return {
        member: mlag_domain_name(first, second)
        for names in groups
        for first, second in pair_device_names(list(names))
        for member in (first, second)
    }


def plan_process(
    plan: TopologyPlan,
    *,
    device: Hfid,
    name: str,
    suffix: str,
    kind: str,
    description: str,
    data: Mapping[str, Any],
) -> Hfid:
    """One BGP (``{name}-bgp-{suffix}``) or OSPF (``{name}-ospf-{suffix}``) process of ``device`` (``name`` is its name),
    owned by the run, added once per plan (a second call with the same name is a no-op). Returns its HFID; the node is
    ``NodeRef("bgp:{process name}")`` (``"ospf:..."``).
    """
    process_name = f"{name}-{kind}-{suffix}" if kind == "bgp" else f"{name}-ospf-{suffix}"
    hfid = Hfid(BGP_KIND if kind == "bgp" else OSPF_KIND, (process_name,))
    key = f"{'bgp' if kind == 'bgp' else 'ospf'}:{process_name}"
    if not any(isinstance(other, NodeSpec) and other.key == key for other in plan.specs):
        plan.add(
            NodeSpec(
                key=key,
                kind=hfid.kind,
                data={
                    "name": process_name,
                    "description": description,
                    "status": "active",
                    "capabilities": [device],
                    **data,
                },
                ownership="owned",
                hfid=hfid,
            )
        )
    return hfid


@dataclass
class _Builder:
    plan: TopologyPlan
    shared: RoutingShared
    devices: Mapping[str, RoutingDevice]
    logger: Any
    underlay_planned: set[str] = field(default_factory=set)
    group_as: dict[str, Ref] = field(default_factory=dict)
    overlay_planned: set[str] = field(default_factory=set)

    def add_once(self, spec: NodeSpec) -> None:
        """Add ``spec`` unless the plan holds one of its key already (an earlier call of the same plan wrote the same
        process, interface or peering).
        """
        if not any(isinstance(other, NodeSpec) and other.key == spec.key for other in self.plan.specs):
            self.plan.add(spec)

    def planned(self, key: str) -> NodeSpec | None:
        """The node of this plan saved under ``key``, if any."""
        return next((spec for spec in self.plan.specs if isinstance(spec, NodeSpec) and spec.key == key), None)

    def process(self, name: str, suffix: str, kind: str, description: str, data: dict[str, Any]) -> Hfid:
        """Add one BGP or OSPF process of a device and return its HFID."""
        return plan_process(
            self.plan,
            device=device_hfid(name),
            name=name,
            suffix=suffix,
            kind=kind,
            description=description,
            data=data,
        )

    def router_id(self, device: RoutingDevice, protocol: str, *, strict: bool = True) -> Ref | None:
        if device.router_id is None:
            if strict:
                raise RoutingPlanError(f"No router-id for {device.name}")
            self.logger.warning(f"No router-id for {device.name}, skipping {protocol}")
        return device.router_id


def plan_routing(
    plan: TopologyPlan,
    *,
    shared: RoutingShared,
    devices: Mapping[str, RoutingDevice],
    bottom_devices: Sequence[str],
    top_devices: Sequence[str],
    links: Sequence[Link],
    reads: RoutingReads,
    mlag_pairs: Mapping[str, str],
    logger: Any,
) -> None:
    """Add the routing of one layer pair to ``plan``: the underlay (eBGP or OSPF; processes of the bottom devices, the
    peering or OSPF interface of every link), the overlay processes of the bottom devices and the overlay peerings
    between the layers. ``links`` are the cables of the pair (none for a process-only call). Raises ``RoutingPlanError``
    for a missing shared object or router id.
    """
    if shared.strategy not in {strategy.value for strategy in RoutingStrategy}:
        logger.warning(f"Routing strategy '{shared.strategy}' not supported")
        return
    if shared.no_overlay and shared.skip_underlay:
        return
    names = set(bottom_devices) | set(top_devices)
    if not names:
        logger.warning("No routing devices provided")
        return
    underlay_type, overlay_type = shared.strategy.split("-")
    if shared.no_overlay:
        overlay_type = "none"
    device_map = {name: devices[name] for name in sorted(names) if name in devices}
    builder = _Builder(plan=plan, shared=shared, devices=device_map, logger=logger)
    top = set(top_devices)
    pairs = _cable_pairs(links, device_map, logger)
    if not shared.skip_underlay:
        if underlay_type == "ebgp":
            _ebgp_underlay(builder, pairs, reads, top, mlag_pairs)
        elif underlay_type == "ospf":
            plan_ospf_underlay(builder, pairs, links)
    if overlay_type == "ibgp" and shared.overlay_as is None:
        raise RoutingPlanError("The shared overlay AS is not found: the DC generator must create it first")
    if overlay_type in ("ebgp", "ibgp"):
        _overlay_processes(builder, shared.overlay_as if overlay_type == "ibgp" else None, top, mlag_pairs)
    if overlay_type != "none":
        _overlay_peerings(builder, overlay_type, reads, set(bottom_devices), top)


def _cable_pairs(
    links: Sequence[Link], device_map: Mapping[str, RoutingDevice], logger: Any
) -> list[tuple[Link, str, str, bool]]:
    """One ``(link, a_name, b_name, swapped)`` per cabled device pair, ``a_name < b_name``, sorted by names: cables that do
    not join two known devices and self-loops are dropped, parallel cables keep the first (``_cable_pairs`` of the
    routing planner).
    """
    pairs: list[tuple[Link, str, str, bool]] = []
    for link in links:
        a_name, b_name = link.bottom.device_name, link.top.device_name
        if a_name not in device_map or b_name not in device_map:
            continue
        if a_name == b_name:
            logger.warning(
                f"Self-loop cable detected: both endpoints on '{a_name}' "
                f"({link.bottom.port_name} / {link.top.port_name}) — skipping"
            )
            continue
        swapped = a_name > b_name
        pairs.append((link, *((b_name, a_name) if swapped else (a_name, b_name)), swapped))
    pairs.sort(key=lambda pair: (pair[1], pair[2]))
    first: dict[tuple[str, str], tuple[Link, str, str, bool]] = {}
    for pair in pairs:
        first.setdefault((pair[1], pair[2]), pair)
    return list(first.values())


def _ebgp_underlay(
    builder: _Builder,
    pairs: list[tuple[Link, str, str, bool]],
    reads: RoutingReads,
    top: set[str],
    mlag_pairs: Mapping[str, str],
) -> None:
    """eBGP underlay: one local AS per group (an MLAG pair shares one; a shared AS covers every bottom device), a
    process per bottom device, a peering per cable with both ends on a process (a top device already has its own).
    """
    shared = builder.shared

    def group_of(name: str) -> str:
        return mlag_pairs.get(name, name)

    # An MLAG pair with different existing underlay AS ids converges onto the lowest one
    existing: dict[str, str] = {}
    for name, as_id in reads.underlay_as.items():
        group = group_of(name)
        if group not in existing or as_id < existing[group]:
            existing[group] = as_id
    bottom = [name for name in builder.devices if name not in top]
    group_as: dict[str, Ref] = {}
    tracked: list[str] = []
    # A device an earlier call of this plan gave its process keeps that AS (the process is not written twice)
    for name in bottom:
        if (earlier := builder.planned(f"bgp:{name}-bgp-underlay")) is not None:
            builder.underlay_planned.add(name)
            group_as.setdefault(group_of(name), _as_of(earlier))
    if shared.shared_underlay_as is not None:
        for name in bottom:
            group_as[group_of(name)] = shared.shared_underlay_as
        if isinstance(shared.shared_underlay_as, str):
            tracked.append(shared.shared_underlay_as)
    else:
        for name in bottom:
            group = group_of(name)
            if group in group_as:
                continue
            if as_id := existing.get(group):
                group_as[group] = as_id
                tracked.append(as_id)
            elif shared.asn_pool is not None:
                key = f"as:{group}"
                builder.plan.add(
                    NodeSpec(
                        key=key,
                        kind=AS_KIND,
                        data={
                            "asn": {"from_pool": {"id": shared.asn_pool}},
                            "description": f"{group} underlay ASN",
                        },
                        ownership="owned",
                        sequential=True,
                    )
                )
                group_as[group] = NodeRef(key)
            else:
                builder.logger.error(f"No ASN pool for {group} — its device(s) will have no BGP process")
    if tracked:
        builder.plan.add(TrackSpec(tuple(dict.fromkeys(tracked))))
    builder.group_as = group_as
    for name in bottom:
        local_as = group_as.get(group_of(name))
        if local_as is None or builder.planned(f"bgp:{name}-bgp-underlay") is not None:
            continue
        router_id = builder.router_id(builder.devices[name], "BGP")
        builder.process(
            name,
            "underlay",
            "bgp",
            f"eBGP process for {name} underlay",
            {
                "local_as": id_ref(local_as),
                "router_id": id_ref(router_id),
                "process_role": "underlay",
                "multipath": True,
            },
        )
        builder.underlay_planned.add(name)
    for pair in pairs:
        _, a_name, b_name, _ = pair
        if (a_name not in builder.underlay_planned and a_name not in top) or (
            b_name not in builder.underlay_planned and b_name not in top
        ):
            continue
        a, b = pair_ends(pair)
        a_label, b_label = a.port_name.replace("/", "_"), b.port_name.replace("/", "_")
        name = f"underlay--{a_name}--{a_label}--{b_name}--{b_label}"
        builder.add_once(
            NodeSpec(
                key=f"bgp-peering:{name}",
                kind="ManagedBGPPeering",
                data={
                    "name": name,
                    "description": f"eBGP underlay: {a_name} ({a.port_name}) <-> {b_name} ({b.port_name})",
                    "session_type": "EBGP",
                    "bfd_enabled": True,
                    "send_community": True,
                    "ttl": 1,
                    "peering_role": "underlay",
                    "capabilities": [a.hfid, b.hfid],
                    "bgp_processes": [
                        Hfid(BGP_KIND, (f"{a_name}-bgp-underlay",)),
                        Hfid(BGP_KIND, (f"{b_name}-bgp-underlay",)),
                    ],
                    **password_ref(shared.underlay_password),
                },
                ownership="owned",
                hfid=Hfid("ManagedBGPPeering", (name,)),
                retry_on_conflict=True,
            )
        )


def _overlay_processes(builder: _Builder, overlay_as: Ref | None, top: set[str], mlag_pairs: Mapping[str, str]) -> None:
    """Overlay BGP processes of the bottom devices that run one: iBGP on the shared overlay AS, eBGP on the group's
    underlay AS (a device with no group AS gets none).
    """
    description = "iBGP process for" if overlay_as is not None else "eBGP process for"
    for name, device in builder.devices.items():
        if name in top or device.role not in OVERLAY_ROLES:
            continue
        if overlay_as is not None:
            local_as: Ref | None = overlay_as
            router_id = builder.router_id(device, "BGP")
        else:
            local_as = builder.group_as.get(mlag_pairs.get(name, name))
            router_id = device.router_id
        if local_as is None or router_id is None:
            continue
        builder.overlay_planned.add(name)
        if builder.planned(f"bgp:{name}-bgp-overlay") is not None:
            continue
        builder.process(
            name,
            "overlay",
            "bgp",
            f"{description} {name} overlay/EVPN",
            {"local_as": id_ref(local_as), "router_id": id_ref(router_id), "process_role": "overlay"},
        )


def _overlay_peerings(
    builder: _Builder, overlay_type: str, reads: RoutingReads, bottom: set[str], top: set[str]
) -> None:
    """The EVPN peerings between the overlay processes of the layers: the new ones and those of devices that already
    have one. Route-reflector roles come from ``_BGPSessionPlanner``.
    """
    shared = builder.shared
    planned = [name for name in builder.devices if name in builder.overlay_planned]
    # A device whose overlay process an earlier call of this plan wrote counts like one that already exists
    others = [
        name
        for name, device in builder.devices.items()
        if name not in builder.overlay_planned
        and device.router_id is not None
        and (name in reads.overlay or builder.planned(f"bgp:{name}-bgp-overlay") is not None)
    ]
    with_process = planned + others
    members = [_BGPDevice(name=name, id=name, role=builder.devices[name].role) for name in with_process]
    members = [member for member in members if _loopback_ready(builder, member.name)]
    if not members:
        return
    sessions = _BGPSessionPlanner(devices=members).build_session_plan(session_type=overlay_type)
    if bottom and top:
        sessions = [s for s in sessions if (s[0] in bottom and s[2] in top) or (s[0] in top and s[2] in bottom)]
    else:
        scoped = bottom | top
        sessions = [s for s in sessions if s[0] in scoped and s[2] in scoped]
    for d1_name, _, d2_name, _, session_type in sessions:
        left, right = sorted([d1_name, d2_name])
        roles = (builder.devices[d1_name].role, builder.devices[d2_name].role)
        name = f"overlay-evpn--{left}--{right}"
        data: dict[str, Any] = {
            "name": name,
            "description": f"{session_type.upper()} EVPN overlay: {d1_name} <-> {d2_name}",
            "session_type": "EBGP_MULTIHOP" if session_type == "ebgp" else "IBGP",
            "ttl": 2 if session_type == "ebgp" else 255,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": True,
            "route_reflector_client": _rr_client_session(session_type, *roles),
            "peering_role": "overlay",
            **({"address_families": [id_ref(shared.evpn_address_family)]} if shared.evpn_address_family else {}),
            **password_ref(shared.overlay_password),
            "bgp_processes": [Hfid(BGP_KIND, (f"{d1_name}-bgp-overlay",)), Hfid(BGP_KIND, (f"{d2_name}-bgp-overlay",))],
        }
        loopbacks = [loopback for dname in (d1_name, d2_name) if (loopback := builder.devices[dname].loopback)]
        if loopbacks:
            data["capabilities"] = loopbacks
        builder.add_once(
            NodeSpec(
                key=f"bgp-peering:{name}",
                kind="ManagedBGPPeering",
                data=data,
                ownership="owned",
                hfid=Hfid("ManagedBGPPeering", (name,)),
                retry_on_conflict=True,
            )
        )


def _loopback_ready(builder: _Builder, name: str) -> bool:
    """Whether the device has a loopback address to peer from; strict: a device without is an error."""
    if builder.devices[name].router_id is None:
        raise RoutingPlanError(f"No loopback IP for {name}")
    return True


def _rr_client_session(session_type: str, d1_role: str, d2_role: str) -> bool:
    """An RR-client session needs a genuine client->RR relationship (see ``RoutingPlanner._plan_overlay_peerings``)."""
    return session_type == "ibgp" and (
        (d1_role in RR_CLIENT_ROLES and (d2_role in _SPINE_ROLES or d2_role == "super-spine"))
        or ((d1_role in _SPINE_ROLES or d1_role == "super-spine") and d2_role in RR_CLIENT_ROLES)
        or (d1_role in _SPINE_ROLES and d2_role == "super-spine")
        or (d1_role == "super-spine" and d2_role in _SPINE_ROLES)
        or (d1_role == "super-spine" and d2_role == "hyper-spine")
        or (d1_role == "hyper-spine" and d2_role == "super-spine")
    )


def _as_of(process: NodeSpec) -> Ref:
    """The AS a planned BGP process points at: its id, or the ``NodeRef`` of the AS of this plan."""
    return process.data["local_as"]["id"]


async def bgp_processes_ready(client: Any, device_names: Sequence[str], process_role: str) -> bool:
    """Whether every named device already has its own ``ManagedBGP`` process of this role (another generator's output,
    waited for before a layer is planned against it).
    """
    processes = await client.filters(
        kind=ManagedBGP,
        capabilities__name__values=list(device_names),
        include=["capabilities"],
        prefetch_relationships=True,
    )
    ready = {
        process.capabilities.peers[0].display_label
        for process in processes
        if process.process_role.value == process_role and len(process.capabilities.peers) == 1
    }
    return set(device_names).issubset(ready)


PASSWORD_KIND = "RoutingPassword"
PEERING_KIND = "ManagedBGPPeering"
ADDRESS_FAMILY_KIND = "RoutingBGPAddressFamily"
EVPN_AF = ("l2vpn", "evpn")
IPV4_UNICAST_AF = ("ipv4", "unicast")
IPV6_UNICAST_AF = ("ipv6", "unicast")
# Defaults for a missing address family; same values as the bootstrap data.
ADDRESS_FAMILY_DATA: dict[tuple[str, str], dict[str, Any]] = {
    IPV4_UNICAST_AF: {"description": "IPv4 unicast"},
    IPV6_UNICAST_AF: {"description": "IPv6 unicast"},
    EVPN_AF: {"description": "L2VPN EVPN overlay", "advertise_all_vni": True, "advertise_default_gw": True},
}


def underlay_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-underlay-key"


def overlay_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-overlay-key"


def super_spine_as_description(fabric_name: str) -> str:
    return f"{fabric_name} super-spine underlay ASN"


def lisp_site_key_name(fabric_name: str) -> str:
    return f"{fabric_name}-lisp-site-key"


def overlay_as_description(fabric_name: str) -> str:
    return f"{fabric_name} overlay ASN for iBGP EVPN"


@dataclass(frozen=True)
class SharedFacts:
    """The shared objects that exist before the run, by id (None: not there)."""

    overlay_as_id: str | None = None
    ospf_area_id: str | None = None
    underlay_password_id: str | None = None
    overlay_password_id: str | None = None
    super_spine_as_id: str | None = None
    evpn_address_family_id: str | None = None


async def read_shared_facts(client: Any, fabric_name: str) -> SharedFacts:
    """Read the fabric's shared routing objects: the ASes by description, the area and the keys by name."""
    from generators.protocols import RoutingAutonomousSystem, RoutingBGPAddressFamily, RoutingOSPFArea, RoutingPassword

    descriptions = [overlay_as_description(fabric_name), super_spine_as_description(fabric_name)]
    ases = await client.filters(kind=RoutingAutonomousSystem, description__values=descriptions)
    areas = await client.filters(kind=RoutingOSPFArea, name__value=ospf_area_name(fabric_name))
    keys = await client.filters(
        kind=RoutingPassword, name__values=[underlay_key_name(fabric_name), overlay_key_name(fabric_name)]
    )
    families = await client.filters(kind=RoutingBGPAddressFamily, afi__value=EVPN_AF[0], safi__value=EVPN_AF[1])
    as_by_description = {node.description.value: node.id for node in ases}
    key_by_name = {node.name.value: node.id for node in keys}
    return SharedFacts(
        overlay_as_id=as_by_description.get(descriptions[0]),
        super_spine_as_id=as_by_description.get(descriptions[1]),
        ospf_area_id=areas[0].id if areas else None,
        underlay_password_id=key_by_name.get(underlay_key_name(fabric_name)),
        overlay_password_id=key_by_name.get(overlay_key_name(fabric_name)),
        evpn_address_family_id=families[0].id if families else None,
    )


def evpn_address_family(existing_id: str | None) -> str:
    """The id of the EVPN address family, which the bootstrap data provides (data/bootstrap/15_bgp_address_families.yml:
    one created by a branch collides with every other such branch on merge). A fabric without it cannot be routed.
    """
    if existing_id is None:
        raise GeneratorError(
            f"BGP address family {'/'.join(EVPN_AF)} not found — load data/bootstrap/15_bgp_address_families.yml"
        )
    return existing_id


@dataclass(frozen=True)
class SharedRoutingRefs:
    """What ``plan_shared_routing`` plans: the objects the plan saves, to point other specs at. The super-spine AS and the
    OSPF area are None when the strategy has none (or, for the AS, no pool and no existing one).
    """

    underlay_password: NodeRef
    overlay_password: NodeRef
    overlay_as: NodeRef
    super_spine_as: NodeRef | str | None
    ospf_area: NodeRef | None


def plan_password(
    plan: TopologyPlan,
    *,
    name: str,
    description: str,
    track: bool = True,
    new_secret: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> NodeRef:
    """A shared ``RoutingPassword``: its secret is ``create_only``, so the reconciler sends it only when the key does not
    exist (it would rotate otherwise). ``track=False``: a run owning a key would delete it in cleanup.
    """
    key = f"password:{name}"
    plan.add(
        NodeSpec(
            key=key,
            kind=PASSWORD_KIND,
            data={"name": name, "description": description},
            create_only={"password": new_secret()},
            hfid=Hfid(PASSWORD_KIND, (name,)),
            ownership="owned" if track else "shared",
        )
    )
    return NodeRef(key)


def plan_shared_as(
    plan: TopologyPlan,
    *,
    description: str,
    asn_pool: NodeRef | str | None,
    existing_id: str | None,
    key: str,
    track: bool = True,
) -> NodeRef | str | None:
    """The one AS a tier shares: the id of the one that exists, else a draw from ``asn_pool``. None when there is neither
    (the caller warns).
    """
    if existing_id is not None:
        if track:
            plan.add(TrackSpec((existing_id,)))
        return existing_id
    if asn_pool is None:
        return None
    plan.add(
        NodeSpec(
            key=key,
            kind=AS_KIND,
            data={"description": description},
            create_only={"asn": {"from_pool": {"id": asn_pool}}},
            ownership="owned" if track else "shared",
            sequential=True,
        )
    )
    return NodeRef(key)


def plan_shared_routing(
    plan: TopologyPlan,
    *,
    fabric_name: str,
    strategy: str,
    overlay_asn: int,
    asn_pool: NodeRef | str | None,
    facts: SharedFacts,
    logger: Any,
) -> SharedRoutingRefs:
    """The DC's shared routing state, once per DC: both auth keys, the super-spine underlay AS (any eBGP underlay), the
    fabric overlay AS pinned to ``overlay_asn`` (every strategy: EVPN route targets need one fabric-constant ASN) and
    the OSPF area (ospf-ibgp). add_dc and dc_pod_cascade own and track it.
    """
    underlay = plan_password(
        plan,
        name=underlay_key_name(fabric_name),
        description=f"Shared eBGP/OSPF underlay auth key for {fabric_name}",
    )
    overlay = plan_password(
        plan,
        name=overlay_key_name(fabric_name),
        description=f"Shared BGP overlay/EVPN auth key for {fabric_name}",
    )
    super_spine_as: NodeRef | str | None = None
    if strategy.split("-")[0] == "ebgp":
        super_spine_as = plan_shared_as(
            plan,
            description=super_spine_as_description(fabric_name),
            asn_pool=asn_pool,
            existing_id=facts.super_spine_as_id,
            key="shared-as:super-spine",
        )
        if super_spine_as is None:
            logger.warning("No ASN pool available — cannot allocate shared super-spine AS")
    description = overlay_as_description(fabric_name)
    plan.add(
        NodeSpec(
            key="shared-as:overlay",
            kind=AS_KIND,
            data={
                "asn": overlay_asn,
                "description": description,
                **({"id": facts.overlay_as_id} if facts.overlay_as_id else {}),
            },
            ownership="owned",
        )
    )
    area: NodeRef | None = plan_ospf_area(plan, fabric_name) if strategy == RoutingStrategy.OSPF_IBGP.value else None
    return SharedRoutingRefs(
        underlay_password=underlay,
        overlay_password=overlay,
        overlay_as=NodeRef("shared-as:overlay"),
        super_spine_as=super_spine_as,
        ospf_area=area,
    )


def dci_key_name(circuit_name: str) -> str:
    """The per-circuit key of a DCI, not a fabric-wide one."""
    return f"{circuit_name}-dci-key"


async def read_password_id(client: Any, name: str) -> str | None:
    """The id of the ``RoutingPassword`` called ``name``, None when there is none."""
    found = await client.filters(kind=PASSWORD_KIND, name__values=[name])
    return found[0].id if found else None


def address_family_lock_key(afi: str, safi: str) -> str:
    """The lock that serializes the creation of an address family nobody has created yet."""
    return f"bgp-af-{afi}-{safi}"


async def read_address_families(client: Any, families: Collection[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """The ids of the address families (afi, safi) that exist among ``families``, in one read."""
    wanted = set(families)
    if not wanted:
        return {}
    found = await client.filters(
        kind=ADDRESS_FAMILY_KIND,
        afi__values=sorted({afi for afi, _ in wanted}),
        safi__values=sorted({safi for _, safi in wanted}),
    )
    ids: dict[tuple[str, str], str] = {}
    for node in found:
        pair = (node.afi.value, node.safi.value)
        if pair in wanted:
            ids.setdefault(pair, node.id)
    return ids


def plan_address_family(plan: TopologyPlan, family: tuple[str, str], existing: Mapping[tuple[str, str], str]) -> Ref:
    """The address family: its id when it exists, else the node the plan creates (shared, untracked: global). Bootstrap
    data normally provides them; a caller that may create one holds ``address_family_lock_key`` around its read and the
    apply.
    """
    if (existing_id := existing.get(family)) is not None:
        return existing_id
    key = f"address-family:{family[0]}/{family[1]}"
    if not any(isinstance(spec, NodeSpec) and spec.key == key for spec in plan.specs):
        plan.add(
            NodeSpec(
                key=key,
                kind=ADDRESS_FAMILY_KIND,
                data={"afi": family[0], "safi": family[1], **ADDRESS_FAMILY_DATA.get(family, {})},
                ownership="shared",
            )
        )
    return NodeRef(key)


def plan_session_peering(
    plan: TopologyPlan,
    *,
    name: str,
    description: str,
    status: str,
    session_type: str,
    peering_role: str,
    process_ids: Sequence[Ref],
    address_families: Sequence[Ref],
    interface_ids: Sequence[Ref],
    password: Ref | None = None,
    ttl: int = 1,
    bfd_enabled: bool = True,
    send_community: bool = True,
    send_extended_community: bool = True,
) -> NodeRef:
    """One ``ManagedBGPPeering`` of a session between objects that exist (processes and interfaces by id), owned by the
    run; directly connected by default (ttl 1, BFD, both community flags). No ``password`` means no password.
    """
    key = f"bgp-peering:{name}"
    plan.add(
        NodeSpec(
            key=key,
            kind=PEERING_KIND,
            data={
                "name": name,
                "description": description,
                "status": status,
                "session_type": session_type,
                "peering_role": peering_role,
                "ttl": ttl,
                "bfd_enabled": bfd_enabled,
                "send_community": send_community,
                "send_extended_community": send_extended_community,
                "address_families": [id_ref(family) for family in address_families],
                "bgp_processes": [{"id": process_id} for process_id in process_ids],
                **password_ref(password),
                "capabilities": [{"id": interface_id} for interface_id in interface_ids],
            },
            ownership="owned",
            hfid=Hfid(PEERING_KIND, (name,)),
            retry_on_conflict=True,
        )
    )
    return NodeRef(key)
