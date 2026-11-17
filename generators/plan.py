"""Declarative intent a generator builds from its input and hands to ``PlanReconciler`` (generators/reconciler.py).

A plan builder reads the cleaned query data and returns a ``TopologyPlan``: it never saves and never allocates. The
reconciler is the only writer.

``track`` says whether the run owns the object: True saves it into the run's group context, so a later run that drops it
deletes it (``delete_unused_nodes``); False saves it with ``update_group_context=False``, so the run never reclaims
it. A shared catalogue object, a key and a lock are untracked.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

Ownership = Literal["owned", "shared", "template"]
"""Who owns what a spec writes. ``owned``: the run's own output; it joins the generator's group and is removed with the
outputs it no longer plans (``delete_unused_nodes``). ``shared``: upserted, never tracked and never pruned: another
run, generator or the pool that allocated it owns it too. ``template``: owned by the object template of its device (a
port the template creates with the device and deletes with it): never tracked, never pruned, but not shared either: no
other generator writes it, and a run that stops planning it must not remove it."""


def _require_identifier(spec: Any) -> None:
    """Reject a spec whose identifier is empty: an allocation without it is not idempotent."""
    if not spec.identifier or not spec.identifier.strip():
        raise ValueError(f"{type(spec).__name__} needs a deterministic identifier (got {spec.identifier!r})")


@dataclass(frozen=True)
class DeviceSpec:
    """A DcimPhysicalDevice, active and tracked. ``template_name`` instantiates a TemplateDcimPhysicalDevice on first
    creation only (resending it to an existing device fails server-side). ``discover_interface_role`` set: the
    template's ports of that role are read back once the device exists (``ReconcileResult.interface_ids``).
    """

    name: str
    template_name: str | None = None
    discover_interface_role: str | None = None
    owner_id: str | None = None
    deployment_id: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class NodeRef:
    """The id of the ``NodeSpec`` saved under ``key`` earlier in the plan; resolved by the reconciler wherever a spec
    takes data, a scope or an id.
    """

    key: str


@dataclass(frozen=True)
class Hfid:
    """A reference to an object by its human-friendly id, whether or not this run has created it yet. In ``data`` it is
    rendered as ``{"hfid": [...], "kind": kind}`` and the server resolves it at save, so the reconciler only has to
    write the object it names first (``plan_graph.dependency_levels``). ``kind`` is the concrete kind (a generic peer
    needs it); ``parts`` follow the schema's ``human_friendly_id`` (see ``HFID_FIELDS``).
    """

    kind: str
    parts: tuple[str, ...]

    def render(self) -> dict[str, Any]:
        return {"hfid": list(self.parts), "kind": self.kind}


# kind -> the attribute paths of its human_friendly_id (schemas/base): the filters a lookup by HFID sends
HFID_FIELDS: dict[str, tuple[str, ...]] = {
    "DcimPhysicalDevice": ("name",),
    "DcimVirtualDevice": ("name",),
    "CloudNetworkAppliance": ("name",),
    "DcimPhysicalInterface": ("device__name", "name"),
    "DcimVirtualInterface": ("device__name", "name"),
    "DcimLAGInterface": ("device__name", "name"),
    "DcimCable": ("name",),
    "CoreStandardGroup": ("name",),
    "CoreIPPrefixPool": ("name",),
    "CoreIPAddressPool": ("name",),
    "CoreNumberPool": ("name",),
    "ManagedBGP": ("name",),
    "ManagedOSPF": ("name",),
    "ManagedMLAG": ("name",),
    "ManagedFirewallHA": ("name",),
    "ManagedFirewallContext": ("name",),
    "ManagedLoadbalancerContext": ("name",),
    "TopologyRoutedExchange": ("name",),
    "ManagedLoadbalancerHA": ("name",),
    "ManagedBGPPeering": ("name",),
    "ManagedOSPFPeering": ("name",),
    "RoutingOSPFInterface": ("name",),
    "RoutingPassword": ("name",),
    "RoutingOSPFArea": ("name",),
    "ManagedSegmentDeployment": ("segment__name", "deployment__name"),
    "ManagedSegmentVlan": ("device__name", "vlan_id"),
    "ManagedVRF": ("deployment__name", "security_zone__name"),
    "ManagedPBR": ("name",),
    "SecurityPolicy": ("name",),
    "SecurityTag": ("name",),
    "ProxyPolicy": ("name",),
    "IpamIPAddress": ("address", "ip_namespace__name"),
    "IpamPrefix": ("prefix", "ip_namespace__name"),
}
HFID_NUMBER_FIELDS = frozenset({"vlan_id"})
"""The HFID fields that are Number attributes: a part is a string in an ``Hfid`` (the server wants strings in an HFID
array), a filter on it takes the integer."""


@dataclass(frozen=True)
class NodeSpec:
    """One node upserted from ``data`` (``client.create`` + ``save(allow_upsert=True)``). A later spec points at it by
    ``NodeRef(key)`` or, when ``hfid`` names it, by that ``Hfid``; the reconciler orders the writes so every referenced
    node is saved first (a cycle is rejected by ``validate``). A ``NodeRef`` anywhere in ``data`` becomes the node's id,
    an ``Hfid`` its ``{"hfid": ...}`` reference. ``data`` with an ``id`` updates that node, without one the server finds
    it by its HFID. ``retry_on_conflict``: one retry after a GraphQL error (a run that created the same node
    concurrently). The saved node is ``ReconcileResult.nodes[key]``, its id ``ReconcileResult.ids[hfid]``. ``track`` as
    for every spec.

    ``replaces``: the objects (HFID) this one takes the place of, deleted just before it is written, and only when they
    are generated (members of a generator's tracking group): the conflicting row that holds the VLAN id the new row takes.

    ``ownership`` says explicitly whether the run owns the node (``Ownership``; set, it decides the tracking and a
    conflicting ``track`` is rejected; unset, ``track`` does).

    ``create_only``: attributes merged into ``data`` at write time and filled by the builder only when its pre-read found
    no such object (a secret, a number drawn from a pool, the fields a create needs): an object that exists is never sent
    them again (a secret would rotate). ``sequential``: a pool draw (an AS whose number
    comes from a pool), written one by one before the wave of its level.

    ``clear``: relationships of the node (``data`` carries its ``id``) that the write sets to none; the SDK sends a
    relationship that is none only when the node clears it, so a relationship the node must lose is named here.

    ``provides``: the children the object brings with it that this plan never writes as specs of their own (the ports
    a device template creates); once the node is saved the reconciler reads their ids back. ``existing``: the node
    already exists (a template port, a port another generator created) and the spec only updates it, so a spec that
    references it waits for the creator: an ``existing`` node nothing in the plan provides is an external dependency
    (``TopologyPlan.producers``), checked before any write.
    """

    key: str
    kind: type[Any] | str
    data: Mapping[str, Any]
    track: bool = False
    retry_on_conflict: bool = False
    hfid: Hfid | None = None
    provides: tuple[Hfid, ...] = ()
    existing: bool = False
    ownership: Ownership | None = None
    create_only: Mapping[str, Any] = field(default_factory=dict)
    sequential: bool = False
    replaces: tuple[Hfid, ...] = ()
    clear: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ownership is not None and self.track and self.ownership != "owned":
            raise ValueError(f"NodeSpec {self.key!r} is {self.ownership} but asks to be tracked")
        if self.hfid is not None and self.hfid in self.replaces:
            raise ValueError(f"NodeSpec {self.key!r} replaces itself")

    @property
    def tracked(self) -> bool:
        """Whether the run tracks the node: ``ownership`` decides when set, else ``track``."""
        return self.track if self.ownership is None else self.ownership == "owned"


@dataclass(frozen=True)
class AddressAllocationSpec:
    """One address drawn from the address pool ``pool_id`` (or the pool called ``pool_name`` when no id) under
    ``identifier`` (``allocate_next_ip_address``: the same identifier gives the same address on a re-run). An ordered
    allocation step: the reconciler runs it before the specs that point at it by ``NodeRef(key)``; the address is
    ``ReconcileResult.nodes[key]``. ``ownership`` is ``shared`` (the pool owns the allocation, nothing is tracked).
    """

    key: str
    identifier: str
    prefix_length: int
    description: str
    ownership: Ownership
    pool_id: str | NodeRef | None = None
    pool_name: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self)
        if self.ownership != "shared":
            raise ValueError(
                f"AddressAllocationSpec {self.key!r} is {self.ownership}: the pool owns an allocated address, "
                "the run never tracks it, so it is shared"
            )
        if self.pool_id is None and self.pool_name is None:
            raise ValueError(f"AddressAllocationSpec {self.key!r} names neither a pool id nor a pool name")


@dataclass(frozen=True)
class LinkPrefixSpec:
    """The P2P prefix of the link joining the two ports ``ends``, from the prefix pool ``pool_id``, and both its
    addresses (offsets 0 and 1). The identifier is the sorted ids of the two ports joined by ``__``, so a re-run gets
    the same prefix; the reconciler reads the ids when the ports exist. The prefix is ``ReconcileResult.nodes[key]``, the
    addresses ``nodes[address_keys[0]]`` (bottom end) and ``nodes[address_keys[1]]``. ``ownership`` is the addresses':
    ``owned`` tracks them with the run's outputs, ``shared`` does not (the pool owns the prefix either way).
    """

    key: str
    ends: tuple[Hfid, Hfid]
    pool_id: str | NodeRef
    prefix_length: int
    ownership: Ownership

    @property
    def address_keys(self) -> tuple[str, str]:
        return f"{self.key}#0", f"{self.key}#1"


@dataclass(frozen=True)
class PoolGrowth:
    """A customer pool: ``PrefixAllocationSpec.pool_name`` is found, or cut out of a ``block_length`` block of the prefix
    pool ``global_pool`` when missing, and grows by one such block when it cannot satisfy an allocation.
    """

    global_pool: str
    block_length: int


@dataclass(frozen=True)
class PrefixAllocationSpec:
    """A prefix of ``prefix_length`` drawn from a prefix pool under ``identifier`` (``allocate_next_ip_prefix``: the same
    identifier gives the same prefix on a re-run), as ``role``. The pool is ``parent`` (the id of a pool, a ``NodeRef``
    to a pool the plan saves, or an ``Hfid`` of a pool that exists or that another generator creates) or ``pool_name``.
    ``is_pool`` and ``member_type`` save the prefix as a pool of that member type; ``status`` and ``description`` are its
    attributes when set. ``grow`` set: ``pool_name`` is a customer pool (``PoolGrowth``). ``address_offsets``: the
    addresses of the prefix at those offsets (``upsert_prefix_addresses``), tracked when ``address_ownership`` is
    ``owned``. The prefix is ``ReconcileResult.nodes[key]``, the address at offset n ``nodes[address_key(n)]``: a pool
    saved around it lists it as a resource. The pool owns the allocation: shared. The caller holds the locks of a pool
    it chooses from (``plan_locks``). ``label`` set: the refusal of another length for an allocated identifier (it cannot
    change in place) is reported naming it. ``track_prefix``: the run owns the prefix (a session circuit's link prefix,
    removed with it): it joins the run's group context, which a pool allocation does not by itself.
    ``identifier_ref`` set: the identifier is ``{identifier}__{id}`` with the id of
    the object that ``Hfid`` names (the run's own or an existing one), so a prefix keyed by an object's id is planned
    before the object has one. ``address_length``: the prefix length of the addresses, when it is not the prefix's.
    """

    key: str
    identifier: str
    prefix_length: int
    role: str
    parent: str | NodeRef | Hfid | None = None
    pool_name: str | None = None
    is_pool: bool = False
    member_type: str | None = None
    status: str | None = None
    description: str | None = None
    grow: PoolGrowth | None = None
    address_offsets: tuple[int, ...] = ()
    address_description: str | None = None
    address_ownership: Ownership = "shared"
    label: str | None = None
    track_prefix: bool = False
    identifier_ref: Hfid | None = None
    address_length: int | None = None

    def __post_init__(self) -> None:
        _require_identifier(self)
        if (self.parent is None) == (self.pool_name is None):
            raise ValueError(f"PrefixAllocationSpec {self.key!r} needs exactly one of a parent pool and a pool name")
        if self.grow is not None and self.pool_name is None:
            raise ValueError(f"PrefixAllocationSpec {self.key!r} grows a customer pool, which is found by name")

    @property
    def data(self) -> dict[str, Any]:
        """The attributes of the allocated prefix."""
        attributes: dict[str, Any] = {"role": self.role}
        if self.status is not None:
            attributes["status"] = self.status
        if self.description is not None:
            attributes["description"] = self.description
        if self.is_pool:
            attributes["is_pool"] = True
        return attributes

    def address_key(self, offset: int) -> str:
        return f"{self.key}#{offset}"

    @property
    def address_keys(self) -> tuple[str, ...]:
        return tuple(self.address_key(offset) for offset in self.address_offsets)


@dataclass(frozen=True)
class TrackSpec:
    """Objects that exist already and that the run owns: their ids join the run's group context without a write, so the
    run keeps them (``delete_unused_nodes``). A found shared AS, a role group's member that is not rewritten.
    """

    ids: tuple[str, ...]


@dataclass(frozen=True)
class RetireSpec:
    """Delete the objects ``hfids`` (of kind ``hfid.kind``) that exist and are generated (members of a generator's
    tracking group), before the nodes of the plan are written: what a run no longer plans and must free the way for. An
    object that is not generated is never deleted by it: the run fails naming it.
    """

    hfids: tuple[Hfid, ...]


@dataclass(frozen=True)
class ExternalProducer:
    """Another generator that creates what the plan references but does not write: ``generators`` run for ``parent_id``.
    An external reference that is missing before the first write is waited for (``WAIT_FOR_PEER_GENERATOR``) and, when
    the producer's last run failed, fails the run at once (``BaseGenerator.wait_for_parent_generators``).
    """

    generators: tuple[str, ...]
    parent_id: str


@dataclass(frozen=True)
class EdgesSpec:
    """Add ``peers`` to ``relation`` of ``node``, one RelationshipAdd; the other peers stay (``add_edges``). A peer is an
    id, a ``NodeRef`` or an ``Hfid`` (an object the plan writes or provides, or one that exists: checked before any write).
    ``retry_not_found``: retried while a peer is not visible yet (a node another run has just created). ``kind`` set:
    the holder is read first and only the peers it lacks are added (nothing is sent when it has them all).
    """

    node: str | NodeRef
    relation: str
    peers: tuple[str | NodeRef | Hfid, ...]
    retry_not_found: bool = False
    kind: type[Any] | str | None = None


@dataclass(frozen=True)
class RoutingTagSpec:
    """Draw ``kind``.``routing_tag`` of ``node_id`` from the number pool ``pool_name``, once (the node has none): saved
    untracked. The number is ``ReconcileResult.routing_tags[node_id]``. A pool that is not loaded fails the run.
    """

    kind: str
    node_id: str
    pool_name: str


@dataclass(frozen=True)
class PruneSpec:
    """Delete what the run no longer wants: every ``kind`` node matching ``scope`` (filters kwargs, never empty) whose
    ``key`` attribute (or ``id``) is not in ``keep``. "Present in the old state, absent from the plan".
    """

    kind: type[Any] | str
    scope: Mapping[str, Any]
    keep: frozenset[Any]
    key: str = "name"

    def __post_init__(self) -> None:
        if not self.scope:
            raise ValueError("PruneSpec needs a non-empty scope: an empty one would delete every node of the kind")


@dataclass(frozen=True)
class PruneEdgesSpec:
    """Detach the peers of ``holder_id``'s ``relationship`` that are not in ``keep_peer_ids`` (the peers stay).
    ``known_peer_ids``: the peers the caller read already; only those not kept are detached and the holder is not read
    again, so a peer another run added meanwhile stays. ``holder_id`` may be a ``NodeRef``.
    """

    holder_kind: type[Any] | str
    holder_id: str | NodeRef
    relationship: str
    keep_peer_ids: frozenset[str]
    known_peer_ids: frozenset[str] | None = None


@dataclass(frozen=True)
class CustomerPoolsSpec:
    """Board a footprint: the customer's pool of each address family its kind has, created once
    (``generators.ipam.customer_pool.ensure_customer_pools``). ``design`` is the cleaned template row with the block lengths.
    """

    kind: str
    org_id: str
    environment: str
    design: Mapping[str, Any]
    footprint_name: str


@dataclass(frozen=True)
class CustomerSliceSpec:
    """A prefix of ``length`` keyed ``identifier`` from the customer's pool, which grows by one ``block_length`` block
    of ``global_pool`` when full (``generators.ipam.customer_pool.allocate_customer_slice``). Untracked. The prefix is
    ``ReconcileResult.prefixes[identifier]``.
    """

    identifier: str
    pool: str
    global_pool: str
    block_length: int
    length: int

    def __post_init__(self) -> None:
        _require_identifier(self)


@dataclass(frozen=True)
class AddEdgesSpec:
    """Add one peer to ``relation`` of ``node_id`` and keep the others (``add_edges``): the prefix a
    ``CustomerSliceSpec`` allocated under ``peer_prefix``. ``node_id`` is an id, or a ``NodeRef`` to a node the plan
    saves (the step runs after the nodes).
    """

    node_id: str | NodeRef
    relation: str
    peer_prefix: str


Spec = (
    CustomerPoolsSpec
    | CustomerSliceSpec
    | AddEdgesSpec
    | DeviceSpec
    | NodeSpec
    | AddressAllocationSpec
    | LinkPrefixSpec
    | PrefixAllocationSpec
    | TrackSpec
    | RetireSpec
    | EdgesSpec
    | RoutingTagSpec
    | PruneSpec
    | PruneEdgesSpec
)
S = TypeVar("S")


@dataclass
class TopologyPlan:
    """Everything a generator wants, by spec kind. ``root_id`` is the object the generator was started for: the
    reconciler adds it to the run's group context before anything else, even when the plan is empty. A plan a helper
    builds inside a run (the SVIs of one device) has no root (None): the run already tracks its own. ``producers``: the
    generators that create the objects the plan references without writing them (``ExternalProducer``).
    """

    root_id: str | None
    specs: list[Spec] = field(default_factory=list)
    producers: list[ExternalProducer] = field(default_factory=list)

    def add(self, spec: Spec) -> None:
        self.specs.append(spec)

    def of(self, spec_type: type[S]) -> list[S]:
        """The specs of one kind, in the order they were added."""
        return [spec for spec in self.specs if isinstance(spec, spec_type)]

    def validate(self) -> None:
        """Reject a plan the reconciler cannot write: repeated names, keys or identifiers, a customer slice or a node. Raises ValueError before anything is written."""
        _unique("device", [spec.name for spec in self.of(DeviceSpec)])
        _unique("customer slice", [spec.identifier for spec in self.of(CustomerSliceSpec)])
        self._validate_nodes()
        slices = {spec.identifier for spec in self.of(CustomerSliceSpec)}
        for edge in self.of(AddEdgesSpec):
            if edge.peer_prefix not in slices:
                raise ValueError(f"AddEdgesSpec on {edge.node_id} uses unknown customer slice {edge.peer_prefix!r}")

    def _validate_nodes(self) -> None:
        """Reject what cannot be ordered: a node key or HFID used twice, a ``NodeRef`` nothing saves, a dependency cycle
        (``plan_graph.schedule``), and a ref in an edge or prune spec (they run after
        every node) to a node the plan does not save.
        """
        from generators.plan_graph import schedule, spec_keys

        graph = [
            spec
            for spec in self.specs
            if isinstance(spec, NodeSpec | AddressAllocationSpec | LinkPrefixSpec | PrefixAllocationSpec)
        ]
        schedule(self.specs)
        all_keys = {key for spec in graph for key in spec_keys(spec)}
        for spec in self.specs:
            if isinstance(spec, EdgesSpec):
                _check_refs((spec.node, spec.peers), all_keys, "EdgesSpec")
            elif isinstance(spec, PruneSpec):
                _check_refs(spec.scope, all_keys, f"PruneSpec of {spec.kind}")
            elif isinstance(spec, PruneEdgesSpec):
                _check_refs(spec.holder_id, all_keys, "PruneEdgesSpec")
            elif isinstance(spec, AddEdgesSpec):
                _check_refs(spec.node_id, all_keys, "AddEdgesSpec")


def node_refs(value: Any) -> list[NodeRef]:
    """Every ``NodeRef`` in ``value`` (a ref, or a mapping, list or tuple holding some)."""
    if isinstance(value, NodeRef):
        return [value]
    if isinstance(value, Mapping):
        return [ref for item in value.values() for ref in node_refs(item)]
    if isinstance(value, list | tuple):
        return [ref for item in value for ref in node_refs(item)]
    return []


def _check_refs(value: Any, saved: set[str], label: str) -> None:
    for ref in node_refs(value):
        if ref.key not in saved:
            raise ValueError(f"{label} points at node {ref.key!r}, which no spec of the plan saves")


def _unique(label: str, keys: list[Any]) -> list[Any]:
    """``keys`` unchanged, or ValueError naming the first repeated one."""
    seen: set[Any] = set()
    for key in keys:
        if key in seen:
            raise ValueError(f"The plan declares {label} {key!r} twice")
        seen.add(key)
    return keys
