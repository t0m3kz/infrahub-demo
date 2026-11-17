"""MLAG domains and their peer-link wiring, as plan builders: pure functions of the cleaned query data and what already
exists (``MlagReads``), with no write (generators/plan.py). ``plan_mlag_domains`` is what a rack plan adds for the devices
it creates (the domain of each pair, then its wiring); ``MLAGGenerator`` (add_mlag, reached only off the ManagedMLAG
"updated" triggers) plans the wiring alone, for a domain that exists.

A domain is wired from its two members (sorted by name): a physical peer-link bundles every ``mlag-peer`` port in a LAG
per member and cables the ports pairwise (``CBL-{domain}-PL{n}``); a virtual peer-link is a loopback per member and no
cable. The control session between the peers (a /31 or /127 from a bootstrap pool, a /32 or /128 on the loopbacks of a
virtual peer-link, none for vPC) is addressed from one prefix keyed by the domain's id. What the other mode left (the
peer-link of the other kind, the control SVIs, the cables) is retired: it is deleted when a generator tracks it, and a
hand-made object in the way fails the run. The wiring is the run's output (``owned``) when the run creates the domain and
untracked (``shared``) when ``MLAGGenerator`` writes it.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from generators.base import BaseGenerator
from generators.dcim.cabling_plan import PortFacts
from generators.dcim.device_plan import device_hfid
from generators.dcim.rack_layout import read_device_positions
from generators.dcim.template_interfaces import TemplatePort
from generators.helpers.cable_attributes import RackPosition, cable_attributes, endpoint_label
from generators.helpers.interface_naming import get_lag_name, get_loopback_name
from generators.helpers.pairing import pair_device_names
from generators.model.errors import GeneratorError
from generators.plan import (
    EdgesSpec,
    Hfid,
    NodeRef,
    NodeSpec,
    Ownership,
    PrefixAllocationSpec,
    RetireSpec,
    TopologyPlan,
)
from generators.protocols import DcimCable, DcimPhysicalDevice, DcimPhysicalInterface, ManagedMLAG, TopologyPod
from utils.data_cleaning import clean_data
from utils.limits import GROUP_MLAG_DOMAINS, MLAG_CONTROL_IPV4_POOL, MLAG_CONTROL_IPV6_POOL
from utils.segment_vlan import mlag_peer_devices

MLAG_KIND = "ManagedMLAG"
LAG_KIND = "DcimLAGInterface"
VIRTUAL_KIND = "DcimVirtualInterface"
CABLE_KIND = "DcimCable"
GROUP_KIND = "CoreStandardGroup"
PEER_ROLE = "mlag-peer"
PEER_LINK_LAG_ID = 100
CONTROL_SVI_NAME = "Vlan4094"

# The MLAG control session runs between the peers only and is never routed, so its family is independent of the
# underlay. SONiC MC-LAG (ICCP) accepts IPv4 only; others use IPv6.
IPV4_CONTROL_PLATFORMS = frozenset({"sonic", "dell_sonic"})
# vPC has no routed control session; its peer-keepalive runs over management
NO_CONTROL_PLATFORMS = frozenset({"cisco_nxos"})
# family -> (pool name, peer-link prefix length, virtual loopback prefix length)
CONTROL_POOLS: dict[int, tuple[str, int, int]] = {
    4: (MLAG_CONTROL_IPV4_POOL, 31, 32),
    6: (MLAG_CONTROL_IPV6_POOL, 127, 128),
}


def mlag_domain_name(first: str, second: str) -> str:
    """The domain of the pair ``first``/``second`` (sorted, as ``pair_device_names`` gives them)."""
    return f"{first}-{second}-mlag"


def mlag_lock_key(domain_name: str) -> str:
    """The lock a plan holds around the reads and the apply of a domain: its control prefix and addresses are found or
    created, and two runs must not both create them.
    """
    return f"mlag-domain-{domain_name}"


@dataclass(frozen=True)
class MlagMember:
    """One device of a domain: its name, its platform's name (the naming of its LAG and loopback, the family of its
    control session) and its ``mlag-peer`` ports.
    """

    name: str
    platform: str
    ports: tuple[PortFacts, ...]


@dataclass(frozen=True)
class MlagReads:
    """What exists before the run, for the domains of one plan: the control pools that are loaded, the peer-link cables by name (-> id), the ``mlag-peer`` ports of the members that
    exist (device -> ports) and the cable on each such port ((device, port) -> cable id).
    """

    control_pools: frozenset[str] = frozenset()
    cables: Mapping[str, str] = field(default_factory=dict)
    ports: Mapping[str, tuple[PortFacts, ...]] = field(default_factory=dict)
    port_cables: Mapping[tuple[str, str], str] = field(default_factory=dict)


def peer_ports(device: str, reads: MlagReads, template_ports: Sequence[TemplatePort]) -> tuple[PortFacts, ...]:
    """The ``mlag-peer`` ports of ``device``, sorted by name: its own when it exists, else its template's."""
    if device in reads.ports:
        return reads.ports[device]
    return tuple(
        sorted(
            (PortFacts(device, port.name, port.interface_type) for port in template_ports if port.role == PEER_ROLE),
            key=lambda port: port.port_name,
        )
    )


