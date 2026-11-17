"""The dependency graph of the specs a reconciler writes object by object (``NodeSpec``, ``AddressAllocationSpec``,
``LinkPrefixSpec``): which spec must be written before which, derived from what the specs reference (generators/plan.py).
An allocation that names its pool by ``pool_name`` depends on the spec that saves that pool, when the plan saves it.

A spec depends on another when its data holds a ``NodeRef`` to the other's key, an ``Hfid`` the other names (its own
``hfid`` or one it ``provides``; a ``LinkPrefixSpec`` needs its end ports to exist, not to be updated), or, for a spec that updates an object another one provides (``hfid`` in someone's
``provides``), on that provider. An ``Hfid`` that no spec names is an external reference: the object exists already or
another generator creates it (``external_hfids``).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from generators.ipam.pools import ADDRESS_POOL_KIND, PREFIX_POOL_KIND
from generators.plan import (
    HFID_FIELDS,
    HFID_NUMBER_FIELDS,
    AddressAllocationSpec,
    EdgesSpec,
    Hfid,
    LinkPrefixSpec,
    NodeSpec,
    PrefixAllocationSpec,
    RoutingTagSpec,
    Spec,
    node_refs,
)

GraphSpec = NodeSpec | AddressAllocationSpec | LinkPrefixSpec | PrefixAllocationSpec
_GRAPH_TYPES = (NodeSpec, AddressAllocationSpec, LinkPrefixSpec, PrefixAllocationSpec)


def spec_keys(spec: GraphSpec) -> tuple[str, ...]:
    """Every key a ``NodeRef`` may use for this spec (a link prefix also defines its two addresses)."""
    if isinstance(spec, LinkPrefixSpec | PrefixAllocationSpec):
        return (spec.key, *spec.address_keys)
    return (spec.key,)


def hfid_refs(value: Any) -> list[Hfid]:
    """Every ``Hfid`` in ``value`` (a reference, or a mapping, list or tuple holding some)."""
    if isinstance(value, Hfid):
        return [value]
    if isinstance(value, Mapping):
        return [ref for item in value.values() for ref in hfid_refs(item)]
    if isinstance(value, list | tuple):
        return [ref for item in value for ref in hfid_refs(item)]
    return []


def render_hfids(value: Any) -> Any:
    """``value`` with every ``Hfid`` replaced by its ``{"hfid": ..., "kind": ...}`` reference."""
    if isinstance(value, Hfid):
        return value.render()
    if isinstance(value, Mapping):
        return {key: render_hfids(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [render_hfids(item) for item in value]
    return value


def _references(spec: GraphSpec) -> tuple[list[str], list[Hfid]]:
    """The ``NodeRef`` keys and the ``Hfid`` references of one spec."""
    if isinstance(spec, NodeSpec):
        both = (spec.data, spec.create_only)
        return [ref.key for ref in node_refs(both)], hfid_refs(both)
    if isinstance(spec, LinkPrefixSpec):
        return [ref.key for ref in node_refs(spec.pool_id)], list(spec.ends)
    if isinstance(spec, AddressAllocationSpec):
        return [ref.key for ref in node_refs(spec.pool_id)], []
    if isinstance(spec, PrefixAllocationSpec):
        return [ref.key for ref in node_refs(spec.parent)], [*hfid_refs(spec.parent), *hfid_refs(spec.identifier_ref)]
    return [], []


class _Index:
    """Which spec saves a key, names an HFID or provides one."""

    def __init__(self, specs: Sequence[GraphSpec]) -> None:
        self.by_key: dict[str, GraphSpec] = {}
        self.by_hfid: dict[Hfid, GraphSpec] = {}
        self.provider: dict[Hfid, GraphSpec] = {}
        for spec in specs:
            for key in spec_keys(spec):
                if key in self.by_key:
                    raise ValueError(f"The plan declares node {key!r} twice")
                self.by_key[key] = spec
            if isinstance(spec, NodeSpec):
                if spec.hfid is not None:
                    if spec.hfid in self.by_hfid:
                        raise ValueError(f"The plan declares {spec.hfid.kind} {list(spec.hfid.parts)} twice")
                    self.by_hfid[spec.hfid] = spec
                for provided in spec.provides:
                    self.provider.setdefault(provided, spec)

    def naming(self, hfid: Hfid) -> GraphSpec | None:
        """The spec that makes ``hfid`` exist: the one that names it, else the one that provides it."""
        return self.by_hfid.get(hfid) or self.provider.get(hfid)

    def existence(self, hfid: Hfid) -> GraphSpec | None:
        """The spec that makes ``hfid`` exist: the one that creates it, else the one that provides it. A spec that only
        updates it (``existing``) does not: what needs the object, not its update, waits for the provider.
        """
        spec = self.by_hfid.get(hfid)
        if isinstance(spec, NodeSpec) and not spec.existing:
            return spec
        return self.provider.get(hfid)

    def dependencies(self, spec: GraphSpec) -> list[GraphSpec]:
        keys, hfids = _references(spec)
        found: list[GraphSpec] = []
        for key in keys:
            if key not in self.by_key:
                raise ValueError(f"{_label(spec)} points at node {key!r}, which no spec of the plan saves")
            found.append(self.by_key[key])
        if isinstance(spec, LinkPrefixSpec):
            # The ends' ids make the identifier; their updates take the addresses this spec allocates
            found.extend(target for hfid in hfids if (target := self.existence(hfid)) is not None)
            return list({id(dep): dep for dep in found if dep is not spec}.values())
        found.extend(target for hfid in hfids if (target := self.naming(hfid)) is not None)
        if (pool := _named_pool(spec)) is not None and (saver := self.by_hfid.get(pool)) is not None:
            found.append(saver)
        if isinstance(spec, NodeSpec) and spec.hfid is not None and (owner := self.provider.get(spec.hfid)):
            found.append(owner)
        unique = {id(dep): dep for dep in found if dep is not spec}
        return list(unique.values())


def _named_pool(spec: GraphSpec) -> Hfid | None:
    """The pool an allocation names by ``pool_name`` only (no id, no parent, not a customer pool): resolved by name at
    write time, so it must come after the spec of the plan that saves a pool of that name.
    """
    if isinstance(spec, AddressAllocationSpec) and spec.pool_id is None and spec.pool_name:
        return Hfid(ADDRESS_POOL_KIND, (spec.pool_name,))
    if isinstance(spec, PrefixAllocationSpec) and spec.parent is None and spec.grow is None and spec.pool_name:
        return Hfid(PREFIX_POOL_KIND, (spec.pool_name,))
    return None


def _label(spec: GraphSpec) -> str:
    return f"{type(spec).__name__} {spec.key!r}"


def schedule(specs: Sequence[Spec]) -> list[list[GraphSpec] | RoutingTagSpec]:
    """The write order of the graph specs of a plan: each item is one dependency level (specs that do not depend on each
    other, in plan order) or a ``RoutingTagSpec`` (a pool draw: it ends the specs before it, nothing is ordered across
    it). Raises ValueError for a cycle, a ``NodeRef`` nothing saves, a key or HFID named twice, and a reference to a
    spec on the other side of a routing tag that comes later.
    """
    graph = [spec for spec in specs if isinstance(spec, _GRAPH_TYPES)]
    index = _Index(graph)  # type: ignore[arg-type]
    segment_of: dict[int, int] = {}
    segment = 0
    for spec in specs:
        if isinstance(spec, RoutingTagSpec):
            segment += 1
        elif isinstance(spec, _GRAPH_TYPES):
            segment_of[id(spec)] = segment

    steps: list[list[GraphSpec] | RoutingTagSpec] = []
    segment = 0
    members: list[GraphSpec] = []

    def close_segment() -> None:
        steps.extend(_levels(members, index, segment_of))
        members.clear()

    for spec in specs:
        if isinstance(spec, RoutingTagSpec):
            close_segment()
            steps.append(spec)
            segment += 1
        elif isinstance(spec, _GRAPH_TYPES):
            members.append(spec)  # type: ignore[arg-type]
    close_segment()
    return steps


def _levels(members: list[GraphSpec], index: _Index, segment_of: Mapping[int, int]) -> list[list[GraphSpec]]:
    """Group one segment's specs into dependency levels (Kahn's algorithm, plan order inside a level)."""
    level_of: dict[int, int] = {}
    pending = list(members)
    deps = {id(spec): index.dependencies(spec) for spec in members}
    for spec in members:
        for dep in deps[id(spec)]:
            if segment_of[id(dep)] > segment_of[id(spec)]:
                raise ValueError(f"{_label(spec)} depends on {_label(dep)}, which is written after a routing tag draw")
    while pending:
        ready = [
            spec
            for spec in pending
            if all(segment_of[id(dep)] < segment_of[id(spec)] or id(dep) in level_of for dep in deps[id(spec)])
        ]
        if not ready:
            raise ValueError(f"The plan has a dependency cycle among {sorted(_label(spec) for spec in pending)}")
        for spec in ready:
            level_of[id(spec)] = 1 + max(
                (level_of[id(dep)] for dep in deps[id(spec)] if id(dep) in level_of), default=-1
            )
        pending = [spec for spec in pending if id(spec) not in level_of]
    levels: dict[int, list[GraphSpec]] = {}
    for spec in members:
        levels.setdefault(level_of[id(spec)], []).append(spec)
    return [levels[number] for number in sorted(levels)]


def external_hfids(specs: Sequence[Spec]) -> list[Hfid]:
    """The HFIDs the plan references (or updates, ``NodeSpec.existing``) that no spec of it names or provides: they must
    exist before the first write, or another generator is expected to create them. In plan order, each once.
    """
    graph = [spec for spec in specs if isinstance(spec, _GRAPH_TYPES)]
    index = _Index(graph)  # type: ignore[arg-type]
    wanted: dict[Hfid, None] = {}
    for spec in graph:
        _, hfids = _references(spec)  # type: ignore[arg-type]
        for hfid in hfids:
            if (index.existence(hfid) if isinstance(spec, LinkPrefixSpec) else index.naming(hfid)) is None:
                wanted[hfid] = None
        if isinstance(spec, NodeSpec) and spec.existing and spec.hfid is not None and spec.hfid not in index.provider:
            wanted[spec.hfid] = None
    for spec in specs:
        if isinstance(spec, EdgesSpec):
            wanted.update(dict.fromkeys(hfid for hfid in hfid_refs(spec.peers) if index.naming(hfid) is None))
    return list(wanted)


def _part(node: Any, field: str) -> str:
    """One part of a node's HFID: an attribute (``name``) or an attribute of its parent (``device__name``)."""
    relationship, separator, attribute = field.rpartition("__")
    owner = getattr(node, relationship).peer if separator else node
    return str(getattr(owner, attribute if separator else field).value)


