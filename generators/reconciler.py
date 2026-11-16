"""The one writer of a ``TopologyPlan`` (generators/plan.py): persists it idempotently, in dependency order.

Steps run in the order of ``STEPS``: track, devices, customer pools, retire, nodes, edges, node edges, prune. A topology
that needs another spec kind adds a step with ``register_step``; nothing else changes. Reads are batched (one
``filters`` per kind); writes of one kind that do not depend on each other run under a semaphore.

The customer pools and slices call ``generators/ipam/customer_pool.py`` with the generator. The fabric flows (DC, pod,
rack, MLAG, colocation, office) are ``NodeSpec`` and the allocations, written by the ``nodes`` step.

Before the first write ``apply`` checks the plan against the schema (``generators/schema_check.py``: unknown kinds and
fields, peers of the wrong kind, a mandatory field a created object lacks) and raises ``GeneratorError`` naming every
offending spec.

The ``nodes`` step writes ``NodeSpec`` and the allocations (``AddressAllocationSpec``, ``LinkPrefixSpec``,
``PrefixAllocationSpec``) by dependency,
not by plan order: ``plan_graph.schedule`` derives the order from what the specs reference (``NodeRef``, ``Hfid``, the
children a spec ``provides``). Each dependency level is written as one bounded concurrent wave, after the allocations
of that level (run one by one: they draw from a shared pool). Before any write, ``apply`` checks that the objects the
plan references and does not write exist, waiting for the generators named in ``TopologyPlan.producers``.

``PrefixAllocationSpec`` is such an allocation: a prefix (and the addresses of it) from a tenant pool or a customer pool
(found, created or grown by a block, ``generators/ipam/customer_pool.py``). It takes no lock: a plan that decides from
a pool's state is applied inside ``plan_locks`` of the generator, around the builder's reads and this ``apply``.
``NodeSpec.create_only`` is sent only when the object is not there (one read by HFID per level), ``NodeSpec.replaces``
and ``RetireSpec`` delete the objects they name before the nodes of their level, and only when a generator's tracking
group holds them: anything else in the way fails the run.

Tracking: ``apply`` first adds the plan root to the run's group context (SDK issue opsmill/infrahub-sdk-python#572), then
every save passes ``tracked_save_kwargs(spec.tracked)``, so an untracked object (a shared catalogue object) is never a
``delete_unused_nodes`` candidate.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNumberPool

from generators.helpers.common import call_with_node_not_found_retry, gather_bounded, tracked_save_kwargs
from generators.helpers.edges import add_edges, remove_edge
from generators.ipam import allocation
from generators.ipam.addresses import upsert_p2p_addresses, upsert_prefix_addresses
from generators.ipam.customer_pool import (
    allocate_customer_prefix,
    allocate_customer_slice,
    allocate_segment_prefix,
    ensure_customer_pools,
)
from generators.plan import (
    HFID_FIELDS,
    AddEdgesSpec,
    AddressAllocationSpec,
    CustomerPoolsSpec,
    CustomerSliceSpec,
    DeviceSpec,
    EdgesSpec,
    ExternalProducer,
    Hfid,
    LinkPrefixSpec,
    NodeRef,
    NodeSpec,
    PrefixAllocationSpec,
    PruneEdgesSpec,
    PruneSpec,
    RetireSpec,
    RoutingTagSpec,
    TopologyPlan,
    TrackSpec,
)
from generators.plan_graph import external_hfids, lookup_hfid_nodes, lookup_hfids, render_hfids, schedule
from generators.protocols import DcimPhysicalDevice, DcimPhysicalInterface
from generators.schema_check import candidates_missing_mandatory, check_nodes, missing_mandatory, raise_problems
from utils.limits import WAIT_FOR_PEER_GENERATOR, WRITE_CONCURRENCY

_TEMPLATE_KIND = "TemplateDcimPhysicalDevice"
_GENERATOR_GROUP_KINDS = frozenset({"CoreGeneratorGroup", "CoreGeneratorAwareGroup"})


@dataclass
class ReconcileResult:
    """What a run wrote or found, for the generator's later steps (routing, circuits)."""

    device_ids: dict[str, str] = field(default_factory=dict)
    interface_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    prefixes: dict[str, Any] = field(default_factory=dict)
    nodes: dict[str, Any] = field(default_factory=dict)
    ids: dict[Hfid, str] = field(default_factory=dict)
    routing_tags: dict[str, int] = field(default_factory=dict)