async def read_mlag_reads(client: Any, *, device_names: Sequence[str], domain_names: Sequence[str]) -> MlagReads:
    """Read what the wiring of ``domain_names`` needs of the members
    ``device_names`` that exist.
    """
    names = sorted(set(device_names))
    if not names:
        return MlagReads()
    pools = await client.filters(kind="CoreIPPrefixPool", name__values=[MLAG_CONTROL_IPV4_POOL, MLAG_CONTROL_IPV6_POOL])
    ports = (
        await client.filters(
            kind=DcimPhysicalInterface,
            device__name__values=names,
            role__value=PEER_ROLE,
            include=["device", "cable"],
        )
        if names
        else []
    )
    # A member that exists has all its ports: a peer-link cable has no more pairs than the most ports of one member
    port_count = max(Counter(port.device.peer.name.value for port in ports).values(), default=0)
    cable_names = [f"CBL-{domain}-PL{index}" for domain in domain_names for index in range(1, port_count + 1)]
    cables = await client.filters(kind=DcimCable, name__values=cable_names) if cable_names else []
    by_device: dict[str, list[PortFacts]] = {}
    port_cables: dict[tuple[str, str], str] = {}
    for port in sorted(ports, key=lambda p: p.name.value):
        device = port.device.peer.name.value
        by_device.setdefault(device, []).append(
            PortFacts(device, port.name.value, getattr(getattr(port, "interface_type", None), "value", None))
        )
        if getattr(port, "cable", None) is not None and port.cable.initialized:
            port_cables[(device, port.name.value)] = port.cable.id
    return MlagReads(
        control_pools=frozenset(pool.name.value for pool in pools),
        cables={cable.name.value: cable.id for cable in cables},
        ports={device: tuple(device_ports) for device, device_ports in by_device.items()},
        port_cables=port_cables,
    )


def plan_mlag_domains(
    plan: TopologyPlan,
    *,
    names: Sequence[str],
    role: str,
    template: Mapping[str, Any],
    mlag_create: str,
    supports_virtual: bool,
    members: Mapping[str, MlagMember],
    cable_deployment_id: str | None,
    reads: MlagReads,
    positions: Mapping[str, RackPosition | None],
    logger: logging.Logger,
    problems: list[str],
) -> None:
    """Pair the devices ``names`` of ``role`` (planned in ``plan``) two at a time into MLAG domains per ``mlag_create``
    and wire each. ``supports_virtual`` False (l2-leaf) forces back-to-back; an unpaired device is its own VLAN domain.
    """
    if len(names) < 2:
        return
    if mlag_create == "virtual" and not supports_virtual:
        logger.info(
            f"{role} has no routing/loopback — using back-to-back MLAG instead of the pod's virtual setting (L2-only role)."
        )
        mlag_create = "back-to-back"
    if mlag_create == "back-to-back" and not any(
        iface.get("role") == PEER_ROLE for iface in template.get("interfaces", [])
    ):
        problems.append(
            f"template {template.get('id')} has no mlag-peer interface — cannot create back-to-back MLAG for {role}s."
        )
        return
    for pair_index, (first, second) in enumerate(pair_device_names(list(names)), start=1):
        name = mlag_domain_name(first, second)
        domain = Hfid(MLAG_KIND, (name,))
        plan.add(
            NodeSpec(
                key=f"mlag:{name}",
                kind=MLAG_KIND,
                data={
                    "name": name,
                    "peer_devices": mlag_peer_devices([first, second]),
                    "domain_id": pair_index,
                    "virtual_peer_link": mlag_create == "virtual",
                    "status": "active",
                    "capabilities": [device_hfid(first), device_hfid(second)],
                    "member_of_groups": [Hfid(GROUP_KIND, (GROUP_MLAG_DOMAINS,))],
                },
                hfid=domain,
                ownership="owned",
            )
        )
        plan_mlag_wiring(
            plan,
            domain=domain,
            domain_node=NodeRef(f"mlag:{name}"),
            members=(members[first], members[second]),
            virtual_peer_link=mlag_create == "virtual",
            cable_deployment_id=cable_deployment_id,
            reads=reads,
            positions=positions,
            ownership="owned",
            logger=logger,
            problems=problems,
        )


