"""Change-risk model — the pure half of the change-risk check.

See [docs/change_risk.md](../docs/change_risk.md) for the design. Nothing in
this module performs I/O: the check makes the API calls and hands the responses
here, which is what lets the traversal comparison and the scoring be unit tested
without a live Infrahub.

The traversal itself is Infrahub's own, not ours (Infrahub 1.10+). Both of its
graph queries are used, for what each is actually good at:

* `InfrahubReachableNodes` (`client.reachable_nodes`) walks outward from one
  source and returns every node of the requested kinds. It takes no relationship
  filter, so at any useful depth it reaches half the site through shared
  containers like `location` — but *unrestricted reachability is a superset of
  restricted reachability*, so it is a sound and very cheap way to rule
  candidates out. One call per changed object produces the shortlist.
* `InfrahubPathTraversal` (`client.traverse_paths`) does take a relationship
  filter, and decides each shortlisted candidate exactly: reachable over the
  curated edge set or not, at what depth, over which path.

Two facts shape everything below:

* The model contributes the *edge set*, not the walk. `TRAVERSAL_EDGES` below is
  the whole of it — an edge missing there is a blast radius the check cannot
  see, and an edge added carelessly is how a tool comes to report that
  everything affects everything.
* The counterfactual is not a simulation. A proposed change's branch already
  contains the change, so "what breaks" is the same traversal run twice: once on
  the branch and once on its base. Nothing has to be excluded by hand.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from utils.data_cleaning import clean_data

#: Stored query in queries/risk/, registered under `queries:` in .infrahub.yml.
#: It supplies the attributes the traversal does not: a reachable node comes
#: back as identity only (id, kind, label), and exposure is weighted by
#: application criticality.
EXPOSURE_QUERY = "impact_exposure"

#: Hops. Deep enough for DC port → cable → cage router → circuit → virtual
#: circuit → cloud endpoint → TGW attachment → TGW → route table → spoke
#: attachment → VPC → segment → component → application.
MAX_DEPTH = 14
#: Candidates per source from the shortlisting pass. Hitting it is reported as
#: unknown impact, never as no impact. 200 is the server's own ceiling on
#: `max_targets`/`max_paths` — asking for more is rejected outright
#: ("max_targets must be in [1, 200]"), so this is a hard limit, not a tuning knob.
MAX_RESULTS = 200
#: Ids per `impact_exposure` call.
BATCH_SIZE = 200

EXPOSURE_WEIGHT = {"critical": 8.0, "high": 4.0, "medium": 2.0, "low": 1.0}
SEVERITY_WEIGHT = {"outage": 1.0, "degraded": 0.4, "ok": 0.0, "unknown": 0.0}

#: "Worst" ordering for folding component severities into an application.
#: `unknown` sits above `ok` because an undetermined fate is not a safe one, and
#: below the two determined failures because it contributes no score.
_SEVERITY_RANK = {"ok": 0, "unknown": 1, "degraded": 2, "outage": 3}

REVIEW_SCORE = 4.0
MIN_CONFIDENCE = 0.8
#: Findings of the same code past this many are summarised instead. A check
#: reports through log lines, and a hundred identical ones hide the one that
#: matters.
MAX_FINDINGS_PER_CODE = 10

#: The edges the traversal may walk, as (kind, relationship name) pairs. They
#: are resolved to schema relationship *identifiers* at runtime, which is what
#: `traverse_paths(relationship_filter=...)` matches on.
#:
#: An identifier is shared by both ends of an edge, so one entry per logical
#: edge is enough and the filter is direction-agnostic. That is also why the
#: association/propagation split below is expressed by omission rather than by
#: an arrival rule.
TRAVERSAL_EDGES: tuple[tuple[str, str], ...] = (
    # ── On-premises: device, interface, cable ────────────────────────────────
    ("DcimDevice", "interfaces"),
    # `cable` is declared on DcimEndpoint, which only DcimPhysicalInterface
    # inherits — a virtual or LAG interface has no cable to walk.
    ("DcimEndpoint", "cable"),
    ("DcimCable", "endpoints"),
    # A virtual device shares the fate of the hardware it runs on, so the walk
    # has to step from the VM onto its host to reach the host's NICs, cables and
    # leaf pair. This is the on-premises counterpart of
    # CloudVirtualNetwork.instances — without it an AppComponent's instances are
    # dead ends and the redundancy verdict has nothing underneath it.
    ("DcimVirtualDevice", "hosting_device"),
    # ── Capability edges ────────────────────────────────────────────────────
    # The uniform "what is this port doing" and "what does this device host"
    # edges. Declared on the generic that owns them, so a device or interface
    # kind added later is covered without editing this list. One pair each
    # covers every capability kind: segments, OSPF, BGP, firewall contexts,
    # exchange gateways, VIPs and virtual circuits.
    ("DcimInterface", "interface_capabilities"),
    ("ManagedGenericDevice", "capabilities"),
    # ── Circuits ────────────────────────────────────────────────────────────
    # A physical circuit keeps its customer/provider split, which carries an
    # endpoint role the flat capability edge cannot express.
    ("TopologyPhysicalCircuit", "customer_interfaces"),
    ("TopologyPhysicalCircuit", "provider_interfaces"),
    ("TopologyVirtualCircuit", "interface_capabilities"),
    ("TopologyVirtualCircuit", "physical_circuits"),
    # The edge that closes the colo leg: a circuit's cloud end is a transit VIF
    # or VPN gateway, not a device port.
    ("TopologyVirtualCircuit", "cloud_endpoints"),
    # ── Cloud hub ───────────────────────────────────────────────────────────
    ("CloudHybridAttachment", "transit_gateway_attachment"),
    ("CloudTransitGatewayAttachment", "transit_gateway"),
    ("CloudTransitGatewayAttachment", "owner_account"),
    ("CloudTransitGatewayAttachment", "virtual_network"),
    ("CloudTransitGatewayAttachment", "network_segments"),
    ("CloudTransitGatewayAttachment", "propagates_to_route_tables"),
    ("CloudTransitGateway", "route_tables"),
    ("CloudTransitGatewayRouteTable", "propagated_attachments"),
    ("CloudVirtualNetwork", "network_segments"),
    ("CloudVirtualNetwork", "instances"),
    ("CloudInstance", "network_segment"),
    # ── Segments and applications ───────────────────────────────────────────
    ("AppComponent", "network_segment"),
    ("AppComponent", "instances"),
    ("AppComponent", "parent"),
    # ── Namespaces (VRF boundaries) ─────────────────────────────────────────
    ("TopologyExchangeGateway", "namespace_a"),
    ("TopologyExchangeGateway", "namespace_z"),
)

#: Edges deliberately left out of `TRAVERSAL_EDGES`, recorded because omitting
#: an edge is a design decision and an unexplained omission reads as an
#: oversight. Not used programmatically; asserted against the schema by
#: tests/unit/test_risk_model.py so a rename cannot rot the reasoning.
EXCLUDED_EDGES: tuple[tuple[str, str, str], ...] = (
    (
        "CloudTransitGatewayRouteTable",
        "associated_attachments",
        "Association says where a spoke's own route lookups resolve; propagation "
        "says whose lookups can see the spoke. They fail separately, and only "
        "propagation is reachability. Walking association is how a blast-radius "
        "tool comes to claim that every spoke on a hub affects every other one.",
    ),
    (
        "CloudTransitGatewayAttachment",
        "associated_route_table",
        "The same edge from the other side. Relationship identifiers are shared "
        "by both ends, so omitting one end omits both.",
    ),
    (
        "TopologyPhysicalCircuit",
        "locations",
        "A shared container, not a path. Two circuits landing in the same colo "
        "are not thereby connected to each other.",
    ),
    (
        "CloudVirtualNetwork",
        "account",
        "Reached through the attachment's owner_account instead, which is the "
        "edge that carries the cross-account failure domain. Walking every "
        "resource in an account from the account node makes account membership "
        "look like connectivity.",
    ),
    (
        "AppComponent",
        "depends_on",
        "Application-level dependency, not network reachability. It belongs in the exposure report, not in the path.",
    ),
)

#: Kinds the shortlisting pass asks `reachable_nodes` for. Deliberately only the
#: things risk is *scored* on: everything the change passes through on the way —
#: cables, circuits, transit gateways, accounts — arrives anyway as a hop on a
#: confirmed path, and asking for it here would only lengthen the shortlist.
#:
#: Generics are used so a segment or component kind added later stays in scope.
SHORTLIST_KINDS: tuple[str, ...] = (
    "AppApplication",
    "AppComponent",
    "ManagedNetworkSegment",
)

#: Of the shortlist, the kinds worth spending one `traverse_paths` call each to
#: decide exactly. A segment is not here on purpose: it is scored only through
#: the components sitting on it, and confirming every VLAN in a site
#: individually would cost more calls than the answer is worth. A shortlisted
#: segment with no component on it is reported as exposure of unknown size,
#: which is what it is — the shortlist is a superset, not a proof.
CONFIRM_KINDS: frozenset[str] = frozenset({"AppApplication", "AppComponent"})

#: Kinds whose appearance on the path to two different applications means those
#: applications share a failure domain.
SHARED_FATE_KINDS = {
    "DcimCable",
    "TopologyPhysicalCircuit",
    "TopologyVirtualCircuit",
    "CloudTransitGateway",
    "CloudTransitGatewayRouteTable",
}

# Root keys of queries/risk/impact_exposure.gql, by what they carry.
_APP_ROOTS = {"AppApplication"}
_COMPONENT_ROOTS = {"AppComponent", "components_via_segment", "components_via_instance"}
_SEGMENT_ROOTS = {"ManagedNetworkSegment"}
_CUSTOMER_ROOTS = {"TopologyCustomer"}
_ACCOUNT_ROOTS = {"CloudAccount"}


# ---------------------------------------------------------------------------
# The change itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChangedNode:
    """One node from the branch diff — a traversal source."""

    id: str
    kind: str
    action: str
    label: str
    elements: frozenset[str] = frozenset()


def changes_from_diff(diff: Iterable[Mapping[str, Any]] | None) -> list[ChangedNode]:
    """Turn `InfrahubClient.get_diff_summary()` output into traversal sources."""
    changes: list[ChangedNode] = []
    for node in diff or []:
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            continue
        changes.append(
            ChangedNode(
                id=node_id,
                kind=node.get("kind") or "",
                action=(node.get("action") or "").lower(),
                label=node.get("display_label") or node_id,
                elements=frozenset(
                    element.get("name") or "" for element in node.get("elements") or [] if isinstance(element, Mapping)
                ),
            )
        )
    return changes


# ---------------------------------------------------------------------------
# Edge set → relationship filter
# ---------------------------------------------------------------------------


def resolve_relationship_filter(
    schema: Mapping[str, Any],
    edges: Iterable[tuple[str, str]] = TRAVERSAL_EDGES,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Resolve `(kind, relationship name)` pairs to schema relationship identifiers.

    `schema` is `client.schema.all()` output — kind name to a schema object with
    a `relationships` list. Resolving at runtime rather than hard-coding the
    identifier strings means a schema rename surfaces as a reported miss instead
    of as a traversal that quietly stops early.

    Returns the deduplicated identifiers and the pairs that did not resolve.
    """
    identifiers: dict[str, None] = {}
    missing: list[tuple[str, str]] = []

    for kind_name, rel_name in edges:
        kind = schema.get(kind_name)
        relationships = getattr(kind, "relationships", None) if kind is not None else None
        identifier = None
        for relationship in relationships or []:
            if getattr(relationship, "name", None) == rel_name:
                identifier = getattr(relationship, "identifier", None)
                break
        if identifier:
            identifiers[identifier] = None
        else:
            missing.append((kind_name, rel_name))

    return list(identifiers), missing