def resolve_refs(value: Any, result: ReconcileResult) -> Any:
    """``value`` with every ``NodeRef`` replaced by the id of the node saved under its key."""
    if isinstance(value, NodeRef):
        return result.nodes[value.key].id
    if isinstance(value, Mapping):
        return {key: resolve_refs(item, result) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [resolve_refs(item, result) for item in value]
    return value


Handler = Callable[["PlanReconciler", TopologyPlan, ReconcileResult], Awaitable[None]]


@dataclass(frozen=True)
class Step:
    """One reconcile step: runs ``handler`` when the plan holds a spec of one of ``spec_types`` (or ``always``)."""

    name: str
    spec_types: tuple[type, ...]
    handler: Handler


STEPS: list[Step] = []


def register_step(step: Step, *, before: str | None = None) -> None:
    """Add ``step`` at the end of ``STEPS``, or just before the step named ``before`` (a new kind goes before prune)."""
    if any(existing.name == step.name for existing in STEPS):
        raise ValueError(f"Step {step.name!r} is already registered")
    if before is None:
        STEPS.append(step)
        return
    STEPS.insert(next(index for index, existing in enumerate(STEPS) if existing.name == before), step)


class PlanReconciler:
    """Persists a plan through ``generator`` (client, logger, and for the steps that need them ``resolve_pool``,
    ``resource_lock``). ``WRITE_CONCURRENCY`` bounds the parallel writes of a kind.
    """

    def __init__(self, generator: Any) -> None:
        self.generator = generator
        self.client = generator.client
        self.logger = generator.logger
        self.sem = asyncio.Semaphore(WRITE_CONCURRENCY)

    async def apply(self, plan: TopologyPlan) -> ReconcileResult:
        """Track the root, validate, then run every step the plan has specs for."""
        # First, even for an empty plan: a run that saves nothing must still be a member of its own group
        if plan.root_id is not None:
            related_ids = self.client.group_context.related_node_ids
            if plan.root_id not in related_ids:
                related_ids.append(plan.root_id)
        plan.validate()
        result = ReconcileResult()
        await self._check_externals(plan, result)
        await self._check_schema(plan)
        for step in STEPS:
            if any(plan.of(spec_type) for spec_type in step.spec_types):
                await step.handler(self, plan, result)
        return result

    # devices

    async def _reconcile_devices(self, plan: TopologyPlan, result: ReconcileResult) -> None:
        specs = plan.of(DeviceSpec)
        nodes = await self.client.filters(kind=DcimPhysicalDevice, name__values=[spec.name for spec in specs])
        if len(specs) == 1:
            # The filter names the device, so the one answer is it
            existing = {specs[0].name: node for node in nodes[:1]}
        else:
            existing = {node.name.value: node for node in nodes}

        template_ids = await self._template_ids(
            sorted({s.template_name for s in specs if s.template_name and s.name not in existing})
        )

        async def upsert(spec: DeviceSpec) -> tuple[str, str]:
            async with self.sem:
                data: dict[str, Any] = {"name": spec.name, "status": "active"}
                if spec.description is not None:
                    data["description"] = spec.description
                for relationship, peer_id in (("owner", spec.owner_id), ("deployment", spec.deployment_id)):
                    if peer_id:
                        data[relationship] = {"id": peer_id}
                node = existing.get(spec.name)
                if node is not None:
                    data["id"] = node.id
                elif spec.template_name:
                    # object_template only on first creation: re-sending it to an existing device fails server-side
                    data["object_template"] = {"id": template_ids[spec.template_name]}
                saved = await self.client.create(kind=DcimPhysicalDevice, data=data)
                await saved.save(**tracked_save_kwargs(True))
                return spec.name, saved.id

        result.device_ids.update(await asyncio.gather(*(upsert(spec) for spec in specs)))
        await self._discover_interfaces(specs, result)

    async def _template_ids(self, names: list[str]) -> dict[str, str]:
        """Ids of the templates new devices need, in one read; a missing one fails before any device is written."""
        if not names:
            return {}
        if len(names) == 1:
            # The filter names the template, so the one answer is it
            templates = await self.client.filters(kind=_TEMPLATE_KIND, template_name__value=names[0])
            found = {names[0]: templates[0].id} if templates else {}
        else:
            templates = await self.client.filters(kind=_TEMPLATE_KIND, template_name__values=names)
            found = {template.template_name.value: template.id for template in templates}
        for name in names:
            if name not in found:
                self.generator.fail(f"Template '{name}' not found — cannot create its device")
        return found

    async def _discover_interfaces(self, specs: list[DeviceSpec], result: ReconcileResult) -> None:
        """Read back the ports the templates created: one read per role of the specs that ask for it."""
        groups: dict[str, list[str]] = defaultdict(list)
        for spec in specs:
            if spec.discover_interface_role is not None:
                groups[spec.discover_interface_role].append(spec.name)
        for role, names in groups.items():
            names_by_id = {result.device_ids[name]: name for name in names}
            interfaces = await self.client.filters(
                kind=DcimPhysicalInterface, device__ids=list(names_by_id), role__value=role
            )
            for iface in interfaces:
                # One device: every port is its own, whatever the node says
                device_name = names[0] if len(names) == 1 else names_by_id.get(iface.device.id)
                if device_name is not None:
                    result.interface_ids[(device_name, iface.name.value)] = iface.id

    # customer pools and slices

    async def _reconcile_customer_pools(self, plan: TopologyPlan, result: ReconcileResult) -> None:  # noqa: ARG002
        """Board each footprint, then allocate the slices (customer pool, grown by a block when full)."""
        for pools in plan.of(CustomerPoolsSpec):
            await ensure_customer_pools(
                self.generator,
                kind=pools.kind,
                org_id=pools.org_id,
                environment=pools.environment,
                design=dict(pools.design),
                footprint_name=pools.footprint_name,
            )
        for spec in plan.of(CustomerSliceSpec):
            result.prefixes[spec.identifier] = await allocate_customer_slice(
                self.generator, spec.identifier, spec.pool, spec.global_pool, spec.block_length, spec.length
            )

    async def _add_edges(self, plan: TopologyPlan, result: ReconcileResult) -> None:
        for spec in plan.of(AddEdgesSpec):
            await add_edges(
                self.client,
                node_id=resolve_refs(spec.node_id, result),
                relation=spec.relation,
                peer_ids=[result.prefixes[spec.peer_prefix].id],
            )

    # nodes, routing tags and edges

    async def _save_nodes(self, plan: TopologyPlan, result: ReconcileResult) -> None:
        """Write the graph specs level by level (``plan_graph.schedule``) and draw the routing tags where the plan puts
        them: a routing tag (a pool allocation) ends the specs before it.
        """
        for step in schedule(plan.specs):
            if isinstance(step, RoutingTagSpec):
                await self._draw_routing_tag(step, result)
            else:
                await self._write_level(step, result)

    async def _write_level(self, level: list[Any], result: ReconcileResult) -> None:
        """One dependency level: the allocations one by one, then its nodes as one bounded wave, then the ids of the
        children the nodes provide (the ports a device template created).
        """
        for spec in level:
            if isinstance(spec, AddressAllocationSpec):
                result.nodes[spec.key] = await self._allocate_address(spec, result)
            elif isinstance(spec, LinkPrefixSpec):
                await self._allocate_link(spec, result)
            elif isinstance(spec, PrefixAllocationSpec):
                await self._allocate_prefix(spec, result)
        nodes = [spec for spec in level if isinstance(spec, NodeSpec)]
        await self._retire([hfid for spec in nodes for hfid in spec.replaces])
        found = await self._find_create_only(nodes)
        # A pool draw is one by one: two concurrent draws of one pool race for the same number
        for spec in (spec for spec in nodes if spec.sequential):
            await self._record(spec, await self._save_node(spec, result, found.get(spec.key)), result)
        wave = [spec for spec in nodes if not spec.sequential]
        saved = await gather_bounded(
            (self._save_node(spec, result, found.get(spec.key)) for spec in wave), sem=self.sem
        )
        for spec, node in zip(wave, saved, strict=True):
            await self._record(spec, node, result)
        provided = [hfid for spec in nodes for hfid in spec.provides]
        if provided:
            await self._await_hfids(provided, result, [], "provided by a saved device")

    async def _record(self, spec: NodeSpec, node: Any, result: ReconcileResult) -> None:
        """Keep the saved node under its key and its HFID."""
        result.nodes[spec.key] = node
        if spec.hfid is not None:
            result.ids[spec.hfid] = node.id

    async def _pool(
        self, ref: str | NodeRef | Hfid | None, kind: type[Any], result: ReconcileResult, name: str | None = None
    ) -> Any:
        """The pool node a spec draws from: the pool the plan saved (``NodeRef``), one that exists by HFID or by id (read
        once per run), or by ``name``.
        """
        if isinstance(ref, NodeRef):
            node = result.nodes[ref.key]
            if hasattr(node, "resources"):
                return node
            # A pool the plan found and did not write (a spec with nothing to update): read it by id
            ref = node.id
        elif isinstance(ref, Hfid):
            ref = result.ids[ref]
        return await self.generator.resolve_pool(provided=ref, kind=kind, fallback_name=name)

    async def _allocate_address(self, spec: AddressAllocationSpec, result: ReconcileResult) -> Any:
        pool = await self._pool(spec.pool_id, CoreIPAddressPool, result, spec.pool_name)
        return await allocation.allocate_address(
            self.client, pool, spec.identifier, spec.prefix_length, data={"description": spec.description}
        )

    async def _allocate_link(self, spec: LinkPrefixSpec, result: ReconcileResult) -> None:
        """The P2P prefix of a link, keyed by the sorted ids of its two ports, and both addresses."""
        identifier = "__".join(sorted(result.ids[end] for end in spec.ends))
        pool = await self._pool(spec.pool_id, CoreIPPrefixPool, result)
        prefix = await allocation.allocate_prefix(
            self.client,
            pool,
            identifier,
            spec.prefix_length,
            member_type="address",
            data={"role": "technical", "is_pool": True},
        )
        self.logger.info(f"- Allocated prefix {prefix.display_label} for {spec.key}")
        result.nodes[spec.key] = prefix
        for key, address in zip(
            spec.address_keys,
            await upsert_p2p_addresses(self.client, prefix, track=spec.ownership == "owned"),
            strict=True,
        ):
            result.nodes[key] = address

    async def _allocate_prefix(self, spec: PrefixAllocationSpec, result: ReconcileResult) -> None:
        """One prefix from the parent pool, or from a customer pool (found, created or grown, ``spec.grow``), and the
        addresses of it the spec asks for; the same identifier gives the same prefix. The caller of ``apply_plan`` holds
        the locks of a pool it chooses from (``plan_locks``).
        """
        identifier = spec.identifier
        if spec.identifier_ref is not None:
            identifier = f"{identifier}__{result.ids[spec.identifier_ref]}"
        if spec.grow is not None and spec.pool_name is not None:
            prefix = await allocate_customer_prefix(
                self.generator,
                identifier,
                spec.pool_name,
                spec.grow.global_pool,
                spec.grow.block_length,
                spec.prefix_length,
                data=spec.data,
                member_type=spec.member_type,
                label=spec.label,
            )
        else:
            pool = await self._pool(spec.parent, CoreIPPrefixPool, result, spec.pool_name)
            prefix = await allocate_segment_prefix(
                self.generator,
                pool,
                identifier,
                spec.prefix_length,
                data=spec.data,
                member_type=spec.member_type,
                label=spec.label,
            )
            if prefix is None:
                self.generator.fail(
                    f"{spec.label or spec.key}: pool {spec.pool_name or spec.parent} returned no prefix"
                )
        result.nodes[spec.key] = prefix
        if spec.track_prefix:
            self._track([prefix.id])
        if spec.address_offsets:
            addresses = await upsert_prefix_addresses(
                self.client,
                prefix,
                offsets=spec.address_offsets,
                address_length=spec.address_length,
                description=spec.address_description,
                track=spec.address_ownership == "owned",
            )
            for offset in spec.address_offsets:
                result.nodes[spec.address_key(offset)] = addresses[offset]

    # objects a plan removes: generated ones only

    async def _retire_specs(self, plan: TopologyPlan, result: ReconcileResult) -> None:  # noqa: ARG002
        await self._retire([hfid for spec in plan.of(RetireSpec) for hfid in spec.hfids])

    async def _retire(self, hfids: list[Hfid]) -> None:
        """Delete the objects among ``hfids`` that exist, when a generator tracks them. One that no generator tracks (a
        hand-written one) is never deleted: the run fails naming it, since what it makes room for cannot be written.
        """
        if not hfids:
            return
        found = await lookup_hfid_nodes(self.client, hfids, include=["member_of_groups"])
        for hfid, node in found.items():
            if not any(peer.typename in _GENERATOR_GROUP_KINDS for peer in node.member_of_groups.peers):
                self.generator.fail(
                    f"{hfid.kind} {'/'.join(hfid.parts)} is in the way and no generator tracks it: "
                    "it is not deleted, remove it by hand"
                )
            await node.delete()
            self.logger.info(f"Removed {hfid.kind} {'/'.join(hfid.parts)}")

    async def _find_create_only(self, nodes: list[NodeSpec]) -> dict[str, str]:
        """{key: id} of the nodes with ``create_only`` fields that exist already (one read per kind)."""
        wanted = {spec.key: spec.hfid for spec in nodes if spec.create_only and spec.hfid is not None}
        found = await lookup_hfids(self.client, wanted.values()) if wanted else {}
        return {key: found[hfid] for key, hfid in wanted.items() if hfid in found}

    # the plan against the schema

    async def _check_schema(self, plan: TopologyPlan) -> None:
        """Before the first write: every ``NodeSpec`` against the schema of its kind (``generators/schema_check.py``), the
        schema loaded once. A spec that lacks a mandatory field is a problem only when its object does not exist, which one
        read per kind of such specs settles.
        """
        if not plan.of(NodeSpec):
            return
        schema = await self.client.schema.all()
        problems = check_nodes(plan.specs, schema)
        candidates = candidates_missing_mandatory(plan.specs, schema)
        wanted = [spec.hfid for spec in plan.of(NodeSpec) if spec.key in candidates and spec.hfid is not None]
        found = await lookup_hfids(self.client, wanted) if wanted else {}
        problems.extend(missing_mandatory(plan.specs, found, candidates))
        raise_problems(type(self.generator).__name__, problems)

    # references to objects the plan does not write

    async def _check_externals(self, plan: TopologyPlan, result: ReconcileResult) -> None:
        """Before the first write: every object the plan references or updates and does not write exists (one read per
        kind), waited for while a generator of ``plan.producers`` may still be creating it.
        """
        wanted = external_hfids(plan.specs)
        if wanted:
            await self._await_hfids(wanted, result, plan.producers, "created by another generator")

    async def _await_hfids(
        self, wanted: list[Hfid], result: ReconcileResult, producers: list[ExternalProducer], origin: str
    ) -> None:
        """Read the ids of ``wanted`` into ``result.ids``, retrying with the shared policy while some are missing. Between
        tries the producers are waited for: a run in flight is awaited, a last run that failed fails this run at once.
        Fails naming what is still missing.
        """
        missing = [hfid for hfid in wanted if hfid not in result.ids]
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            result.ids.update(await lookup_hfids(self.client, missing))
            missing = [hfid for hfid in wanted if hfid not in result.ids]
            if not missing:
                return
            waited = False
            for producer in producers:
                waited = (
                    await self.generator.wait_for_parent_generators(producer.generators, producer.parent_id) or waited
                )
            if attempt == WAIT_FOR_PEER_GENERATOR.attempts - 1:
                break
            if not waited:
                delay = self.generator._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
                self.logger.info(
                    f"{len(missing)} object(s) {origin} not found yet, first {missing[0].kind} {list(missing[0].parts)} — "
                    f"retrying in {delay:.2f}s (attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
                )
                await asyncio.sleep(delay)
        names = ", ".join(f"{hfid.kind} {'/'.join(hfid.parts)}" for hfid in missing)
        creators = sorted({name for producer in producers for name in producer.generators})
        hint = f" (expected from generator {', '.join(creators)})" if creators else ""
        self.generator.fail(f"Not found after {WAIT_FOR_PEER_GENERATOR.attempts} attempts, {origin}{hint}: {names}")

    async def _save_node(self, spec: NodeSpec, result: ReconcileResult, found_id: str | None = None) -> Any:
        """Write one node. ``create_only`` is merged into ``data`` unless the pre-read found the object (``found_id``):
        an upsert by HFID without an id is validated as a create, so an existing object is updated by id, without the
        create-only fields; an untracked one that has nothing else to write is not written at all.
        """
        data = render_hfids(resolve_refs(spec.data, result))
        if found_id is None:
            data = {**data, **render_hfids(resolve_refs(spec.create_only, result))}
        else:
            data = {**data, "id": found_id}
            identity = {field.partition("__")[0] for field in HFID_FIELDS[spec.hfid.kind]} if spec.hfid else set()
            if not spec.tracked and not spec.clear and set(data) <= identity | {"id"}:
                return SimpleNamespace(id=found_id)
        node = await self._created(spec, data)
        try:
            await node.save(**tracked_save_kwargs(spec.tracked))
        except GraphQLError:
            if not spec.retry_on_conflict:
                raise
            node = await self._created(spec, data)
            await node.save(**tracked_save_kwargs(spec.tracked))
        return node

    async def _created(self, spec: NodeSpec, data: Mapping[str, Any]) -> Any:
        """The SDK node of ``spec`` to save, with the relationships of ``spec.clear`` set to none."""
        node = await self.client.create(kind=spec.kind, data=data)
        for relation in spec.clear:
            setattr(node, relation, None)
        return node

    def _track(self, ids: Iterable[str]) -> None:
        """Join the run's group context without a write."""
        related_ids = self.client.group_context.related_node_ids
        related_ids.extend(node_id for node_id in ids if node_id not in related_ids)

    async def _track_existing(self, plan: TopologyPlan, result: ReconcileResult) -> None:  # noqa: ARG002
        for spec in plan.of(TrackSpec):
            self._track(spec.ids)

    async def _draw_routing_tag(self, spec: RoutingTagSpec, result: ReconcileResult) -> None:
        pool = await self.client.get(kind=CoreNumberPool, name__value=spec.pool_name, raise_when_missing=False)
        if pool is None:
            self.generator.fail(
                f"Routing tag pool {spec.pool_name} not found - load data/bootstrap/19c_routing_tag_pools.yml"
            )
        consumer = await self.client.create(
            kind=spec.kind, data={"id": spec.node_id, "routing_tag": {"from_pool": {"id": pool.id}}}
        )
        await consumer.save(allow_upsert=True, update_group_context=False)
        result.routing_tags[spec.node_id] = int(consumer.routing_tag.value)

    async def _add_node_edges(self, plan: TopologyPlan, result: ReconcileResult) -> None:
        """One RelationshipAdd per ``EdgesSpec``; the other peers of the relation stay. The specs of different
        (node, relation) pairs are independent and run together; the ones of one pair run in plan order.
        """
        by_holder: dict[tuple[str, str], list[EdgesSpec]] = defaultdict(list)
        for spec in plan.of(EdgesSpec):
            by_holder[(resolve_refs(spec.node, result), spec.relation)].append(spec)

        async def add_all(node_id: str, specs: list[EdgesSpec]) -> None:
            for spec in specs:
                await self._add_node_edge(spec, node_id, result)

        await gather_bounded(
            (add_all(node_id, specs) for (node_id, _relation), specs in by_holder.items()), sem=self.sem
        )

    async def _add_node_edge(self, spec: EdgesSpec, node_id: str, result: ReconcileResult) -> None:
        peer_ids = list(
            dict.fromkeys(
                result.ids[peer] if isinstance(peer, Hfid) else resolve_refs(peer, result) for peer in spec.peers
            )
        )
        if spec.kind is not None:
            holders = await self.client.filters(kind=spec.kind, ids=[node_id], include=[spec.relation])
            have = {peer.id for holder in holders for peer in getattr(holder, spec.relation).peers}
            peer_ids = [peer_id for peer_id in peer_ids if peer_id not in have]
        if not peer_ids:
            return
        if spec.retry_not_found:
            await call_with_node_not_found_retry(
                lambda: add_edges(self.client, node_id=node_id, relation=spec.relation, peer_ids=peer_ids),
                self.logger,
                f"adding {spec.relation} to {node_id}",
            )
        else:
            await add_edges(self.client, node_id=node_id, relation=spec.relation, peer_ids=peer_ids)

    # prune

    async def _prune(self, plan: TopologyPlan, result: ReconcileResult) -> None:
        """Remove what the old state has and the plan does not: nodes (``PruneSpec``) and edges (``PruneEdgesSpec``)."""
        for spec in plan.of(PruneSpec):
            for node in await self.client.filters(kind=spec.kind, **resolve_refs(spec.scope, result)):
                value = node.id if spec.key == "id" else getattr(node, spec.key).value
                if value not in spec.keep:
                    await node.delete()
                    self.logger.info(f"Pruned {spec.kind} {value}")
        for edges in plan.of(PruneEdgesSpec):
            holder_id = resolve_refs(edges.holder_id, result)
            if edges.known_peer_ids is not None:
                # The caller read the peers: detach those it no longer wants, edge by edge, with no second read
                stale = sorted(edges.known_peer_ids - edges.keep_peer_ids)
                for peer_id in stale:
                    await remove_edge(self.client, node_id=holder_id, relation=edges.relationship, peer_id=peer_id)
                if stale:
                    self.logger.info(f"Detached {len(stale)} stale peer(s) from {holder_id}.{edges.relationship}")
                continue
            holders = await self.client.filters(kind=edges.holder_kind, ids=[holder_id], include=[edges.relationship])
            for holder in holders:
                stale = [
                    peer.id for peer in getattr(holder, edges.relationship).peers if peer.id not in edges.keep_peer_ids
                ]
                if stale:
                    await holder.remove_relationships(edges.relationship, stale)
                    self.logger.info(f"Detached {len(stale)} stale peer(s) from {holder_id}.{edges.relationship}")


for _step in (
    Step("devices", (DeviceSpec,), PlanReconciler._reconcile_devices),
    Step("prune", (PruneSpec, PruneEdgesSpec), PlanReconciler._prune),
):
    register_step(_step)

# The steps, each at its place in the order of ``STEPS``
for _step, _before in (
    (Step("track", (TrackSpec,), PlanReconciler._track_existing), "devices"),
    (
        Step("customer_pools", (CustomerPoolsSpec, CustomerSliceSpec), PlanReconciler._reconcile_customer_pools),
        "prune",
    ),
    (Step("retire", (RetireSpec,), PlanReconciler._retire_specs), "prune"),
    (
        Step(
            "nodes",
            (NodeSpec, RoutingTagSpec, AddressAllocationSpec, LinkPrefixSpec, PrefixAllocationSpec),
            PlanReconciler._save_nodes,
        ),
        "prune",
    ),
    # After the nodes: a slice edge may land on a node the plan has just saved (``NodeRef``)
    (Step("edges", (AddEdgesSpec,), PlanReconciler._add_edges), "prune"),
    (Step("node_edges", (EdgesSpec,), PlanReconciler._add_node_edges), "prune"),
):
    register_step(_step, before=_before)