def plan_mlag_wiring(
    plan: TopologyPlan,
    *,
    domain: Hfid,
    domain_node: str | NodeRef,
    members: tuple[MlagMember, MlagMember],
    virtual_peer_link: bool,
    cable_deployment_id: str | None,
    reads: MlagReads,
    positions: Mapping[str, RackPosition | None],
    ownership: Ownership,
    logger: logging.Logger,
    problems: list[str],
) -> None:
    """Wire one domain (``domain``: its HFID, ``domain_node``: its id or its node in the plan): the control addresses, the
    peer-link of each member, the cables, what the other mode left, and the domain's list of interfaces.
    """
    name = domain.parts[0]
    first, second = sorted(members, key=lambda member: member.name)
    logger.info(f"Processing MLAG domain {name} ({'virtual' if virtual_peer_link else 'physical'} peer-link)")
    retire: list[Hfid] = []
    control = _plan_control_addresses(
        plan, domain, name, (first, second), virtual_peer_link, reads, ownership, retire, logger
    )
    listed: list[Hfid] = [port.hfid for member in (first, second) for port in member.ports]
    wired = 0
    for member in (first, second):
        lag_name = get_lag_name(member.platform, PEER_LINK_LAG_ID)
        loopback_name = get_loopback_name(member.platform, PEER_LINK_LAG_ID)
        # Physical and virtual peer-links are mutually exclusive: the one of the other mode is stale
        retire.append(
            Hfid(LAG_KIND, (member.name, lag_name))
            if virtual_peer_link
            else Hfid(VIRTUAL_KIND, (member.name, loopback_name))
        )
        if virtual_peer_link:
            link = _virtual_peer_link(member, name, loopback_name, control.get(member.name), ownership)
        else:
            if not member.ports:
                problems.append(
                    f"[{member.name}] No physical interfaces with role={PEER_ROLE} found — cannot create peer-link LAG"
                )
                continue
            _activate_ports(plan, member)
            link = _lag_peer_link(member, domain, lag_name, ownership)
            if control:
                svi = _control_svi(member, name, control[member.name], ownership)
                plan.add(svi)
                listed.append(_hfid_of(svi))
        plan.add(link)
        listed.append(_hfid_of(link))
        wired += 1
    if virtual_peer_link:
        retire.extend(
            Hfid(CABLE_KIND, (f"CBL-{name}-PL{index}",))
            for index in range(1, max(len(m.ports) for m in (first, second)) + 1)
        )
    elif wired == 2:
        _plan_peer_cables(plan, name, (first, second), cable_deployment_id, reads, positions, ownership)
    plan.add(RetireSpec(tuple(retire)))
    plan.add(EdgesSpec(node=domain_node, relation="interfaces", peers=tuple(listed), kind=MLAG_KIND))


