"""The plan of a service context (FirewallContext, LoadbalancerContext) and of its legs: pure functions of the facts the
caller read (generators/customers/firewall_transit.py) with no write and no read (generators/plan.py).

Two plans, because what the legs are named after is drawn by the first: ``plan_context`` writes the context, with its VLAN
drawn from the fabric's pool; ``plan_legs`` then plans the SNAT address and the VIP range the context points at, the /29
of each leg and its two addresses, the exchanges, and per member and leg the VLAN sub-interface (riding on the member's
uplink (its ``parent`` and ``vlan_id``), in the VRF instance of the leg) and the context's join of the service
contexts group. A dedicated context (``track``) is the customer's own and is written owned; a shared one is reached by
every customer and is written shared (never tracked, never pruned).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from generators.capabilities.ha import ContextSpec
from generators.capabilities.vrf import plan_interface_vrf
from generators.customers.firewall_context_spec import ContextTarget, TransitMember
from generators.plan import (
    AddressAllocationSpec,
    EdgesSpec,
    Hfid,
    NodeRef,
    NodeSpec,
    Ownership,
    PrefixAllocationSpec,
    TopologyPlan,
)
from generators.protocols import DcimVirtualInterface, TopologyRoutedExchange
from utils.exchange_transit import INTERNET_TYPE, TRANSIT_OFFSETS, transit_vlan
from utils.limits import FW_SNAT_POOL, GROUP_SERVICE_CONTEXTS, LB_VIP_PREFIXLEN, SNAT_PREFIX_LENGTH, TRANSIT_PREFIXLEN

CONTEXT_KEY = "context"
VIP_POOL_KEY = "vip"
"""The key of the VIP range pool among the pools of the legs (next to the leg types)."""


@dataclass(frozen=True)
class ExistingLegPrefix:
    """The prefix an addressed leg already carries (authored, or allocated by an earlier run): its network, namespace and
    the ids of the addresses that exist, by offset.
    """

    network: str
    namespace_id: str
    namespace_name: str
    address_ids: Mapping[int, str]


@dataclass(frozen=True)
class LegInterface:
    """A member's stored sub-interface on a leg and the ids of the VRF instances it is in."""

    id: str
    vrf_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LegsReads:
    """What the plan of the legs needs of what exists: the stored context (its id, name, SNAT address and VIP prefix), the
    context's VLAN, the prefix pool of each leg type (and ``VIP_POOL_KEY``), the prefix each leg already carries, and the
    stored sub-interface of each (member device id, leg type).
    """

    context: Any
    vlan_id: int
    pool_names: Mapping[str, str]
    existing_prefixes: Mapping[str, ExistingLegPrefix]
    interfaces: Mapping[tuple[str, str], LegInterface]


def ownership_of(target: ContextTarget) -> Ownership:
    """A dedicated context is the customer's own, a shared one is shared by every customer."""
    return "owned" if target.track else "shared"


def exchange_specs_of(
    context_name: str, tenant_type: str, leg_types: tuple[str, ...], namespaces: Mapping[str, Any]
) -> list[tuple[str, str, str]]:
    """(exchange name, A type, Z type): the tenant namespace to each peer leg type."""
    tenant = namespaces[tenant_type].name.value
    return [(f"{context_name}-{tenant}-{namespaces[peer].name.value}", tenant_type, peer) for peer in leg_types[1:]]


def legs_of(spec: ContextSpec, target: ContextTarget, tenant_type: str) -> tuple[str, ...]:
    """The leg types of a context: the spec's, less the INTERNET one when an external-firewall pair holds it (the Internet
    edge context has it; a dedicated context keeps only its own public address).
    """
    leg_types = spec.leg_types(tenant_type)
    if target.edge_hosted:
        leg_types = tuple(leg_type for leg_type in leg_types if leg_type != INTERNET_TYPE)
    return leg_types


def needs_snat(target: ContextTarget, leg_types: tuple[str, ...]) -> bool:
    """A context with an Internet leg, or a dedicated one behind an edge pair, has a SNAT address."""
    return INTERNET_TYPE in leg_types or (target.edge_hosted and target.tenant_id is not None)


def has_vlan(context: Any | None) -> bool:
    """Whether the stored context has its VLAN."""
    return context is not None and bool(getattr(getattr(context, "vlan_id", None), "value", None))


def has_peer(context: Any, relationship: str) -> bool:
    """Whether the stored context points at something through ``relationship`` (its SNAT address, its VIP prefix)."""
    return bool(getattr(getattr(context, relationship, None), "id", None))