# ---------------------------------------------------------------------------
# Pass 1: the candidate set, from the scoring targets on the branch
# ---------------------------------------------------------------------------


@dataclass
class Shortlist:
    """The scoring targets one source is worth deciding, on one branch.

    `InfrahubReachableNodes` looked like the natural first pass — unrestricted
    reachability is a superset of reachability over `TRAVERSAL_EDGES`, so it
    could have pruned the candidate list for free. It cannot be used: being
    unfiltered, it fans out through every shared container in a site, and past
    depth 5 on demo-sized data the server answers `Java heap space`. The depth
    this model needs is 14.

    So the candidate list is not pruned by reachability at all — it is every
    application, component and segment on the branch, and pass 2 decides each one
    over the curated edges. That is affordable because the things risk is
    *scored* on are few (tens), while the things a change can reach are many.
    """

    source_id: str = ""
    source_label: str = ""
    source_kind: str = ""
    #: candidate id -> kind
    candidates: dict[str, str] = field(default_factory=dict)
    #: The cap was hit, so candidates beyond it were never offered at all.
    truncated: bool = False
    #: The source does not exist on this branch — added, or deleted. Not an
    #: error: it is the change itself.
    missing: bool = False

    def of_kinds(self, kinds: Iterable[str]) -> set[str]:
        wanted = set(kinds)
        return {node_id for node_id, kind in self.candidates.items() if kind in wanted}