def _plan_control_addresses(
    plan: TopologyPlan,
    domain: Hfid,
    name: str,
    members: tuple[MlagMember, MlagMember],
    virtual_peer_link: bool,
    reads: MlagReads,
    ownership: Ownership,
    retire: list[Hfid],
    logger: logging.Logger,
) -> dict[str, NodeRef]:
    """The control session addresses, member name -> address: one prefix per domain, stable by the domain's id; the lower
    address goes to the member sorting first. Nothing for vPC or a pool that is not loaded; the control SVIs the other
    mode left go to ``retire`` for vPC and for a virtual peer-link (the loopback carries the address).
    """
    platforms = {member.platform for member in members}
    svis = [Hfid(VIRTUAL_KIND, (member.name, CONTROL_SVI_NAME)) for member in members]
    if platforms & NO_CONTROL_PLATFORMS:
        retire.extend(svis)
        return {}
    pool_name, prefix_length, loopback_length = CONTROL_POOLS[4 if platforms & IPV4_CONTROL_PLATFORMS else 6]
    if pool_name not in reads.control_pools:
        logger.warning(f"[{name}] Pool {pool_name} not found — skipping MLAG control addressing")
        return {}
    if virtual_peer_link:
        retire.extend(svis)
    prefix = PrefixAllocationSpec(
        key=f"mlag-control:{name}",
        identifier="mlag-control",
        identifier_ref=domain,
        prefix_length=prefix_length,
        role="technical",
        pool_name=pool_name,
        is_pool=True,
        member_type="address",
        address_offsets=(0, 1),
        address_description=f"MLAG control — {name}",
        address_length=loopback_length if virtual_peer_link else prefix_length,
        address_ownership=ownership,
    )
    plan.add(prefix)
    return {member.name: NodeRef(prefix.address_key(offset)) for offset, member in enumerate(members)}


def _hfid_of(spec: NodeSpec) -> Hfid:
    """The HFID a builder here always sets on its node."""
    if spec.hfid is None:
        raise ValueError(f"NodeSpec {spec.key!r} has no HFID")
    return spec.hfid


def _activate_ports(plan: TopologyPlan, member: MlagMember) -> None:
    """The peer ports of a physical peer-link are in service (a template port: not tracked)."""
    for port in member.ports:
        plan.add(
            NodeSpec(
                key=f"port:{member.name}/{port.port_name}",
                kind="DcimPhysicalInterface",
                data={"device": device_hfid(member.name), "name": port.port_name, "status": "active"},
                hfid=port.hfid,
                existing=True,
                ownership="template",
            )
        )


def _lag_peer_link(member: MlagMember, domain: Hfid, lag_name: str, ownership: Ownership) -> NodeSpec:
    """The LAG bundling ALL ``mlag-peer`` ports of the member."""
    return NodeSpec(
        key=f"lag:{member.name}/{lag_name}",
        kind=LAG_KIND,
        data={
            "name": lag_name,
            "device": device_hfid(member.name),
            "status": "active",
            "role": PEER_ROLE,
            "lag_id": PEER_LINK_LAG_ID,
            "lacp_mode": "active",
            "mtu": 9000,
            "minimum_links": 1,
            "mlag_domain": domain,
            "member_interfaces": [port.hfid for port in member.ports],
        },
        hfid=Hfid(LAG_KIND, (member.name, lag_name)),
        ownership=ownership,
    )


def _virtual_peer_link(
    member: MlagMember, domain_name: str, loopback_name: str, control_address: NodeRef | None, ownership: Ownership
) -> NodeSpec:
    """The loopback that is the member's virtual peer-link, carrying its control address when there is one."""
    return NodeSpec(
        key=f"virtual-peer-link:{member.name}/{loopback_name}",
        kind=VIRTUAL_KIND,
        data={
            "name": loopback_name,
            "device": device_hfid(member.name),
            "status": "active",
            "role": PEER_ROLE,
            "description": f"MLAG virtual peer-link — {domain_name}",
            **({"ip_address": control_address} if control_address is not None else {}),
        },
        hfid=Hfid(VIRTUAL_KIND, (member.name, loopback_name)),
        ownership=ownership,
    )


def _control_svi(member: MlagMember, domain_name: str, address: NodeRef, ownership: Ownership) -> NodeSpec:
    """The Vlan4094 SVI that carries the control session over a back-to-back peer-link."""
    return NodeSpec(
        key=f"control-svi:{member.name}",
        kind=VIRTUAL_KIND,
        data={
            "name": CONTROL_SVI_NAME,
            "device": device_hfid(member.name),
            "status": "active",
            "role": "mlag-control",
            "description": f"MLAG control — {domain_name}",
            "ip_address": address,
        },
        hfid=Hfid(VIRTUAL_KIND, (member.name, CONTROL_SVI_NAME)),
        ownership=ownership,
    )


