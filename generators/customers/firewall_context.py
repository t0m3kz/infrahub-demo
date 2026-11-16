"""Service context provisioning (FirewallContext, LoadbalancerContext) shared by the customer boarding generators. A
ContextSpec holds what differs between kinds; legs, exchanges, SNAT and VIP rules are in domain-model.md. Per facility the
flow resolves the target (the cluster whose members host the context, or the customer's dedicated pair), reads the legs
(firewall_transit.py), plans the context and its legs (context_plan.py) and applies them under the context's lock.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from infrahub_sdk.protocols import CoreNumberPool

from generators.base import BaseGenerator
from generators.capabilities.ha import ContextSpec
from generators.capabilities.vrf import local_vrf_ids, vrfs_held
from generators.customers.context_plan import (
    VIP_POOL_KEY,
    LegsReads,
    context_vrf,
    exchange_specs_of,
    has_vlan,
    legs_of,
    needs_snat,
    plan_context,
    plan_legs,
)
from generators.customers.dedicated_pair import ensure_dedicated_pair
from generators.customers.firewall_context_spec import ContextTarget, TransitState
from generators.customers.firewall_transit import (
    context_named,
    dev_id,
    dev_name,
    existing_transit_prefix,
    read_leg_interfaces,
    read_namespaces,
    read_prefix_pools,
    read_snat_pool,
    resolve_transit_members,
    transit_state,
)
from generators.plan import EdgesSpec, NodeSpec, PruneEdgesSpec, TopologyPlan
from generators.protocols import DcimPhysicalDevice
from utils.exchange_transit import namespace_type_for_environment
from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE
from utils.firewall_split import is_external

__all__ = [
    "ContextOutcome",
    "ensure_transit_legs",
    "link_serving_context",
    "resolve_context_target",
    "serving_context_ids",
    "unlink_stale_serving_contexts",
]


@dataclass(frozen=True)
class ContextOutcome:
    """A context the flow built or found: its id and the border devices facing its members, whose VRF instances the
    caller converges once the legs are written (outside the context's lock).
    """

    context_id: str
    border_device_ids: frozenset[str]


def _physical_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parent's physical devices of a role, by name. devices(role__value: ...) also returns hosted virtual
    instances, so the shared cluster and the dedicated pair's hosts come from the physical ones in a stable order.
    """
    return sorted((d for d in devices if d.get("kind") == DcimPhysicalDevice.__name__), key=dev_name)


def _dedicated_name_parts(customer: dict[str, Any], customer_id: str) -> tuple[str, str]:
    """(owner, environment class) a dedicated device, HA and context are named from, e.g. ("c009", "prod"): owner org_id
    first, namespace class last, lowercase.
    """
    owner = customer.get("owner") or {}
    org_id = owner.get("org_id") or customer.get("name", customer_id)
    return org_id.lower(), namespace_type_for_environment(customer.get("environment") or "").replace("_", "")


def serving_context_ids(customer: dict[str, Any], spec: ContextSpec) -> list[str]:
    """The ids of the contexts of spec's kind the boarding query found the customer linked to. The firewall contexts of a
    footprint are one list; a spec owns those of its own role (utils/firewall_split.py ``is_external``), so the internal
    run never drops the edge link.
    """
    linked = customer.get(spec.serving_field) or []
    peers = [peer for peer in (linked if isinstance(linked, list) else [linked]) if peer.get("id")]
    if spec.serving_many:
        external = spec.role == EXTERNAL_FIREWALL_ROLE
        peers = [peer for peer in peers if is_external(peer) == external]
    return [peer["id"] for peer in peers]


async def link_serving_context(
    gen: BaseGenerator,
    spec: ContextSpec,
    *,
    customer_kind: str,
    customer_id: str,
    context_id: str,
    current_ids: list[str],
) -> None:
    """Point the deployment at the context of spec's kind that serves it (spec.serving_field); untracked. Nothing is read
    or written when ``context_id`` is in ``current_ids``; a many link is added when the deployment lacks it, a single one
    is replaced and a move re-runs spec.relink_generator for spec.relink_consumers.
    """
    if context_id in current_ids:
        return
    plan = TopologyPlan(root_id=None)
    previous_id = None
    if spec.serving_many:
        plan.add(EdgesSpec(customer_id, spec.serving_field, (context_id,), kind=customer_kind))
    else:
        holders = await gen.client.filters(kind=customer_kind, ids=[customer_id], include=[spec.serving_field])
        previous_id = getattr(holders[0], spec.serving_field).id if holders else None
        if previous_id == context_id:
            return
        plan.add(
            NodeSpec(
                key="serving",
                kind=customer_kind,
                data={"id": customer_id, spec.serving_field: {"id": context_id}},
                ownership="shared",
            )
        )
    await gen.apply_plan(plan)
    gen.logger.info(f"Deployment {customer_id} served by {spec.label} {context_id}")
    if not previous_id or spec.relink_consumers is None:
        return
    consumer_ids = await spec.relink_consumers(gen.client, customer_id)
    if consumer_ids:
        gen.logger.info(
            f"Deployment {customer_id} moved off {spec.label} {previous_id}: "
            f"re-running {spec.relink_generator} for {len(consumer_ids)} node(s)"
        )
        await gen.run_generator(spec.relink_generator, consumer_ids, wait=False)


async def unlink_stale_serving_contexts(
    gen: BaseGenerator,
    spec: ContextSpec,
    *,
    customer_kind: str,
    customer_id: str,
    current_ids: list[str],
    served_ids: list[str],
) -> None:
    """Drop the links of a many serving field to contexts this run did not serve the deployment from. A re-run converges
    the set (a zone that lost firewalls, a footprint moved dedicated <-> shared, a DC that lost its edge pair). Only the
    link goes; the shared context stays.
    """
    stale = [context_id for context_id in current_ids if context_id not in served_ids]
    if not stale:
        return
    # The links the caller read, less those this run served: only those go, the contexts stay
    plan = TopologyPlan(root_id=None)
    plan.add(
        PruneEdgesSpec(
            customer_kind,
            customer_id,
            spec.serving_field,
            keep_peer_ids=frozenset(served_ids),
            known_peer_ids=frozenset(current_ids),
        )
    )
    await gen.apply_plan(plan)
    gen.logger.info(f"Deployment {customer_id} no longer served by {spec.label} {stale}")


async def resolve_context_target(
    gen: BaseGenerator,
    spec: ContextSpec,
    customer: dict[str, Any],
    customer_id: str,
    parent: dict[str, Any],
    *,
    parent_generators: tuple[str, ...],
    controllers: list[dict[str, Any]],
) -> ContextTarget | None:
    """The cluster and ownership of this customer's context of spec's kind on parent (the DC or one zone of the metro);
    None (logged) when it has none to provision. The caller guarantees parent.id.
    """
    parent_id: str = parent.get("id", "")
    parent_name: str = parent.get("name", parent_id)
    deployment_id: str = parent.get("hosting_deployment_id") or parent_id
    edge_hosted = spec.role != EXTERNAL_FIREWALL_ROLE and bool(parent.get("external_firewall_devices"))
    skip_reason = spec.skip_reason(parent)
    if skip_reason:
        gen.logger.info(f"{parent_name}: {skip_reason} — skipping {spec.label} provisioning")
        return None

    # The devices arrive with the deployment's own GraphQL response, no separate filters()
    devices: list[Any] = _physical_devices(parent.get(spec.devices_key) or [])
    if not devices:
        gen.logger.info(f"{parent_name} has no {spec.role} devices — skipping {spec.label} provisioning")
        return None

    cluster = await _resolve_parent_cluster(
        gen, spec, devices, parent_id=parent_id, parent_name=parent_name, parent_generators=parent_generators
    )
    # The parent can have several HA clusters of the role; a context belongs to exactly one, so create sub-interfaces only
    # on its members
    member_ids = {peer.id for peer in cluster.capabilities.peers}
    devices = [d for d in devices if dev_id(d) in member_ids]
    if not devices:
        gen.fail(f"{parent_name}: no {spec.role} device resolved as a member of cluster '{cluster.name.value}'")

    dedicated = bool((customer.get("design") or {}).get(spec.dedicated_flag))
    # Ownership decides tracking. A dedicated context is reached by this customer's run only, so it is tracked. A shared
    # context and its legs are reached by EVERY customer, so they are written untracked. A dedicated request whose pair
    # cannot be provisioned fails the run, never a move onto the shared context.
    if not dedicated:
        return ContextTarget(
            spec=spec,
            name_base=cluster.name.value,
            deployment_id=deployment_id,
            tenant_id=None,
            track=False,
            devices=devices,
            parent_name=parent_name,
            edge_hosted=edge_hosted,
        )
    name_parts = _dedicated_name_parts(customer, customer_id)
    ha_name, members = await ensure_dedicated_pair(
        gen,
        role=spec.role,
        ha_kind=spec.ha_kind.__name__,
        physical_devices=devices,
        parent_id=parent_id,
        parent_name=parent_name,
        dc_design=(parent.get("design") or {}).get("name"),
        name_parts=name_parts,
        controllers=controllers,
    )
    # The dedicated cluster is `{owner}-{first}-{second}-ha-{class}`; the context name drops `-ha-{class}` and
    # ContextTarget.context_name puts the class last
    _, env_class = name_parts
    return ContextTarget(
        spec=spec,
        name_base=ha_name.removesuffix(f"-ha-{env_class}"),
        deployment_id=deployment_id,
        tenant_id=customer_id,
        track=True,
        devices=members,
        parent_name=parent_name,
        edge_hosted=edge_hosted,
    )


async def _resolve_parent_cluster(
    gen: BaseGenerator,
    spec: ContextSpec,
    devices: list[Any],
    *,
    parent_id: str,
    parent_name: str,
    parent_generators: tuple[str, ...],
) -> Any:
    """The parent's spec.ha_kind cluster holding devices[0], the first physical device of the role by name. Never pairs
    the devices: the parent's own generator (``parent_generators``) owns the HA pair, or a customer run would claim it and
    delete it next run. An unpaired parent means that generator has not finished: wait for it, re-query, and fail loudly
    if still unpaired.
    """
    cluster = await _find_parent_cluster(gen, spec, devices)
    if cluster is not None:
        return cluster
    gen.logger.info(
        f"{parent_name}: {spec.role} device(s) not yet HA-paired — waiting for "
        f"{'/'.join(parent_generators)} before re-checking"
    )
    await gen.wait_for_parent_generators(parent_generators, parent_id)
    cluster = await _find_parent_cluster(gen, spec, devices)
    if cluster is None:
        gen.fail(
            f"{parent_name}: {spec.role} device(s) {sorted(dev_name(d) for d in devices)} are not HA-paired "
            f"into a {spec.ha_kind.__name__} cluster — run {'/'.join(parent_generators)} for {parent_name} first"
        )
    return cluster


async def _find_parent_cluster(gen: BaseGenerator, spec: ContextSpec, devices: list[Any]) -> Any | None:
    """One spec.ha_kind lookup by devices[0]'s membership; None when unpaired."""
    clusters = await gen.client.filters(
        kind=spec.ha_kind, capabilities__ids=[dev_id(devices[0])], include=["capabilities"]
    )
    return clusters[0] if clusters else None


async def ensure_transit_legs(
    gen: BaseGenerator, *, customer: dict[str, Any], customer_id: str, customer_kind: str, target: ContextTarget
) -> ContextOutcome:
    """The context and its legs for the VRFs this customer is in. A leg is each member's `<uplink>.<transit_vlan>`
    sub-interface from a /29 in that VRF; firewall legs are joined by one TopologyRoutedExchange per pair. Shared
    (track=False) is found by reads alone; dedicated (track=True) is re-saved. Writes run under the context's lock,
    rechecked inside it, and fail before the first one when a prerequisite is missing.
    """
    spec = target.spec
    tenant_type = namespace_type_for_environment(customer.get("environment") or "")
    leg_types = legs_of(spec, target, tenant_type)
    namespaces = await read_namespaces(gen, leg_types)
    context_name = target.context_name(namespaces[tenant_type].name.value)
    members = await resolve_transit_members(gen, spec, target.devices, context_name=context_name)
    if not members:
        gen.fail(f"{context_name}: no {spec.role} member has an uplink cabled to a border service port")

    needs = {member.bl_port.device.id: {namespaces[t].name.value for t in leg_types} for member in members}
    async with vrfs_held(gen, needs):
        vrfs = await local_vrf_ids(gen, needs, [zone.name.value for zone in namespaces.values()])
        # One instance per leg zone, or nothing is written
        for leg_type in leg_types:
            context_vrf(members, vrfs, namespaces[leg_type].name.value)
        context_id = await _write_context(
            gen,
            customer=customer,
            customer_id=customer_id,
            customer_kind=customer_kind,
            target=target,
            tenant_type=tenant_type,
            members=members,
            namespaces=namespaces,
            leg_types=leg_types,
            vrfs=vrfs,
            context_name=context_name,
        )
    return ContextOutcome(context_id=context_id, border_device_ids=frozenset(needs))


async def _write_context(
    gen: BaseGenerator,
    *,
    customer: dict[str, Any],
    customer_id: str,
    customer_kind: str,
    target: ContextTarget,
    tenant_type: str,
    members: list[Any],
    namespaces: dict[str, Any],
    leg_types: tuple[str, ...],
    vrfs: dict[tuple[str, str], str],
    context_name: str,
) -> str:
    """Write the context and its legs with the border-device VRFs held, before taking the context lock; returns the
    context's id.
    """
    spec = target.spec
    exchange_names = [name for name, _, _ in exchange_specs_of(context_name, tenant_type, leg_types, namespaces)]
    snat = needs_snat(target, leg_types)
    served_by = serving_context_ids(customer, spec)

    async def link(context_id: str) -> None:
        await link_serving_context(
            gen,
            spec,
            customer_kind=customer_kind,
            customer_id=customer_id,
            context_id=context_id,
            current_ids=served_by,
        )

    async def read_state() -> TransitState:
        return await transit_state(
            gen, spec, context_name, members, namespaces, leg_types, exchange_names, needs_snat=snat
        )

    # A complete shared context is found by reads alone, with no lock
    if not target.track and (found := await read_state()).complete and found.context is not None:
        await link(found.context.id)
        return found.context.id

    async with gen.plan_locks(f"{spec.key}-{context_name}"):
        # Re-read under the lock: a concurrent run may have completed it. A dedicated context re-saves every leg, so it
        # needs only the context
        state = await context_named(gen.client, spec, context_name) if target.track else await read_state()
        if state.complete and state.context is not None:
            await link(state.context.id)
            return state.context.id
        context = state.context

        # Everything the plans need, read before the first write
        pool_names = {leg_type: spec.leg_pool_name(ns.name.value) for leg_type, ns in namespaces.items()}
        if spec.vip_pool_name is not None:
            pool_names[VIP_POOL_KEY] = spec.vip_pool_name(namespaces[tenant_type].name.value)
        pools = await read_prefix_pools(gen, pool_names, context_name=context_name)
        if snat:
            await read_snat_pool(gen)
        vlan_pool_id = None if has_vlan(context) else await _read_vlan_pool(gen, spec, target)
        leg_reads = None
        if has_vlan(context):
            leg_reads = await _read_legs(gen, context, members, namespaces, leg_types, pools)

        context_plan = TopologyPlan(root_id=None)
        plan_context(
            context_plan, spec=spec, target=target, name=context_name, existing=context, vlan_pool_id=vlan_pool_id
        )
        legs_plan = TopologyPlan(root_id=None)
        if leg_reads is not None:
            plan_legs(
                legs_plan,
                spec=spec,
                target=target,
                reads=leg_reads,
                tenant_type=tenant_type,
                members=members,
                namespaces=namespaces,
                leg_types=leg_types,
                vrfs=vrfs,
            )
        await gen.apply_plan(context_plan)
        if leg_reads is None:
            # A context just given its VLAN has no leg yet: read what the VLAN drew, and plan the legs on it
            context = (await context_named(gen.client, spec, context_name)).context
            if context is None:
                gen.fail(f"{spec.label} '{context_name}' is not there after it was written")
            leg_reads = LegsReads(
                context=context,
                vlan_id=context.vlan_id.value,
                pool_names=pool_names,
                existing_prefixes={},
                interfaces={},
            )
            plan_legs(
                legs_plan,
                spec=spec,
                target=target,
                reads=leg_reads,
                tenant_type=tenant_type,
                members=members,
                namespaces=namespaces,
                leg_types=leg_types,
                vrfs=vrfs,
            )
        await link(leg_reads.context.id)
        await gen.apply_plan(legs_plan)
        return leg_reads.context.id


async def _read_vlan_pool(gen: BaseGenerator, spec: ContextSpec, target: ContextTarget) -> str:
    """The id of the parent's `{key}-vlan-pool`, which a context without a VLAN draws from; fails when it is missing."""
    pool_name = f"{target.parent_name.lower()}-{spec.key}-vlan-pool"
    try:
        return (await gen.client.get(kind=CoreNumberPool, name__value=pool_name)).id
    except Exception as exc:
        gen.fail(f"Cannot find {spec.label} VLAN pool '{pool_name}' for {target.parent_name}: {exc}")


async def _read_legs(
    gen: BaseGenerator,
    context: Any,
    members: list[Any],
    namespaces: dict[str, Any],
    leg_types: tuple[str, ...],
    pools: dict[str, Any],
) -> LegsReads:
    """What the legs of a context with a VLAN already have: the prefix each addressed leg carries and the stored
    sub-interface of each member and leg.
    """
    vlan_id = context.vlan_id.value
    existing = {}
    for leg_type in leg_types:
        prefix = await existing_transit_prefix(
            gen,
            namespace=namespaces[leg_type],
            pool=pools[leg_type],
            members=members,
            vlan_id=vlan_id,
            leg_type=leg_type,
        )
        if prefix is not None:
            existing[leg_type] = prefix
    return LegsReads(
        context=context,
        vlan_id=vlan_id,
        pool_names={key: pool.name.value for key, pool in pools.items()},
        existing_prefixes=existing,
        interfaces=await read_leg_interfaces(gen, members, vlan_id, leg_types),
    )