def shortlist_from_targets(
    source: ChangedNode,
    targets: Mapping[str, str],
    *,
    present: bool,
    max_results: int = MAX_RESULTS,
) -> Shortlist:
    """Pair one source with the branch's scoring targets.

    `targets` is id -> kind for every candidate on the branch, and `present` is
    whether the source itself exists there. A missing source is the change
    itself — added, or deleted — and is established by asking for the node
    rather than by inferring it from a traversal that failed, because a traversal
    can fail for reasons that have nothing to do with the node being there.
    """
    if not present:
        return Shortlist(source_id=source.id, source_label=source.label, source_kind=source.kind, missing=True)

    return Shortlist(
        source_id=source.id,
        source_label=source.label,
        source_kind=source.kind,
        candidates=dict(list(targets.items())[:max_results]),
        truncated=len(targets) > max_results,
    )


# ---------------------------------------------------------------------------
# Pass 2: confirmed reachability, from InfrahubPathTraversal
# ---------------------------------------------------------------------------


@dataclass
class ReachedNode:
    """A node the server found reachable, and how far away it was."""

    id: str
    kind: str
    label: str
    depth: int
    #: Node ids of the confirmed path, source first and this node last. It is
    #: *a* shortest path over `TRAVERSAL_EDGES`, not every path that exists.
    path: list[str] = field(default_factory=list)