def _plan_peer_cables(
    plan: TopologyPlan,
    name: str,
    members: tuple[MlagMember, MlagMember],
    deployment_id: str | None,
    reads: MlagReads,
    positions: Mapping[str, RackPosition | None],
    ownership: Ownership,
) -> None:
    """One cable per index-matched pair of the members' sorted ``mlag-peer`` ports. A cable already on either port under
    another name is adopted (renamed), as the upsert by id does.
    """
    first, second = members
    for index, (port_a, port_b) in enumerate(zip(first.ports, second.ports), start=1):
        cable_name = f"CBL-{name}-PL{index}"
        existing_id = (
            reads.cables.get(cable_name)
            or reads.port_cables.get((first.name, port_a.port_name))
            or reads.port_cables.get((second.name, port_b.port_name))
        )
        plan.add(
            NodeSpec(
                key=f"cable:{cable_name}",
                kind=CABLE_KIND,
                data={
                    **({"id": existing_id} if existing_id else {}),
                    "name": cable_name,
                    "type": "smf",
                    "endpoints": [port_a.hfid, port_b.hfid],
                    **cable_attributes(
                        "smf",
                        [
                            endpoint_label(first.name, port_a.port_name),
                            endpoint_label(second.name, port_b.port_name),
                        ],
                        positions.get(first.name),
                        positions.get(second.name),
                    ),
                    **({"deployment": {"id": deployment_id}} if deployment_id else {}),
                },
                hfid=Hfid(CABLE_KIND, (cable_name,)),
                ownership=ownership,
            )
        )


# add_mlag


class MLAGGenerator(BaseGenerator):
    """Wire the peer-link interfaces of both devices of an MLAG domain, off its "updated" trigger. The wiring belongs to
    the generator that created the domain (a rack): this one writes it untracked, or the cleanup of its own run would
    remove a peer-link it stopped reaching.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        mlag_nodes: list[dict] = cleaned.get("ManagedMLAG", [])

        if not mlag_nodes:
            self.logger.error("No ManagedMLAG found in query response")
            return

        for mlag in mlag_nodes:
            await self._wire_domain(mlag["id"], mlag["name"])

    async def _wire_domain(self, mlag_id: str, name: str) -> None:
        """Read the domain and its two devices, plan the wiring under the domain's lock and write it."""
        mlag_obj = await self.client.get(kind=ManagedMLAG, id=mlag_id, include=["capabilities"])
        member_ids = [peer.id for peer in getattr(mlag_obj, "capabilities").peers]
        if len(member_ids) != 2:
            self.logger.error(f"[{name}] Expected exactly 2 peer devices in capabilities, got {len(member_ids)}")
            return
        # include= puts the platform in the client store, so platform.peer resolves without a fetch()
        devices = await self.client.filters(kind=DcimPhysicalDevice, ids=member_ids, include=["deployment", "platform"])
        if len(devices) != 2:
            self.logger.error(f"[{name}] could not resolve both peer devices ({member_ids})")
            return
        virtual_peer_link = bool(getattr(mlag_obj, "virtual_peer_link").value)
        device_names = [device.name.value for device in devices]
        async with self.plan_locks(mlag_lock_key(name)):
            reads = await read_mlag_reads(self.client, device_names=device_names, domain_names=[name])
            members = tuple(
                MlagMember(
                    name=device.name.value,
                    platform=device.platform.peer.name.value if device.platform.initialized else "",
                    ports=peer_ports(device.name.value, reads, ()),
                )
                for device in devices
            )
            problems: list[str] = []
            plan = TopologyPlan(root_id=None)
            plan_mlag_wiring(
                plan,
                domain=Hfid(MLAG_KIND, (name,)),
                domain_node=mlag_id,
                members=(members[0], members[1]),
                virtual_peer_link=virtual_peer_link,
                cable_deployment_id=await self._cable_deployment_id(devices[0]),
                reads=reads,
                positions=await read_device_positions(self.client, device_names),
                ownership="shared",
                logger=self.logger,
                problems=problems,
            )
            if problems:
                raise GeneratorError("Cannot wire the MLAG domain, nothing is written:\n- " + "\n- ".join(problems))
            await self.apply_plan(plan)

    async def _cable_deployment_id(self, device: Any) -> str | None:
        """Peer-link cables are filed against the PARENT of the device's deployment when that is a TopologyPod, else the
        deployment itself.
        """
        deployment = getattr(device, "deployment", None)
        if deployment is None or not deployment.initialized:
            return None
        if deployment.typename == "TopologyPod":
            pod = await self.client.get(kind=TopologyPod, id=deployment.id, include=["parent"])
            parent = getattr(pod, "parent", None)
            return parent.id if parent is not None and parent.initialized else None
        return deployment.id