async def lookup_hfid_nodes(client: Any, hfids: Iterable[Hfid], include: Sequence[str] = ()) -> dict[Hfid, Any]:
    """The objects that exist among ``hfids`` (one filtered read per kind), read with the relationships ``include``."""
    by_kind: dict[str, list[Hfid]] = defaultdict(list)
    for hfid in dict.fromkeys(hfids):
        by_kind[hfid.kind].append(hfid)

    async def read(kind: str, wanted: list[Hfid]) -> dict[Hfid, Any]:
        fields = HFID_FIELDS[kind]
        filters = {
            f"{field}__values": sorted({_filter_value(field, hfid.parts[n]) for hfid in wanted}, key=str)
            for n, field in enumerate(fields)
        }
        parents = [field.partition("__")[0] for field in fields if "__" in field]
        relations = list(dict.fromkeys([*parents, *include]))
        nodes = await client.filters(kind=kind, **filters, **({"include": relations} if relations else {}))
        found = {Hfid(kind, tuple(_part(node, field) for field in fields)): node for node in nodes}
        return {hfid: found[hfid] for hfid in wanted if hfid in found}

    found: dict[Hfid, Any] = {}
    for part in await asyncio.gather(*(read(kind, wanted) for kind, wanted in by_kind.items())):
        found.update(part)
    return found


def _filter_value(field: str, part: str) -> str | int:
    """A part of an HFID as the value a filter takes: a Number attribute is an integer."""
    return int(part) if field in HFID_NUMBER_FIELDS else part


async def lookup_hfids(client: Any, hfids: Iterable[Hfid]) -> dict[Hfid, str]:
    """The ids of the objects that exist among ``hfids``: one filtered read per kind."""
    return {hfid: node.id for hfid, node in (await lookup_hfid_nodes(client, hfids)).items()}