@dataclass
class ReachSet:
    """Everything confirmed reachable from a set of sources, on one branch."""

    nodes: dict[str, ReachedNode] = field(default_factory=dict)
    #: source id -> label, for sources that resolved on this branch.
    sources: dict[str, str] = field(default_factory=dict)
    #: source ids that do not exist on this branch — added on the base, or
    #: deleted on the branch. Not an error: it is the change itself.
    missing_sources: set[str] = field(default_factory=set)
    #: sources whose radius is under-reported, because the shortlist was capped
    #: or a path search ran out of depth budget.
    truncated_sources: set[str] = field(default_factory=set)
    #: kind by id for every node seen, including path hops and shortlisted
    #: candidates that were never confirmed.
    kinds: dict[str, str] = field(default_factory=dict)
    #: source id -> the ids one hop out from it.
    rings: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    #: source id -> what went wrong, for pairs the server refused to answer. Kept
    #: apart from `truncated_sources` only to name the error in the report; both
    #: mean the same thing for the score, which is that the radius is a floor.
    errors: dict[str, str] = field(default_factory=dict)

    def note_error(self, source_id: str, message: str) -> None:
        """Record a traversal that failed rather than returning no path.

        A refused call and an unreachable destination arrive at the same place in
        the code and mean opposite things: one is "no impact", the other is "not
        known". Conflating them is how a risk check comes to pass a change it
        never managed to look at.
        """
        self.errors.setdefault(source_id, message)
        self.truncated_sources.add(source_id)

    def note_shortlist(self, shortlist: Shortlist) -> None:
        """Record what the shortlisting pass learned, before anything is confirmed."""
        if shortlist.missing:
            self.missing_sources.add(shortlist.source_id)
            return
        self.sources[shortlist.source_id] = shortlist.source_label
        self.kinds.setdefault(shortlist.source_id, shortlist.source_kind)
        if shortlist.truncated:
            self.truncated_sources.add(shortlist.source_id)
        for node_id, kind in shortlist.candidates.items():
            self.kinds.setdefault(node_id, kind)

    def absorb_paths(self, payload: Mapping[str, Any] | None) -> None:
        """Fold one `InfrahubPathTraversal` result in.

        Accepts the serialised form (`result.model_dump()`), so the model stays
        free of any dependency on the SDK's pydantic classes. Every node *along*
        a confirmed path is recorded too, not only its destination: a circuit the
        change reaches an application through is as impacted as the application,
        and is what the shared-fate finding is computed from.

        No path means the destination is not reachable over `TRAVERSAL_EDGES` —
        unless the search was truncated, in which case it means nothing and the
        source is marked as under-reported.
        """
        payload = payload or {}
        source = payload.get("source") or {}
        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id:
            return
        self.sources.setdefault(source_id, source.get("display_label") or source.get("label") or source_id)
        self.kinds.setdefault(source_id, source.get("kind") or "")

        paths = payload.get("paths") or []
        if payload.get("truncated_at_depth") is not None and not paths:
            self.truncated_sources.add(source_id)
        for path in paths:
            if isinstance(path, Mapping):
                self._absorb_path(source_id, path)

    def _absorb_path(self, source_id: str, path: Mapping[str, Any]) -> None:
        hops = [hop for hop in path.get("hops") or [] if isinstance(hop, Mapping)]
        walked: list[str] = []
        for hop in hops:
            node = hop.get("node") or {}
            node_id = node.get("id")
            if not isinstance(node_id, str) or not node_id:
                continue
            walked.append(node_id)
            depth = len(walked) - 1
            self.kinds[node_id] = node.get("kind") or self.kinds.get(node_id, "")
            if depth == 0:
                continue  # the source itself
            if depth == 1:
                self.rings[source_id].add(node_id)
            existing = self.nodes.get(node_id)
            if existing is None or depth < existing.depth:
                self.nodes[node_id] = ReachedNode(
                    id=node_id,
                    kind=node.get("kind") or "",
                    label=node.get("display_label") or node.get("label") or node_id,
                    depth=depth,
                    path=list(walked),
                )

    def ring(self, source_id: str) -> set[str]:
        return set(self.rings.get(source_id, set()))

    def kind_counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for node in self.nodes.values():
            counts[node.kind or "unknown"] += 1
        return dict(counts)


# ---------------------------------------------------------------------------
# Severity: the same traversal, two branches
# ---------------------------------------------------------------------------


