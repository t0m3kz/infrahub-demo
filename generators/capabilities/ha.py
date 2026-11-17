"""HA pairs (a firewall or load-balancer domain: its group id, its sync ports and cable) and the HA-paired service devices
of a DC or pod (firewalls, load balancers, the Internet-edge pair, cabled to the border tier), as plan builders: pure
functions of the cleaned query data and what already exists (``HaReads``), with no write (generators/plan.py).

The pairing is two devices at a time, sorted, an odd one left unpaired (``pair_device_names``). A pair's domain is
``{first}-{second}``. The sync port of a device is its first port of role ``ha``; the cable between the two exists only
when neither port is cabled to anything else. Physical pairs are ``plan_ha_pairs``, provider-hosted appliance pairs
``plan_appliance_ha_pairs`` (no sync port, no cable) and the virtual pair of a dedicated customer context
``plan_virtual_ha_pair`` (the same domain over two hosted instances, no sync cable).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from generators.dcim.cabling_plan import (
    ChainPort,
    ChainPorts,
    border_service_chains,
    chain_ports_of,
    plan_chain_legs,
    read_chain_ports,
)
from generators.dcim.controllers import resolve_role_controller
from generators.dcim.device_plan import (
    DEVICE_KIND,
    HA_KIND_BY_ROLE,
    VIRTUAL_DEVICE_KIND,
    DeviceOptions,
    DeviceReads,
    device_hfid,
    plan_physical_devices,
    planned_device_names,
    port_hfid,
)
from generators.dcim.rack_layout import read_device_positions
from generators.dcim.template_interfaces import TemplatePort, TemplateReads
from generators.helpers.cable_attributes import RackPosition, cable_attributes, endpoint_label
from generators.helpers.naming import NamingConvention
from generators.helpers.pairing import pair_device_names
from generators.ipam.pools import plan_number_pool
from generators.model.pod_profile import templates_by_role
from generators.plan import Hfid, NodeRef, NodeSpec, TopologyPlan, TrackSpec
from generators.protocols import (
    DcimCable,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ManagedFirewallContext,
    ManagedFirewallHA,
    ManagedLoadbalancerContext,
    ManagedLoadbalancerHA,
)
from utils.exchange_transit import (
    BORDER_ROLE_FOR_SERVICES,
    EXCHANGE_PEERS,
    INTERNET_TYPE,
    lb_leg_pool_name,
    lb_vip_pool_name,
    transit_pool_name,
)
from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE, INTERNAL_FIREWALL_ROLE
from utils.limits import (
    FIREWALL_HA_GROUP_MAX,
    FIREWALL_HA_GROUP_MIN,
    FIREWALL_HA_GROUP_POOL,
    FW_CONTEXT_VLAN_END,
    FW_CONTEXT_VLAN_START,
    GROUP_HA_DOMAINS,
    LB_CONTEXT_VLAN_END,
    LB_CONTEXT_VLAN_START,
)

# The generator definitions that write a DC's own data; consumers (pod, customer boarding) wait them out.
DC_GENERATORS = ("add_dc", "dc_pod_cascade")

POOL_KIND = "CoreNumberPool"
GROUP_KIND = "CoreStandardGroup"
CABLE_KIND = "DcimCable"
HA_POOL_LOCK = "firewall-ha-group-pool"
SYNC_ROLE = "ha"


def sync_cable_name(ha_name: str) -> str:
    return f"CBL-{ha_name}-SYNC"


@dataclass(frozen=True)
class HaDomain:
    """An HA domain that exists: its id, and its group id (None: an older pair that was never given one)."""

    id: str
    group_id: int | None = None


@dataclass(frozen=True)
class HaReads:
    """What exists before the run, by name: the domains, the sync port of each device that has ports already (its first
    port of role ``ha``), the devices whose sync port is cabled, the sync cables, and the group id pool (None: not loaded).
    """

    domains: Mapping[str, HaDomain] = field(default_factory=dict)
    sync_ports: Mapping[str, str] = field(default_factory=dict)
    cabled_sync: frozenset[str] = frozenset()
    sync_cables: Mapping[str, str] = field(default_factory=dict)
    group_pool_id: str | None = None


def ha_domain_names(names: Sequence[str]) -> list[str]:
    """The names of the domains ``names`` pair into."""
    return [f"{first}-{second}" for first, second in pair_device_names(list(names))]


def ha_lock_keys(names: Sequence[str], *, firewall: bool) -> list[str]:
    """The locks a plan with the pairs of ``names`` holds around its reads and its apply: a domain is found or created by
    name, and the group id comes from one pool.
    """
    keys = [f"ha-domain-{name}" for name in ha_domain_names(names)]
    return [*keys, HA_POOL_LOCK] if firewall and keys else keys


async def read_ha_reads(
    client: Any, *, domains: Mapping[str, str], device_names: Sequence[str], firewall: bool
) -> HaReads:
    """Read the HA domains ``domains`` (name -> kind), the sync ports of the devices that exist and the sync cables.
    ``firewall``: the group id pool is read too.
    """
    found: dict[str, HaDomain] = {}
    for kind in sorted(set(domains.values())):
        wanted = sorted(name for name, domain_kind in domains.items() if domain_kind == kind)
        for node in await client.filters(kind=kind, name__values=wanted):
            group = getattr(getattr(node, "group_id", None), "value", None)
            found[node.name.value] = HaDomain(id=node.id, group_id=group)
    ports = (
        await client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=sorted(set(device_names)),
            role__value=SYNC_ROLE,
            include=["device", "cable"],
        )
        if device_names
        else []
    )
    sync_ports: dict[str, str] = {}
    cabled: set[str] = set()
    for port in sorted(ports, key=lambda p: p.name.value):
        device = port.device.peer.name.value
        if device in sync_ports:
            continue
        sync_ports[device] = port.name.value
        if getattr(port, "cable", None) is not None and port.cable.initialized:
            cabled.add(device)
    cables = (
        await client.filters(kind=DcimCable, name__values=[sync_cable_name(name) for name in domains])
        if domains
        else []
    )
    pool_id = await _group_pool_id(client) if firewall else None
    return HaReads(
        domains=found,
        sync_ports=sync_ports,
        cabled_sync=frozenset(cabled),
        sync_cables={cable.name.value: cable.id for cable in cables},
        group_pool_id=pool_id,
    )


def plan_ha_pairs(
    plan: TopologyPlan,
    *,
    names: Sequence[str],
    role: str,
    reads: HaReads,
    template_ports: Mapping[str, Sequence[TemplatePort]],
    positions: Mapping[str, RackPosition | None],
    deployment_id: str,
    problems: list[str],
) -> list[str]:
    """Pair the devices ``names`` of ``role`` (planned in ``plan``) into HA domains and return the domain names. Per pair:
    the domain (found: kept in the run's group, and given a group id when a firewall pair has none; new: both members,
    the ``ha_domains`` group and, for a firewall pair, a group id drawn from the pool), the sync port of each member
    (active; a template port, so untracked) and the cable between them. A member without a sync port is a problem the
    caller raises before anything is written.
    """
    ha_kind = HA_KIND_BY_ROLE[role]
    firewall = ha_kind == ManagedFirewallHA.__name__
    group_pool: str | NodeRef | None = reads.group_pool_id
    if firewall and group_pool is None and any(name not in reads.domains for name in ha_domain_names(names)):
        group_pool = _plan_group_pool(plan)
    domains: list[str] = []
    for first, second in pair_device_names(list(names)):
        name = f"{first}-{second}"
        domains.append(name)
        _plan_domain(
            plan,
            name=name,
            members=(first, second),
            member_kind=DEVICE_KIND,
            ha_kind=ha_kind,
            reads=reads,
            group_pool=group_pool,
        )
        _plan_sync(
            plan,
            name=name,
            members=(first, second),
            reads=reads,
            template_ports=template_ports,
            positions=positions,
            deployment_id=deployment_id,
            problems=problems,
        )
    return domains


def _plan_domain(
    plan: TopologyPlan,
    *,
    name: str,
    members: tuple[str, str],
    member_kind: str,
    ha_kind: str,
    reads: HaReads,
    group_pool: str | NodeRef | None,
) -> None:
    """The HA domain ``name`` of ``members`` (devices of ``member_kind``): found, it stays in the run's group and a firewall
    domain without a group id is given one; new, it holds both members and the ``ha_domains`` group and a firewall domain
    draws its group id from ``group_pool``.
    """
    firewall = ha_kind == ManagedFirewallHA.__name__
    hfid = Hfid(ha_kind, (name,))
    existing = reads.domains.get(name)
    group_data: dict[str, Any] = {"group_id": {"from_pool": {"id": group_pool}}} if firewall else {}
    if existing is None:
        plan.add(
            NodeSpec(
                key=f"ha:{name}",
                kind=ha_kind,
                data={
                    "name": name,
                    "status": "active",
                    "capabilities": [device_hfid(member, member_kind) for member in members],
                    "member_of_groups": [Hfid(GROUP_KIND, (GROUP_HA_DOMAINS,))],
                },
                create_only=group_data,
                hfid=hfid,
                ownership="owned",
                sequential=firewall,
            )
        )
    elif firewall and existing.group_id is None:
        plan.add(
            NodeSpec(
                key=f"ha:{name}",
                kind=ha_kind,
                data={"id": existing.id, **group_data},
                hfid=hfid,
                ownership="owned",
                sequential=True,
            )
        )
    else:
        plan.add(TrackSpec(ids=(existing.id,)))


def _plan_group_pool(plan: TopologyPlan) -> NodeRef:
    """The group id pool when the bootstrap data did not load it: created once, shared (it is not the run's output)."""
    key = f"pool:{FIREWALL_HA_GROUP_POOL}"
    if any(isinstance(spec, NodeSpec) and spec.key == key for spec in plan.specs):
        return NodeRef(key)
    plan.add(
        NodeSpec(
            key=f"pool:{FIREWALL_HA_GROUP_POOL}",
            kind=POOL_KIND,
            data={
                "name": FIREWALL_HA_GROUP_POOL,
                "node": ManagedFirewallHA.__name__,
                "node_attribute": "group_id",
                "start_range": FIREWALL_HA_GROUP_MIN,
                "end_range": FIREWALL_HA_GROUP_MAX,
            },
            hfid=Hfid(POOL_KIND, (FIREWALL_HA_GROUP_POOL,)),
            ownership="shared",
        )
    )
    return NodeRef(f"pool:{FIREWALL_HA_GROUP_POOL}")


def _sync_port(device: str, reads: HaReads, template_ports: Mapping[str, Sequence[TemplatePort]]) -> str | None:
    """The first port of role ``ha`` of ``device``: its own when it exists already, else its template's."""
    if device in reads.sync_ports:
        return reads.sync_ports[device]
    return min((port.name for port in template_ports.get(device, ()) if port.role == SYNC_ROLE), default=None)


def _plan_sync(
    plan: TopologyPlan,
    *,
    name: str,
    members: tuple[str, str],
    reads: HaReads,
    template_ports: Mapping[str, Sequence[TemplatePort]],
    positions: Mapping[str, RackPosition | None],
    deployment_id: str,
    problems: list[str],
) -> None:
    """Both members' sync ports active, and their cable unless one is cabled to something else."""
    ports: list[tuple[str, str]] = []
    for device in members:
        port = _sync_port(device, reads, template_ports)
        if port is None:
            problems.append(f"[{device}] No HA sync interface found (expected role={SYNC_ROLE})")
            continue
        ports.append((device, port))
        plan.add(
            NodeSpec(
                key=f"sync-port:{device}/{port}",
                kind="DcimPhysicalInterface",
                data={"device": device_hfid(device), "name": port, "status": "active"},
                hfid=port_hfid(device, port),
                existing=True,
                ownership="template",
            )
        )
    cable_name = sync_cable_name(name)
    if len(ports) != 2:
        return
    if cable_name in reads.sync_cables:
        # Keep it in this run's group, or the next run deletes it as unused and the cable oscillates
        plan.add(TrackSpec(ids=(reads.sync_cables[cable_name],)))
        return
    if any(device in reads.cabled_sync for device, _ in ports):
        # An orphan of a partial run: the sync ports are cabled under another name, leave it
        return
    (dev_a, port_a), (dev_b, port_b) = sorted(ports)
    plan.add(
        NodeSpec(
            key=f"cable:{cable_name}",
            kind=CABLE_KIND,
            data={
                "name": cable_name,
                "type": "smf",
                "endpoints": [port_hfid(dev_a, port_a), port_hfid(dev_b, port_b)],
                **cable_attributes(
                    "smf",
                    [endpoint_label(dev_a, port_a), endpoint_label(dev_b, port_b)],
                    positions.get(dev_a),
                    positions.get(dev_b),
                ),
                **({"deployment": {"id": deployment_id}} if deployment_id else {}),
            },
            hfid=Hfid(CABLE_KIND, (cable_name,)),
            ownership="owned",
        )
    )


async def _group_pool_id(client: Any) -> str | None:
    """The id of the firewall HA group id pool, None when the bootstrap data did not load it."""
    pools = await client.filters(kind=POOL_KIND, name__value=FIREWALL_HA_GROUP_POOL)
    return pools[0].id if pools else None


def virtual_ha_lock_keys(ha_name: str, *, role: str) -> list[str]:
    """The locks a plan of the virtual pair ``ha_name`` holds around its reads and its apply: the domain is found or
    created by name, and a firewall domain draws its group id from one pool.
    """
    return [f"ha-domain-{ha_name}", *([HA_POOL_LOCK] if HA_KIND_BY_ROLE[role] == ManagedFirewallHA.__name__ else [])]


async def read_virtual_ha_reads(client: Any, *, ha_name: str, role: str, device_names: Sequence[str]) -> HaReads:
    """What exists of the virtual pair ``ha_name``: the domain, the sync NIC of each member (its first NIC of role ``ha``)
    and, for a firewall pair, the group id pool.
    """
    ha_kind = HA_KIND_BY_ROLE[role]
    domains = {
        node.name.value: HaDomain(id=node.id, group_id=getattr(getattr(node, "group_id", None), "value", None))
        for node in await client.filters(kind=ha_kind, name__value=ha_name)
    }
    nics = await client.filters(
        kind=DcimVirtualInterface, device__name__values=sorted(device_names), role__value=SYNC_ROLE, include=["device"]
    )
    sync_ports: dict[str, str] = {}
    for nic in sorted(nics, key=lambda port: port.name.value):
        sync_ports.setdefault(nic.device.peer.name.value, nic.name.value)
    return HaReads(
        domains=domains,
        sync_ports=sync_ports,
        group_pool_id=await _group_pool_id(client) if ha_kind == ManagedFirewallHA.__name__ else None,
    )


def plan_virtual_ha_pair(
    plan: TopologyPlan, *, names: Sequence[str], role: str, ha_name: str, reads: HaReads, problems: list[str]
) -> None:
    """Pair the two virtual devices ``names`` of ``role`` (hosted instances, written or found) into the HA domain
    ``ha_name``: the domain as ``_plan_domain`` plans it and the sync NIC of each member set active (a template NIC, so
    untracked). A virtual pair has no sync cable: the instances ride their hosts' ports. A member without a sync NIC is a
    problem the caller raises before anything is written.
    """
    first, second = sorted(names)
    ha_kind = HA_KIND_BY_ROLE[role]
    group_pool: str | NodeRef | None = reads.group_pool_id
    if ha_kind == ManagedFirewallHA.__name__ and group_pool is None and ha_name not in reads.domains:
        group_pool = _plan_group_pool(plan)
    _plan_domain(
        plan,
        name=ha_name,
        members=(first, second),
        member_kind=VIRTUAL_DEVICE_KIND,
        ha_kind=ha_kind,
        reads=reads,
        group_pool=group_pool,
    )
    for device in (first, second):
        port = reads.sync_ports.get(device)
        if port is None:
            problems.append(f"[{device}] No HA sync interface found (expected role={SYNC_ROLE})")
            continue
        plan.add(
            NodeSpec(
                key=f"sync-port:{device}/{port}",
                kind="DcimVirtualInterface",
                data={"device": device_hfid(device, VIRTUAL_DEVICE_KIND), "name": port, "status": "active"},
                hfid=Hfid("DcimVirtualInterface", (device, port)),
                existing=True,
                ownership="template",
            )
        )


def plan_appliance_ha_pairs(
    plan: TopologyPlan, *, names: Sequence[str], role: str, reads: HaReads, appliance_kind: str
) -> list[str]:
    """Pair the provider-hosted appliances ``names`` (of ``appliance_kind``) of ``role`` into HA domains and return the
    domain names: the domain of ``plan_ha_pairs`` (found: kept in the run's group, and given a group id when a firewall
    pair has none; new: both members, the ``ha_domains`` group and, for a firewall pair, a group id) and nothing else: an
    appliance has no sync port and no cable.
    """
    ha_kind = HA_KIND_BY_ROLE[role]
    firewall = ha_kind == ManagedFirewallHA.__name__
    group_pool: str | NodeRef | None = reads.group_pool_id
    if firewall and group_pool is None and any(name not in reads.domains for name in ha_domain_names(names)):
        group_pool = _plan_group_pool(plan)
    domains: list[str] = []
    for first, second in pair_device_names(list(names)):
        name = f"{first}-{second}"
        domains.append(name)
        existing = reads.domains.get(name)
        group_data: dict[str, Any] = {"group_id": {"from_pool": {"id": group_pool}}} if firewall else {}
        if existing is None:
            plan.add(
                NodeSpec(
                    key=f"ha:{name}",
                    kind=ha_kind,
                    data={
                        "name": name,
                        "status": "active",
                        "capabilities": [device_hfid(first, appliance_kind), device_hfid(second, appliance_kind)],
                        "member_of_groups": [Hfid(GROUP_KIND, (GROUP_HA_DOMAINS,))],
                    },
                    create_only=group_data,
                    hfid=Hfid(ha_kind, (name,)),
                    ownership="owned",
                    sequential=firewall,
                )
            )
        elif firewall and existing.group_id is None:
            plan.add(
                NodeSpec(
                    key=f"ha:{name}",
                    kind=ha_kind,
                    data={"id": existing.id, **group_data},
                    hfid=Hfid(ha_kind, (name,)),
                    ownership="owned",
                    sequential=True,
                )
            )
        else:
            plan.add(TrackSpec(ids=(existing.id,)))
    return domains


# the HA-paired roles of a DC or pod, in the order their devices are planned
SERVICE_ROLES = ("internal-firewall", "load-balancer")


@dataclass
class ServiceDevices:
    """The HA-paired service devices a fabric_templates list declares, by kind."""

    firewalls: list[str] = field(default_factory=list)
    load_balancers: list[str] = field(default_factory=list)
    external_firewalls: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[str]:
        return [*self.firewalls, *self.load_balancers, *self.external_firewalls]


def service_entries(
    fabric_templates: list[dict[str, Any]], *, external: bool = False
) -> list[tuple[str, dict[str, Any]]]:
    """The firewall and load-balancer entries of ``fabric_templates`` (and the external-firewall ones when ``external``:
    the Internet edge pair) as (role, entry), in the order their devices are planned.
    """
    roles = [*SERVICE_ROLES, EXTERNAL_FIREWALL_ROLE] if external else SERVICE_ROLES
    return [(role, entry) for role in roles for entry in templates_by_role(fabric_templates, role)]


@dataclass(frozen=True)
class ServiceReads:
    """What exists before the run and the service devices need, besides the device and template reads of the plan: the HA
    state, the rack positions of the devices the cables join and the live ports of the border and service devices that
    exist.
    """

    ha: HaReads
    positions: Mapping[str, RackPosition | None]
    chain_ports: Mapping[str, Sequence[ChainPort]]


def role_index_starts(entries: Sequence[tuple[str, dict[str, Any]]]) -> list[int]:
    """The role index the devices of each entry start at: the numbering of a role continues across its entries, so a
    second entry of a role never names a device of the first.
    """
    planned: dict[str, int] = {}
    starts: list[int] = []
    for role, entry in entries:
        starts.append(planned.get(role, 0) + 1)
        planned[role] = planned.get(role, 0) + entry["quantity"]
    return starts


def service_names(
    entries: Sequence[tuple[str, dict[str, Any]]],
    *,
    fabric_name: str,
    naming_convention: NamingConvention,
    indexes: list[int],
) -> list[list[str]]:
    """The names of the devices of each entry of ``entries`` (as ``plan_physical_devices`` names them), in entry order."""
    return [
        planned_device_names(
            fabric_name=fabric_name,
            pod_name=None,
            device_role=role,
            quantity=entry["quantity"],
            naming_convention=naming_convention,
            options=DeviceOptions(indexes=indexes, role_index_start=start),
        )
        for (role, entry), start in zip(entries, role_index_starts(entries), strict=True)
    ]


def service_lock_keys(entries: Sequence[tuple[str, dict[str, Any]]], names: Sequence[Sequence[str]]) -> list[str]:
    """The locks the plan of the service devices of ``entries`` (named ``names``) holds: the HA domains and the group id
    pool.
    """
    return [
        key
        for (role, _), entry_names in zip(entries, names, strict=True)
        for key in ha_lock_keys(entry_names, firewall=role != "load-balancer")
    ]


async def read_service_reads(
    client: Any,
    *,
    entries: Sequence[tuple[str, dict[str, Any]]],
    names: Sequence[Sequence[str]],
    border_names: Sequence[str],
) -> ServiceReads:
    """Read the HA state of the entries' pairs, the positions of the devices their cables join (the services and
    the border tier) and the live ports of those that exist.
    """
    if not entries:
        return ServiceReads(ha=HaReads(), positions={}, chain_ports={})
    kinds = {
        f"{first}-{second}": HA_KIND_BY_ROLE[role]
        for (role, _), entry_names in zip(entries, names, strict=True)
        for first, second in pair_device_names(list(entry_names))
    }
    every = [name for entry_names in names for name in entry_names]
    return ServiceReads(
        ha=await read_ha_reads(
            client,
            domains=kinds,
            device_names=every,
            firewall=any(kind == "ManagedFirewallHA" for kind in kinds.values()),
        ),
        positions=await read_device_positions(client, [*every, *border_names]),
        chain_ports=await read_chain_ports(client, [*every, *border_names]),
    )


def plan_fabric_services(
    plan: TopologyPlan,
    *,
    entries: Sequence[tuple[str, dict[str, Any]]],
    fabric_name: str,
    deployment_id: str,
    cable_deployment_id: str,
    naming_convention: NamingConvention,
    indexes: list[int],
    connectivity_mode: Literal["pbr", "inline"],
    border_names: list[str],
    border_ports: Mapping[str, Sequence[TemplatePort]],
    devices: DeviceReads,
    templates: TemplateReads,
    reads: ServiceReads,
    controllers: list[dict[str, Any]],
    logger: logging.Logger,
    problems: list[str],
    options: DeviceOptions | None = None,
) -> ServiceDevices:
    """The HA-paired service devices of ``entries`` (firewalls, load balancers, the Internet-edge pair): the devices (a
    management address each, the role's group or controller, no loopback), their HA domains, and their cables to the
    border tier ``border_names`` (the template ports of a border device the plan creates: ``border_ports``). An external
    pair is cabled as a pbr leg off the same border devices on the next free firewall ports, never inline.
    ``deployment_id`` is the deployment of the devices, ``cable_deployment_id`` the one of their cables. ``options``:
    more ``DeviceOptions`` of every device (the pools their addresses come from).
    """
    services = ServiceDevices()
    ports_of: dict[str, Sequence[TemplatePort]] = dict(border_ports)
    for (role, entry), start in zip(entries, role_index_starts(entries), strict=True):
        template = entry["template"]
        template_ports = templates.ports.get(template["id"], [])
        names = plan_physical_devices(
            plan,
            reads=devices,
            fabric_name=fabric_name,
            pod_name="",
            role=role,
            quantity=entry["quantity"],
            deployment_id=deployment_id,
            template=template,
            template_ports=template_ports,
            template_owner_id=templates.owners.get(template["id"]),
            naming_convention=naming_convention,
            options={"indexes": indexes, "role_index_start": start, **(options or {})},
            controller=resolve_role_controller(controllers, device_role=role, template=template, logger=logger),
        )
        ports_of.update(dict.fromkeys(names, template_ports))
        plan_ha_pairs(
            plan,
            names=names,
            role=role,
            reads=reads.ha,
            template_ports=ports_of,
            positions=reads.positions,
            deployment_id=cable_deployment_id,
            problems=problems,
        )
        {
            "internal-firewall": services.firewalls,
            "load-balancer": services.load_balancers,
            EXTERNAL_FIREWALL_ROLE: services.external_firewalls,
        }[role].extend(names)
    chain_ports = chain_ports_of(reads.chain_ports, ports_of)
    plan_border_services(
        plan,
        connectivity_mode=connectivity_mode,
        border_names=border_names,
        firewall_names=services.firewalls,
        load_balancer_names=services.load_balancers,
        ports=chain_ports,
        deployment_id=cable_deployment_id,
        positions=reads.positions,
        problems=problems,
    )
    if services.external_firewalls:
        plan_border_services(
            plan,
            connectivity_mode="pbr",
            border_names=border_names,
            firewall_names=services.external_firewalls,
            load_balancer_names=[],
            ports=chain_ports,
            deployment_id=cable_deployment_id,
            positions=reads.positions,
            problems=problems,
        )
    return services


def plan_border_services(
    plan: TopologyPlan,
    *,
    connectivity_mode: Literal["pbr", "inline"],
    border_names: list[str],
    firewall_names: list[str],
    load_balancer_names: list[str],
    ports: ChainPorts,
    deployment_id: str,
    positions: Mapping[str, RackPosition | None],
    problems: list[str],
) -> None:
    """Cable border<->firewall<->load-balancer per ``connectivity_mode``, index-paired (border[0]<->fw[0], ...): one
    chain per leg (``border_service_chains``). A leg with nothing to cable is a no-op.
    """
    for hops in border_service_chains(
        border_role_for=BORDER_ROLE_FOR_SERVICES,
        connectivity_mode=connectivity_mode,
        border_names=border_names,
        firewall_names=firewall_names,
        load_balancer_names=load_balancer_names,
    ):
        plan_chain_legs(
            plan, hops=hops, ports=ports, deployment_id=deployment_id, positions=positions, problems=problems
        )


# The service contexts on an HA pair (a shared capability, schemas/extensions/capabilities/ha.yml): what a kind of
# context differs in, and the VLAN pool a fabric draws its contexts' VLANs from. The topologies plan the pool (dc/,
# colocation/); the customer boarding (customers/) writes the contexts.


def _firewall_leg_types(tenant_type: str) -> tuple[str, ...]:
    """The tenant VRF and every VRF it exchanges with (EXCHANGE_PEERS), tenant first."""
    return (tenant_type, *EXCHANGE_PEERS[tenant_type])


def _edge_leg_types(tenant_type: str) -> tuple[str, ...]:
    """The Internet edge context has a leg in the tenant VRF and one in INTERNET, nothing else."""
    return (tenant_type, INTERNET_TYPE)


def _no_skip(_parent: dict[str, Any]) -> str | None:
    """Every parent with devices of the role gets the context."""
    return None


def _inline_parent(parent: dict[str, Any]) -> str | None:
    """Why an inline DC gets no load-balancer context: its load balancers sit in the firewall chain
    (capabilities/ha.py plan_border_services).
    """
    if parent.get("connectivity_mode") == "inline":
        return "its load balancers are inline behind the firewall (connectivity_mode inline)"
    return None


@dataclass(frozen=True)
class ContextSpec:
    """What a kind of service context differs in; everything else is one flow. Fields: lock key and VLAN pool name,
    tenant leg types, leg identifier, VLAN range, skip reason, VIP pool, serving field(s), relink consumers.
    """

    label: str
    role: str
    devices_key: str
    dedicated_flag: str
    ha_kind: type[Any]
    context_kind: type[Any]
    key: str
    serving_field: str
    serving_many: bool
    leg_types: Callable[[str], tuple[str, ...]]
    leg_pool_name: Callable[[str], str]
    leg_identifier: str
    vlan_range: tuple[int, int]
    skip_reason: Callable[[dict[str, Any]], str | None] = _no_skip
    vip_pool_name: Callable[[str], str] | None = None
    relink_consumers: Callable[[Any, str], Awaitable[list[str]]] | None = None
    relink_generator: str = ""


FIREWALL_CONTEXT = ContextSpec(
    label="FirewallContext",
    role=INTERNAL_FIREWALL_ROLE,
    devices_key="firewall_devices",
    dedicated_flag="dedicated_firewall",
    ha_kind=ManagedFirewallHA,
    context_kind=ManagedFirewallContext,
    key="fw-context",
    serving_field="serving_firewall_contexts",
    serving_many=True,
    leg_types=_firewall_leg_types,
    leg_pool_name=transit_pool_name,
    leg_identifier="transit",
    vlan_range=(FW_CONTEXT_VLAN_START, FW_CONTEXT_VLAN_END),
)

EDGE_CONTEXT = ContextSpec(
    label="FirewallContext",
    role=EXTERNAL_FIREWALL_ROLE,
    devices_key="external_firewall_devices",
    dedicated_flag="",
    ha_kind=ManagedFirewallHA,
    context_kind=ManagedFirewallContext,
    # The VLAN pool and lock prefix are the firewall contexts': an edge context draws from the same DC pool, so no two
    # share a VLAN
    key="fw-context",
    # The footprint lists its Internet edge context next to its internal one (utils/firewall_split.py)
    serving_field="serving_firewall_contexts",
    serving_many=True,
    leg_types=_edge_leg_types,
    leg_pool_name=transit_pool_name,
    leg_identifier="transit",
    vlan_range=(FW_CONTEXT_VLAN_START, FW_CONTEXT_VLAN_END),
)

LOADBALANCER_CONTEXT = ContextSpec(
    label="LoadbalancerContext",
    role="load-balancer",
    devices_key="loadbalancer_devices",
    dedicated_flag="dedicated_loadbalancer",
    ha_kind=ManagedLoadbalancerHA,
    context_kind=ManagedLoadbalancerContext,
    key="lb-context",
    serving_field="serving_loadbalancer_context",
    serving_many=False,
    leg_types=lambda tenant_type: (tenant_type,),
    leg_pool_name=lb_leg_pool_name,
    leg_identifier="lb-leg",
    vlan_range=(LB_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END),
    skip_reason=_inline_parent,
    vip_pool_name=lb_vip_pool_name,
)


def context_vlan_pool_lock(*, name: str, spec: ContextSpec) -> str:
    """The lock key of a fabric's context VLAN pool of one kind: a number pool upsert is idempotent only for sequential
    runs, so its creation holds it.
    """
    return f"{spec.key}-pools-{name}"


def plan_context_vlan_pool(plan: TopologyPlan, *, name: str, spec: ContextSpec) -> NodeRef:
    """A fabric's VLAN pool for the service contexts of ``spec``'s kind, ``{name}-{spec.key}-vlan-pool``, as a plan node
    owned by the run; read by firewall_context.py's _ensure_context_vlan. ``spec.vlan_range`` keeps the firewall and
    load-balancer bands apart. The caller holds ``context_vlan_pool_lock``.
    """
    return plan_number_pool(
        plan,
        name=f"{name}-{spec.key}-vlan-pool",
        description=f"{spec.label} VLAN pool for {name.upper()}",
        start_range=spec.vlan_range[0],
        end_range=spec.vlan_range[1],
        node=spec.context_kind.__name__,
        node_attribute="vlan_id",
    )