def plan_context(
    plan: TopologyPlan,
    *,
    spec: ContextSpec,
    target: ContextTarget,
    name: str,
    existing: Any | None,
    vlan_pool_id: str | None,
) -> None:
    """The context ``name`` on the members of ``target``: the deployment, the shared devices (the one pair that enforces
    the context, written as the whole list: this flow is their only writer) and, for a dedicated one, its tenant. The VLAN
    is drawn from ``vlan_pool_id`` when the stored context has none (idempotent per node id); the caller read the pool
    before the first write.
    """
    data: dict[str, Any] = {
        "name": name,
        "deployment": {"id": target.deployment_id},
        "shared_devices": [
            {"id": device["id"] if isinstance(device, dict) else device.id} for device in target.devices
        ],
        **({"tenant": {"id": target.tenant_id}} if target.tenant_id else {}),
    }
    if existing is not None:
        data["id"] = existing.id
    drawn = not has_vlan(existing)
    if drawn:
        if vlan_pool_id is None:
            raise ValueError(f"{spec.label} '{name}' has no VLAN and no pool to draw one from")
        data["vlan_id"] = {"from_pool": {"id": vlan_pool_id}}
    plan.add(
        NodeSpec(
            key=CONTEXT_KEY,
            kind=spec.context_kind,
            data=data,
            hfid=Hfid(spec.context_kind.__name__, (name,)),
            ownership=ownership_of(target),
            sequential=drawn,
        )
    )


def context_vrf(members: list[TransitMember], vrfs: Mapping[tuple[str, str], str], zone: str) -> str:
    """A context's border attachments must identify one local instance for each exchange leg."""
    instances = {vrfs.get((member.bl_port.device.id, zone)) for member in members}
    if None in instances or len(instances) != 1:
        raise ValueError(f"Context attachments have missing or conflicting VRF instances for {zone}")
    instance = instances.pop()
    assert instance is not None
    return instance


def plan_legs(
    plan: TopologyPlan,
    *,
    spec: ContextSpec,
    target: ContextTarget,
    reads: LegsReads,
    tenant_type: str,
    members: list[TransitMember],
    namespaces: Mapping[str, Any],
    leg_types: tuple[str, ...],
    vrfs: Mapping[tuple[str, str], str],
) -> None:
    """The SNAT address and VIP range the stored context lacks, a /29 and two addresses per leg, one exchange per peer,
    per member and leg the sub-interface in its VRF instance, and the context's join of the service contexts group.
    Raises ValueError for a context whose border attachments hold no one VRF instance per leg (nothing is written).
    """
    context, ownership = reads.context, ownership_of(target)
    tenant_namespace = namespaces[tenant_type].name.value
    refs: dict[str, Any] = {}
    if needs_snat(target, leg_types) and not has_peer(context, "snat_address"):
        plan.add(
            AddressAllocationSpec(
                key="snat",
                identifier=f"{context.id}-snat",
                prefix_length=SNAT_PREFIX_LENGTH,
                description=f"SNAT address of {context.name.value}",
                ownership="shared",
                pool_name=FW_SNAT_POOL,
            )
        )
        refs["snat_address"] = NodeRef("snat")
    if spec.vip_pool_name is not None and not has_peer(context, "vip_prefix"):
        plan.add(
            PrefixAllocationSpec(
                key=VIP_POOL_KEY,
                identifier=f"{context.id}-{tenant_namespace}-lb-vip",
                prefix_length=LB_VIP_PREFIXLEN,
                role="technical",
                member_type="address",
                pool_name=reads.pool_names[VIP_POOL_KEY],
            )
        )
        refs["vip_prefix"] = NodeRef(VIP_POOL_KEY)
    if refs:
        plan.add(
            NodeSpec(
                key="context-refs",
                kind=spec.context_kind,
                data={"id": context.id, **refs},
                ownership=ownership,
            )
        )

    address_keys = {
        leg_type: _plan_leg_addresses(plan, spec, target, reads, namespaces, leg_type) for leg_type in leg_types
    }

    for exchange_name, a_type, z_type in exchange_specs_of(context.name.value, tenant_type, leg_types, namespaces):
        plan.add(
            NodeSpec(
                key=f"exchange:{exchange_name}",
                kind=TopologyRoutedExchange,
                data={
                    "name": exchange_name,
                    "vrf_a": {"id": context_vrf(members, vrfs, namespaces[a_type].name.value)},
                    "vrf_z": {"id": context_vrf(members, vrfs, namespaces[z_type].name.value)},
                    "gateway": {"id": context.id},
                    "status": "active",
                },
                hfid=Hfid(TopologyRoutedExchange.__name__, (exchange_name,)),
                ownership=ownership,
            )
        )

    # Exchange legs are derived (utils/service_contexts.py exchange_legs); nothing links a leg to its exchange
    for leg_type in leg_types:
        vrf_id = context_vrf(members, vrfs, namespaces[leg_type].name.value)
        for member in members:
            _plan_sub_interface(
                plan,
                member=member,
                leg_type=leg_type,
                vlan_id=reads.vlan_id,
                address=NodeRef(address_keys[leg_type][member.offset]),
                vrf_id=vrf_id,
                stored=reads.interfaces.get((member.device_id, leg_type)),
                ownership=ownership,
            )
    plan.add(
        EdgesSpec(
            node=context.id,
            relation="member_of_groups",
            peers=(Hfid("CoreStandardGroup", (GROUP_SERVICE_CONTEXTS,)),),
        )
    )