def severity_of(node_id: str, before: ReachSet, after: ReachSet) -> str:
    """Compare the base branch against the proposed one for a single node."""
    was = node_id in before.nodes
    now = node_id in after.nodes

    if was and not now:
        return "outage"
    if not was and now:
        # Newly reachable. The change opened a path rather than closing one,
        # which is exposure to report but not damage to score.
        return "ok"
    if not was and not now:
        return "unknown"
    if after.nodes[node_id].depth > before.nodes[node_id].depth:
        # Still reachable, but the short way round went away.
        return "degraded"
    return "ok"


def _worst(*severities: str) -> str:
    return max(severities, key=lambda severity: _SEVERITY_RANK.get(severity, 1))


def component_severity(component: Mapping[str, Any], before: ReachSet, after: ReachSet) -> str:
    """Severity for one application component, redundancy included.

    The branch comparison answers reachability; `instances` answers redundancy,
    and it cuts both ways. A component with a copy that is still reachable cannot
    be a full outage however its own path fared — and a component that kept its
    path but lost a copy is not untouched either. A component with no instance
    recorded cannot be called safe at all, because there is nothing to divide by.

    The exception is a component the change *newly* reaches: redundancy answers
    "was there a copy to fall back on", and nothing fell over. Without this,
    every addition reports as unknown on a dataset that does not model instances,
    and a branch that only adds circuits can never clear the confidence floor.
    """
    component_id = str(component["id"])
    base = severity_of(component_id, before, after)
    if base == "ok" and component_id not in before.nodes:
        return "ok"
    instances = list(component.get("instances") or [])
    if not instances:
        return base if base != "ok" else "unknown"

    live = [instance for instance in instances if instance in after.nodes]
    lost = [instance for instance in instances if instance in before.nodes and instance not in after.nodes]
    if not live and not lost:
        # No instance was decided on either branch, so redundancy is unknowable
        # even though instances are recorded.
        return base if base != "ok" else "unknown"
    if lost and not live:
        # Every copy the change could reach is gone, whatever the component node
        # itself still looks like.
        return "outage"
    severity = _worst(base, "degraded") if lost else base
    return "degraded" if live and severity == "outage" else severity


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


@dataclass
class ApplicationImpact:
    """One application in the blast radius."""

    id: str
    name: str
    criticality: str = ""
    environment: str = ""
    components: list[str] = field(default_factory=list)
    #: Empty until a component (or the application node itself) has been scored.
    severity: str = ""

    @property
    def weight(self) -> float:
        return EXPOSURE_WEIGHT.get(self.criticality, 1.0)

    @property
    def score(self) -> float:
        return self.weight * SEVERITY_WEIGHT.get(self.severity, 0.0)


@dataclass
class ExposureIndex:
    """What `impact_exposure` found for the reachable id set."""

    applications: dict[str, ApplicationImpact] = field(default_factory=dict)
    #: component id -> {id, name, app_id, segment_id, instances}
    components: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: segment id -> name, for segments no component was found on.
    orphan_segments: dict[str, str] = field(default_factory=dict)
    customers: dict[str, str] = field(default_factory=dict)
    #: account id -> name, for accounts whose `account_role` is `network`.
    network_accounts: dict[str, str] = field(default_factory=dict)


def index_exposure(payloads: Iterable[Mapping[str, Any] | None]) -> ExposureIndex:
    """Fold `impact_exposure` responses into an index.

    Several payloads because the id set is queried in batches, and because the
    query is run on both branches: an object deleted on the proposed branch
    still has to be named in the report, and only the base branch can name it.
    """
    index = ExposureIndex()
    segments: dict[str, str] = {}
    segments_with_components: set[str] = set()

    for payload in payloads:
        cleaned = clean_data(dict(payload or {}))
        for root, nodes in cleaned.items():
            if not isinstance(nodes, list):
                continue
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                    continue
                if root in _APP_ROOTS:
                    _index_application(index, node)
                elif root in _COMPONENT_ROOTS:
                    segment_id = _index_component(index, node)
                    if segment_id:
                        segments_with_components.add(segment_id)
                elif root in _SEGMENT_ROOTS:
                    segments[node["id"]] = node.get("name") or node["id"]
                elif root in _CUSTOMER_ROOTS:
                    index.customers[node["id"]] = node.get("name") or node["id"]
                elif root in _ACCOUNT_ROOTS and (node.get("account_role") or "") == "network":
                    index.network_accounts[node["id"]] = node.get("name") or node["id"]

    # A segment with no component on it is exposure of unknown size, not zero.
    index.orphan_segments = {
        segment_id: name for segment_id, name in segments.items() if segment_id not in segments_with_components
    }
    return index


def _application_from(node: Mapping[str, Any]) -> ApplicationImpact:
    return ApplicationImpact(
        id=str(node["id"]),
        name=node.get("name") or str(node["id"]),
        criticality=str(node.get("criticality") or "").lower(),
        environment=str(node.get("environment") or ""),
    )


def _index_application(index: ExposureIndex, node: Mapping[str, Any]) -> None:
    app = index.applications.setdefault(str(node["id"]), _application_from(node))
    if not app.criticality:
        app.criticality = str(node.get("criticality") or "").lower()
    for child in node.get("children") or []:
        if not isinstance(child, dict) or not isinstance(child.get("id"), str):
            continue
        if child["id"] not in index.components:
            index.components[child["id"]] = {
                "id": child["id"],
                "name": child.get("name") or child["id"],
                "app_id": app.id,
                "segment_id": (child.get("network_segment") or {}).get("id"),
                "instances": [],
            }
            app.components.append(str(child.get("name") or child["id"]))


def _index_component(index: ExposureIndex, node: Mapping[str, Any]) -> str | None:
    parent = node.get("parent") or {}
    app_id = parent.get("id") if isinstance(parent, dict) else None
    app = None
    if isinstance(app_id, str):
        app = index.applications.setdefault(app_id, _application_from(parent))
        if not app.criticality:
            app.criticality = str(parent.get("criticality") or "").lower()

    component_id = str(node["id"])
    segment_id = (node.get("network_segment") or {}).get("id")
    instances = [
        instance["id"] for instance in node.get("instances") or [] if isinstance(instance, dict) and instance.get("id")
    ]
    existing = index.components.get(component_id)
    if existing is None:
        index.components[component_id] = {
            "id": component_id,
            "name": node.get("name") or component_id,
            "app_id": app.id if app else None,
            "segment_id": segment_id,
            "instances": instances,
        }
        if app:
            app.components.append(str(node.get("name") or component_id))
    else:
        # Reached through more than one root, or on both branches; keep
        # whichever answer knew more.
        existing["app_id"] = existing["app_id"] or (app.id if app else None)
        existing["segment_id"] = existing["segment_id"] or segment_id
        existing["instances"] = existing["instances"] or instances
        if app and existing["name"] not in app.components:
            app.components.append(str(existing["name"]))
    return segment_id if isinstance(segment_id, str) else None


# ---------------------------------------------------------------------------
# Scoring and verdict
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One statement about the change, reported whatever the score says."""

    code: str
    level: str  # "error" drives the Proposed Change to fail; "info" does not.
    message: str
    object_id: str | None = None
    object_kind: str | None = None


@dataclass
class RiskReport:
    verdict: str = "PASS"
    score: float = 0.0
    confidence: float = 1.0
    impacted: int = 0
    unknown: int = 0
    applications: list[ApplicationImpact] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    kinds: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Serialisable form, for an artifact or an MCP response."""
        return {
            "verdict": self.verdict,
            "score": round(self.score, 2),
            "confidence": round(self.confidence, 2),
            "impacted_nodes": self.impacted,
            "unknown_nodes": self.unknown,
            "impacted_kinds": self.kinds,
            "applications": [
                {
                    "id": app.id,
                    "name": app.name,
                    "criticality": app.criticality,
                    "environment": app.environment,
                    "severity": app.severity,
                    "weight": app.weight,
                    "score": round(app.score, 2),
                    "components": app.components,
                }
                for app in self.applications
            ],
            "findings": [
                {
                    "code": finding.code,
                    "level": finding.level,
                    "message": finding.message,
                    "object_id": finding.object_id,
                    "object_kind": finding.object_kind,
                }
                for finding in self.findings
            ],
        }


def build_report(
    changes: Iterable[ChangedNode],
    before: ReachSet,
    after: ReachSet,
    exposure: ExposureIndex,
    unresolved_edges: Iterable[tuple[str, str]] = (),
) -> RiskReport:
    """Fold the two traversals and the exposure query into a verdict."""
    change_list = list(changes)
    unknown_ids: set[str] = set()

    for component in exposure.components.values():
        severity = component_severity(component, before, after)
        component["severity"] = severity
        if severity == "unknown":
            unknown_ids.add(str(component["id"]))
        app = exposure.applications.get(component.get("app_id") or "")
        if app:
            app.severity = _worst(app.severity, severity) if app.severity else severity

    for app in exposure.applications.values():
        if not app.severity:
            # An application node with nothing resolved below it: score the node.
            app.severity = severity_of(app.id, before, after)
        if app.severity == "unknown":
            unknown_ids.add(app.id)

    unknown_ids |= set(exposure.orphan_segments)

    all_ids = set(before.nodes) | set(after.nodes)
    # A truncated radius under-reports, so the sources it under-reported for
    # count against confidence rather than silently scoring zero.
    truncated = before.truncated_sources | after.truncated_sources
    unknown_ids |= truncated
    total = len(all_ids | truncated) or 1
    confidence = max(0.0, 1.0 - (len(unknown_ids) / total))

    applications = sorted(
        exposure.applications.values(),
        key=lambda app: (-app.score, -app.weight, app.name),
    )
    score = sum(app.score for app in applications)

    findings = _collect_findings(
        change_list=change_list,
        before=before,
        after=after,
        exposure=exposure,
        applications=applications,
        truncated=truncated,
        unresolved_edges=list(unresolved_edges),
    )

    blocking = [app for app in applications if app.criticality == "critical" and app.severity == "outage"]
    outages = [app for app in applications if app.severity == "outage"]
    if blocking:
        verdict = "BLOCK"
    elif outages or score >= REVIEW_SCORE or confidence < MIN_CONFIDENCE:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    findings.extend(
        _verdict_findings(
            verdict=verdict,
            score=score,
            confidence=confidence,
            blocking=blocking,
            outages=outages,
            unknown_count=len(unknown_ids),
        )
    )

    return RiskReport(
        verdict=verdict,
        score=score,
        confidence=confidence,
        impacted=len(all_ids),
        unknown=len(unknown_ids),
        applications=applications,
        findings=findings,
        kinds=after.kind_counts() or before.kind_counts(),
    )