def _plan_leg_addresses(
    plan: TopologyPlan,
    spec: ContextSpec,
    target: ContextTarget,
    reads: LegsReads,
    namespaces: Mapping[str, Any],
    leg_type: str,
) -> dict[int, str]:
    """The /29 of a leg and its addresses at ``TRANSIT_OFFSETS``: stable per (context, namespace), so a re-run returns the
    same prefix; the prefix a leg already carries (authored or allocated) is reused and its addresses upserted. Returns the
    key of the address at each offset.
    """
    namespace = namespaces[leg_type]
    key = f"leg:{leg_type}"
    owner: Ownership = ownership_of(target)
    existing = reads.existing_prefixes.get(leg_type)
    if existing is None:
        plan.add(
            PrefixAllocationSpec(
                key=key,
                identifier=f"{reads.context.id}-{namespace.name.value}-{spec.leg_identifier}",
                prefix_length=TRANSIT_PREFIXLEN,
                role="technical",
                member_type="address",
                pool_name=reads.pool_names[leg_type],
                address_offsets=TRANSIT_OFFSETS,
                address_ownership="owned" if target.track else "shared",
            )
        )
        return {offset: f"{key}#{offset}" for offset in TRANSIT_OFFSETS}
    network = ipaddress.ip_network(existing.network, strict=False)
    keys: dict[int, str] = {}
    for offset in TRANSIT_OFFSETS:
        address = f"{network[offset]}/{network.prefixlen}"
        keys[offset] = f"{key}#{offset}"
        known = existing.address_ids.get(offset)
        plan.add(
            NodeSpec(
                key=keys[offset],
                kind="IpamIPAddress",
                data={
                    **({"id": known} if known else {}),
                    "address": address,
                    "ip_namespace": {"id": existing.namespace_id},
                },
                hfid=Hfid("IpamIPAddress", (address, existing.namespace_name)),
                ownership=owner,
            )
        )
    return keys


def _plan_sub_interface(
    plan: TopologyPlan,
    *,
    member: TransitMember,
    leg_type: str,
    vlan_id: int,
    address: NodeRef,
    vrf_id: str,
    stored: LegInterface | None,
    ownership: Ownership,
) -> None:
    """The sub-interface ``<uplink>.<tag>`` of a member on a leg: addressed, riding on the uplink (its
    ``parent`` and ``vlan_id``) and in the VRF instance ``vrf_id`` (the other instances it was in, ``stored``, are left).
    """
    tag = transit_vlan(vlan_id, leg_type)
    name = f"{member.trunk.name.value}.{tag}"
    key = f"sub:{member.name}:{leg_type}"
    plan.add(
        NodeSpec(
            key=key,
            kind=DcimVirtualInterface,
            data={
                "name": name,
                "device": {"id": member.device_id},
                "status": "active",
                "role": "service",
                "ip_address": address,
                "parent": {"id": member.trunk.id},
                "vlan_id": tag,
            },
            hfid=Hfid(DcimVirtualInterface.__name__, (member.name, name)),
            ownership=ownership,
        )
    )
    plan_interface_vrf(
        plan, NodeRef(key), vrf_id, stale_vrf_ids=(old for old in (stored.vrf_ids if stored else ()) if old != vrf_id)
    )