def _collect_findings(
    change_list: list[ChangedNode],
    before: ReachSet,
    after: ReachSet,
    exposure: ExposureIndex,
    applications: list[ApplicationImpact],
    truncated: set[str],
    unresolved_edges: list[tuple[str, str]],
) -> list[Finding]:
    findings: list[Finding] = []

    # An edge the filter could not resolve is a hole in the traversal, and the
    # traversal cannot report what it never walked.
    if unresolved_edges:
        pairs = ", ".join(f"{kind}.{name}" for kind, name in unresolved_edges)
        findings.append(
            Finding(
                code="unresolved_edge",
                level="error",
                message=(
                    f"{len(unresolved_edges)} traversal edge(s) do not exist in the schema: {pairs}. "
                    "The blast radius through them was not walked."
                ),
            )
        )

    # Single-homed: one instance means there is nothing to fall back to,
    # whatever the score says.
    single_homed = [
        component for component in exposure.components.values() if len(component.get("instances") or []) == 1
    ]
    for component in single_homed[:MAX_FINDINGS_PER_CODE]:
        app = exposure.applications.get(component.get("app_id") or "")
        owner = f" of '{app.name}'" if app else ""
        findings.append(
            Finding(
                code="single_homed",
                level="info",
                message=(
                    f"Component '{component['name']}'{owner} has a single instance; "
                    "an impact on it has no second path to fall back to."
                ),
                object_id=str(component["id"]),
                object_kind="AppComponent",
            )
        )
    findings.extend(_overflow("single_homed", len(single_homed), "single-homed component(s)"))

    # Shared fate by construction: everything attached to the hub account.
    for account_id, account_name in exposure.network_accounts.items():
        findings.append(
            Finding(
                code="shared_fate",
                level="info",
                message=(
                    f"Account '{account_name}' has account_role 'network', so every spoke "
                    "attached to its transit gateway shares fate with this change."
                ),
                object_id=account_id,
                object_kind="CloudAccount",
            )
        )

    # Shared fate by path: one object on the discovered path to two applications.
    shared = _shared_fate(before, after, exposure)
    for node_id, app_ids in list(shared.items())[:MAX_FINDINGS_PER_CODE]:
        kind = after.kinds.get(node_id) or before.kinds.get(node_id) or "node"
        reached = after.nodes.get(node_id) or before.nodes.get(node_id)
        label = reached.label if reached else node_id
        names = sorted(exposure.applications[app_id].name for app_id in app_ids if app_id in exposure.applications)
        findings.append(
            Finding(
                code="shared_fate",
                level="info",
                message=f"{kind} '{label}' is on the path to {len(app_ids)} applications: {', '.join(names)}.",
                object_id=node_id,
                object_kind=kind,
            )
        )
    findings.extend(_overflow("shared_fate", len(shared), "shared-fate object(s)"))

    # A modified attribute whose meaning this check does not model is a change
    # of unknown severity, not a change of no severity.
    unmodelled = [
        change
        for change in change_list
        if change.action == "updated"
        and change.id not in exposure.components
        and change.id not in exposure.applications
    ]
    for change in unmodelled[:MAX_FINDINGS_PER_CODE]:
        elements = ", ".join(sorted(name for name in change.elements if name)) or "no named element"
        findings.append(
            Finding(
                code="unmodelled_change",
                level="info",
                message=(
                    f"{change.kind} '{change.label}' changed ({elements}); this check does not model "
                    "what that change does, so its severity is unknown."
                ),
                object_id=change.id,
                object_kind=change.kind,
            )
        )
    findings.extend(_overflow("unmodelled_change", len(unmodelled), "unmodelled change(s)"))

    if truncated:
        findings.append(
            Finding(
                code="truncated",
                level="info",
                message=(
                    f"The traversal hit its result cap for {len(truncated)} source(s), so their radius "
                    f"is larger than reported. Impact beyond the cap is unknown, not zero."
                ),
            )
        )

    # Reported as a warning rather than swallowed: a server that refuses the
    # traversal produces the same empty result as a change that reaches nothing,
    # and only this finding tells the two apart.
    errors = {**before.errors, **after.errors}
    for source_id, message in list(errors.items())[:MAX_FINDINGS_PER_CODE]:
        label = after.sources.get(source_id) or before.sources.get(source_id) or source_id
        findings.append(
            Finding(
                code="traversal_failed",
                # Informational, like every non-verdict finding: the failure
                # lands in the score through confidence, which is what forces
                # REVIEW. Failing the Proposed Change here would fail it for an
                # Infrahub hiccup rather than for anything about the change.
                level="info",
                message=(
                    f"The traversal from '{label}' failed ({message}), so its blast radius is unknown "
                    "rather than empty."
                ),
                object_id=source_id,
                object_kind=after.kinds.get(source_id) or before.kinds.get(source_id),
            )
        )
    findings.extend(_overflow("traversal_failed", len(errors), "failed traversal(s)"))

    for source_id in sorted(after.missing_sources):
        findings.append(
            Finding(
                code="source_removed",
                level="info",
                message=(
                    f"Changed object {before.sources.get(source_id, source_id)} does not exist on the "
                    "proposed branch; its blast radius was measured from the base branch and from what "
                    "remains next to it."
                ),
                object_id=source_id,
                object_kind=before.kinds.get(source_id),
            )
        )

    if not applications and not exposure.orphan_segments and change_list:
        findings.append(
            Finding(
                code="no_exposure",
                level="info",
                message=(
                    f"No application or segment was reached from {len(change_list)} changed object(s). "
                    "Either the change is genuinely isolated or the path is not modelled."
                ),
            )
        )
    return findings


def _shared_fate(before: ReachSet, after: ReachSet, exposure: ExposureIndex) -> dict[str, set[str]]:
    """Objects on the discovered path to two or more applications."""
    shared: dict[str, set[str]] = defaultdict(set)
    for component in exposure.components.values():
        app_id = component.get("app_id")
        if not app_id:
            continue
        reached = after.nodes.get(str(component["id"])) or before.nodes.get(str(component["id"]))
        if reached is None:
            continue
        for hop in reached.path:
            kind = after.kinds.get(hop) or before.kinds.get(hop) or ""
            if kind in SHARED_FATE_KINDS:
                shared[hop].add(str(app_id))
    return {node_id: app_ids for node_id, app_ids in shared.items() if len(app_ids) >= 2}


def _overflow(code: str, total: int, noun: str) -> list[Finding]:
    if total <= MAX_FINDINGS_PER_CODE:
        return []
    return [
        Finding(
            code=code,
            level="info",
            message=f"… and {total - MAX_FINDINGS_PER_CODE} more {noun} not listed individually.",
        )
    ]


def _verdict_findings(
    verdict: str,
    score: float,
    confidence: float,
    blocking: list[ApplicationImpact],
    outages: list[ApplicationImpact],
    unknown_count: int,
) -> list[Finding]:
    findings: list[Finding] = []
    for app in blocking:
        findings.append(
            Finding(
                code="critical_outage",
                level="error",
                message=(
                    f"Critical application '{app.name}' ({app.environment or 'no environment'}) loses its path: "
                    f"components {', '.join(app.components) or 'unnamed'}."
                ),
                object_id=app.id,
                object_kind="AppApplication",
            )
        )

    if verdict != "REVIEW":
        return findings

    for app in outages:
        if app in blocking:
            continue
        findings.append(
            Finding(
                code="outage",
                level="error",
                message=f"Application '{app.name}' ({app.criticality or 'unrated'}) loses its path.",
                object_id=app.id,
                object_kind="AppApplication",
            )
        )
    if score >= REVIEW_SCORE and not outages:
        findings.append(
            Finding(
                code="score_threshold",
                level="error",
                message=f"Weighted impact score {score:.1f} is at or above the review threshold {REVIEW_SCORE:.1f}.",
            )
        )
    if confidence < MIN_CONFIDENCE:
        findings.append(
            Finding(
                code="low_confidence",
                level="error",
                message=(
                    f"Confidence {confidence:.0%} is below {MIN_CONFIDENCE:.0%}: the fate of "
                    f"{unknown_count} impacted object(s) could not be determined. A change whose "
                    "impact cannot be determined is not a low-risk change."
                ),
            )
        )
    return findings
