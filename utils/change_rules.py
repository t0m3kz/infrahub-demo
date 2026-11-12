"""The fields of a change request, derived from what a Proposed Change changes. Pure: no I/O.

``derive_change_request`` is the one derivation: generators/change/change_request.py feeds it the PC, its artifact
diff (REST ``/api/diff/artifacts``), its data diff (``DiffTree``), the facts of the affected devices and customers and
the other open PCs' requests, and writes what it returns. checks/change_request.py reads ``artifact_fingerprint`` and
``effective_risk`` from here, and transforms/change/servicenow.py ``effective_risk``, so the three agree.

Every weight, threshold and window is in this module: a change of the policy is a change of one table.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from utils.fabric_roles import BORDER_ROLES, CARRIER_ROLES, FIREWALL_ROLES, RR_CLIENT_ROLES, RR_ROLES

# --- data diff ----------------------------------------------------------------------------------------------------

ADDED = "ADDED"
UPDATED = "UPDATED"
REMOVED = "REMOVED"
STATUS_ORDER = (ADDED, UPDATED, REMOVED)

CHANGE_REQUEST_KIND = "ChangeRequest"
BOOKKEEPING_KINDS = frozenset({CHANGE_REQUEST_KIND, "CoreStandardGroup", "CoreGeneratorGroup", "CoreGraphQLQueryGroup"})
"""Kinds a change moves as a side effect (the request itself, the group memberships a run writes): never counted,
listed or judged."""

STANDARD_KINDS = frozenset(
    {
        "ManagedVxlanSegment",
        "ManagedSegmentDeployment",
        "ManagedSegmentVlan",
        "AppComponent",
        "SecurityPolicyRule",
        "IpamPrefix",
        "IpamIPAddress",
    }
)
"""A change made of these kinds only (a new segment and what its generator realizes, an application component, a
policy rule, a prefix, an address), with no removal and a low risk, is a standard change."""

ROUTING_KINDS = frozenset(
    {
        "ManagedBGP",
        "ManagedBGPPeering",
        "ManagedVRF",
        "TopologyCarrierCircuit",
        "TopologySessionCircuit",
        "RoutingPolicy",
        "RoutingPolicyTerm",
        "RoutingPrefixList",
        "RoutingPrefixListEntry",
    }
)
"""A changed routing object (a process, a session, a policy or a prefix list and their entries, a VRF, a circuit)."""

# --- risk -------------------------------------------------------------------------------------------------------

HIGH_BLAST_ROLES = RR_ROLES | CARRIER_ROLES | FIREWALL_ROLES
"""Spines, the border and the edge, the firewalls: a mistake there reaches every tenant behind them."""
LOAD_BALANCER_ROLE = "load-balancer"
ACCESS_ROLES = RR_CLIENT_ROLES - BORDER_ROLES
"""Leaf, ToR and access leaf: a mistake there reaches the hosts of one rack or pod."""

ROLE_WEIGHTS: dict[str, int] = {
    **dict.fromkeys(HIGH_BLAST_ROLES, 30),
    LOAD_BALANCER_ROLE: 20,
    **dict.fromkeys(ACCESS_ROLES, 10),
}
OTHER_ROLE_WEIGHT = 3
"""The score of the most critical device of the change (the maximum, not a sum: one spine is the risk)."""
REMOVED_WEIGHT = 20
ROUTING_WEIGHT = 15
PROD_WEIGHT = 20
CUSTOMER_WEIGHT = 5
CUSTOMER_WEIGHT_CAP = 25
DEVICE_WEIGHT = 2
DEVICE_WEIGHT_CAP = 20
RISK_THRESHOLDS = ((20, "low"), (50, "moderate"))
"""score < 20 low, < 50 moderate, else high."""
RISK_LEVELS = ("low", "moderate", "high")

PROD_ZONE = "PROD"
PROD_ENVIRONMENT = "p"

# --- window -----------------------------------------------------------------------------------------------------

MAINTENANCE_TZ = ZoneInfo("Europe/Warsaw")
MAINTENANCE_WEEKDAY = 3  # Thursday
MAINTENANCE_HOUR = 22
"""The weekly maintenance window of a normal change: Thursday 22:00 Europe/Warsaw."""
STEP_MINUTES = 10
MIN_WINDOW_MINUTES = 30
HOTFIX_PREFIX = "hotfix/"

# --- text caps --------------------------------------------------------------------------------------------------

DESCRIPTION_MAX_CHARS = 4000
SECTION_MAX_ITEMS = 15
DEVICE_MAX_ITEMS = 30
OTHER_KINDS_MAX = 12
"""The caps of the text fields: the listing of a description, the items of a section, the kinds counted as other."""
DIFF_TRUNCATED_NOTE = "(more objects changed than the part read: the lists above are the first ones)"


@dataclass(frozen=True)
class ArtifactChange:
    """One entry of the artifact diff of a branch."""

    id: str
    action: str
    target_id: str
    target_kind: str
    target: str
    definition: str
    checksum_new: str | None
    checksum_previous: str | None
    storage_previous: str | None

    @property
    def change_text(self) -> str:
        """What happens to the config: ``new config <sum>``, ``remove config <sum>`` or ``<old> -> <new>``."""
        if self.action == "removed":
            return f"remove config {self.checksum_previous or '?'}"
        if not self.checksum_previous:
            return f"new config {self.checksum_new or '?'}"
        return f"{self.checksum_previous} -> {self.checksum_new or '?'}"


@dataclass(frozen=True)
class ObjectDiff:
    """One changed object of the filtered data diff (segment, rule, cable, circuit) with what the diff says of it.

    ``values`` are its attributes (``None`` when unset; the previous values on a removed object), ``changes`` the
    attributes of an updated object that changed (name -> (previous, new)), ``peers`` the labels of the peers of its
    relationships (``peer_ids`` their ids, in the same order) and ``dropped`` the peers an update removed."""

    uuid: str
    kind: str
    label: str
    status: str
    values: Mapping[str, str | None] = field(default_factory=dict)
    changes: Mapping[str, tuple[str | None, str | None]] = field(default_factory=dict)
    peers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    peer_ids: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    dropped: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class DeviceFacts:
    """What the plans say about one affected device."""

    id: str
    name: str
    role: str | None = None
    kind: str = ""
    prod: bool = False
    peer: str | None = None
    bgp_peerings: int = 0


@dataclass(frozen=True)
class ChangeInputs:
    """Everything the derivation reads."""

    pc_name: str
    pc_description: str | None
    branch: str
    now: datetime
    artifacts: Sequence[ArtifactChange]
    diff_counts: Mapping[str, int] = field(default_factory=dict)
    """Added, updated and removed objects of the data diff, bookkeeping kinds apart: the server's counts."""
    only_standard: bool = False
    """The data diff holds objects of STANDARD_KINDS only (the server's count of any other kind is zero)."""
    routing_changed: bool = False
    """The data diff holds a routing object (ROUTING_KINDS)."""
    devices: Sequence[DeviceFacts] = ()
    customers: Sequence[str] = ()
    prod_objects: bool = False
    conflicts: Mapping[str, Sequence[str]] = field(default_factory=dict)
    current_planned_start: datetime | None = None
    rows: Mapping[str, Any] = field(default_factory=dict)
    """The cleaned rows of change_request_facts.gql, by root kind (the place of a device: utils/change_facts.py)."""
    objects: Sequence[ObjectDiff] = ()
    """The segments, rules, cables, circuits, footprints and applications of the data diff, with their values."""
    objects_truncated: bool = False
    """The objects were read up to a cap (utils/change_sources.py): more changed than are described."""


# --- parsing ----------------------------------------------------------------------------------------------------


def parse_artifact_diff(payload: Mapping[str, Any] | None) -> list[ArtifactChange]:
    """The entries of ``GET /api/diff/artifacts`` (a dict keyed by artifact id), sorted by target then definition."""
    changes = []
    for artifact_id, entry in (payload or {}).items():
        if not isinstance(entry, Mapping):
            continue
        target = entry.get("target") or {}
        label = str(entry.get("display_label") or "")
        target_label = str(target.get("display_label") or "")
        prefix = f"{target_label} - "
        definition = label[len(prefix) :] if target_label and label.startswith(prefix) else label
        new = entry.get("item_new") or {}
        previous = entry.get("item_previous") or {}
        changes.append(
            ArtifactChange(
                id=str(entry.get("id") or artifact_id),
                action=str(entry.get("action") or ""),
                target_id=str(target.get("id") or ""),
                target_kind=str(target.get("kind") or ""),
                target=target_label,
                definition=definition,
                checksum_new=new.get("checksum"),
                checksum_previous=previous.get("checksum"),
                storage_previous=previous.get("storage_id"),
            )
        )
    return sorted(changes, key=lambda c: (c.target, c.definition, c.id))


NULL_VALUE = "NULL"
"""How the diff spells an unset attribute."""


def _property(attribute: Mapping[str, Any], name: str) -> Any:
    for prop in attribute.get("properties") or []:
        if prop.get("property_type") == "HAS_VALUE":
            return prop.get(name)
    return None


def _text(value: Any) -> str | None:
    return None if value is None or value == NULL_VALUE else str(value)


def parse_diff_summaries(result: Mapping[str, Any]) -> tuple[dict[str, int], bool, bool]:
    """(counts, only standard kinds, routing changed) of the aliased ``DiffTreeSummary`` roots of the diff query:
    ``total`` (bookkeeping kinds apart), ``non_standard`` and ``routing``."""

    def counts(root: str) -> dict[str, int]:
        summary = result.get(root) or {}
        return {status: int(summary.get(f"num_{status.lower()}") or 0) for status in STATUS_ORDER}

    total = counts("total")
    only_standard = bool(sum(total.values())) and not sum(counts("non_standard").values())
    return total, only_standard, bool(sum(counts("routing").values()))


def parse_object_nodes(nodes: Iterable[Mapping[str, Any]]) -> list[ObjectDiff]:
    """The objects of the filtered ``DiffTree`` read: values, changed values and peers by relationship name."""
    objects = []
    for n in nodes:
        if n.get("status") not in STATUS_ORDER:
            continue
        removed = n.get("status") == REMOVED
        values: dict[str, str | None] = {}
        changes: dict[str, tuple[str | None, str | None]] = {}
        for attribute in n.get("attributes") or []:
            new, previous = _text(_property(attribute, "new_value")), _text(_property(attribute, "previous_value"))
            values[str(attribute.get("name"))] = previous if removed else new
            if n.get("status") == UPDATED and new != previous and previous is not None:
                changes[str(attribute.get("name"))] = (previous, new)
        peers: dict[str, tuple[str, ...]] = {}
        peer_ids: dict[str, tuple[str, ...]] = {}
        dropped: dict[str, tuple[str, ...]] = {}
        for relationship in n.get("relationships") or []:
            elements = relationship.get("elements") or []
            name = str(relationship.get("name"))
            kept = sorted(
                (str(e.get("peer_label")), str(e.get("peer_id") or ""))
                for e in elements
                if e.get("status") != REMOVED or removed
            )
            peers[name] = tuple(label for label, _ in kept)
            peer_ids[name] = tuple(peer_id for _, peer_id in kept)
            if gone := tuple(
                sorted(str(e.get("peer_label")) for e in elements if e.get("status") == REMOVED and not removed)
            ):
                dropped[name] = gone
        objects.append(
            ObjectDiff(
                uuid=str(n.get("uuid") or ""),
                kind=str(n.get("kind")),
                label=str(n.get("label") or n.get("uuid")),
                status=str(n.get("status")),
                values=values,
                changes=changes,
                peers=peers,
                peer_ids=peer_ids,
                dropped=dropped,
            )
        )
    return objects


PAIR_KINDS = frozenset({"ManagedMLAG", "ManagedFirewallHA", "ManagedLoadbalancerHA"})
OFFICE_KIND = "TopologyCustomerOffice"
FOOTPRINT_KINDS = frozenset({"TopologyCustomerDC", "TopologyCustomerColocation", "TopologyCustomerCloud"})
OWNER_DIFF_KINDS = frozenset(
    {"ManagedVxlanSegment", "CloudNetworkSegment", OFFICE_KIND, "AppApplication"} | FOOTPRINT_KINDS
)
"""The changed kinds of the data diff whose ``owner`` is an affected customer (read with the objects)."""
CUSTOMER_KIND = "OrganizationCustomer"


def _customer(owner: Any) -> tuple[str, str] | None:
    """(id, name) of a device owner that is a customer (a provider or our own organisation is not)."""
    if not isinstance(owner, Mapping) or not owner.get("id"):
        return None
    if owner.get("typename", CUSTOMER_KIND) != CUSTOMER_KIND:
        return None
    return str(owner["id"]), str(owner.get("name") or owner["id"])


def device_facts(row: Mapping[str, Any]) -> DeviceFacts:
    """The facts of one cleaned ``DcimDeviceManaged`` row of change_request_facts.gql."""
    name = str(row.get("name") or row.get("id"))
    capabilities = [c for c in row.get("capabilities") or [] if isinstance(c, Mapping)]
    peers = sorted(
        str(member.get("name"))
        for capability in capabilities
        if capability.get("typename") in PAIR_KINDS
        for member in capability.get("members") or []
        if isinstance(member, Mapping) and member.get("name") and member.get("name") != name
    )
    peerings = sum(int(c.get("peerings") or 0) for c in capabilities if c.get("typename") == "ManagedBGP")
    zones = {
        (c.get("security_zone") or {}).get("name")
        for c in row.get("shared_capabilities") or []
        if isinstance(c, Mapping) and c.get("typename") == "ManagedVRF"
    }
    environment = (row.get("deployment") or {}).get("environment")
    return DeviceFacts(
        id=str(row.get("id")),
        name=name,
        role=row.get("role"),
        kind=str(row.get("typename") or ""),
        prod=PROD_ZONE in zones or environment == PROD_ENVIRONMENT,
        peer=peers[0] if peers else None,
        bgp_peerings=peerings,
    )


def affected_customers(cleaned: Mapping[str, Any], objects: Iterable[ObjectDiff] = ()) -> dict[str, str]:
    """Customer id -> name: the owners of the devices and of their footprints (change_request_facts.gql) and of the
    changed objects (their ``owner`` peers in the data diff, by label)."""
    owners = []
    for row in cleaned.get("DcimDeviceManaged") or []:
        owners += [row.get("owner"), (row.get("deployment") or {}).get("owner")]
    found = dict(sorted(customer for owner in owners if (customer := _customer(owner))))
    for changed in objects:
        if changed.kind in OWNER_DIFF_KINDS:
            found |= {
                i: label for i, label in zip(changed.peer_ids.get("owner", ()), changed.peers.get("owner", ())) if i
            }
    return dict(sorted(found.items()))


def prod_objects(objects: Iterable[ObjectDiff]) -> bool:
    """A changed object is PROD: a PROD footprint, or an office (always PROD)."""
    return any(
        o.kind == OFFICE_KIND or (o.kind in FOOTPRINT_KINDS and o.values.get("environment") == PROD_ENVIRONMENT)
        for o in objects
    )


def parse_datetime(value: Any) -> datetime | None:
    """A DateTime attribute value as an aware datetime (None when unset or unreadable)."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- derivations ------------------------------------------------------------------------------------------------


def artifact_fingerprint(artifacts: Iterable[ArtifactChange]) -> str:
    """A stable hash of the artifact diff: the sorted (artifact id, new checksum) pairs."""
    pairs = sorted(f"{a.id}:{a.checksum_new or ''}" for a in artifacts)
    return hashlib.sha256("\n".join(pairs).encode()).hexdigest()


def effective_risk(risk: str | None, override: str | None) -> str | None:
    """The override when the author set one, else the derived risk."""
    return override or risk


def risk_rank(risk: str | None) -> int:
    """The order of a risk level (low 0, moderate 1, high 2); an unknown level ranks highest."""
    return RISK_LEVELS.index(risk) if risk in RISK_LEVELS else len(RISK_LEVELS)


def diff_counts(inputs: ChangeInputs) -> dict[str, int]:
    """Added, updated and removed objects of the data diff, whatever their number."""
    return {status: int(inputs.diff_counts.get(status, 0)) for status in STATUS_ORDER}


def role_weight(role: str | None) -> int:
    """The weight of one device role."""
    return ROLE_WEIGHTS.get(role or "", OTHER_ROLE_WEIGHT)


def is_prod(inputs: ChangeInputs) -> bool:
    """PROD is affected: a device in a PROD VRF or footprint, or a changed PROD footprint."""
    return inputs.prod_objects or any(device.prod for device in inputs.devices)


def risk_score(inputs: ChangeInputs) -> int:
    """The weighted score: the most critical role, a removal, a routing change, PROD, customers and device count."""
    counts = diff_counts(inputs)
    score = max((role_weight(device.role) for device in inputs.devices), default=0)
    if counts[REMOVED]:
        score += REMOVED_WEIGHT
    if inputs.routing_changed:
        score += ROUTING_WEIGHT
    if is_prod(inputs):
        score += PROD_WEIGHT
    score += min(CUSTOMER_WEIGHT * len(inputs.customers), CUSTOMER_WEIGHT_CAP)
    score += min(DEVICE_WEIGHT * len(inputs.devices), DEVICE_WEIGHT_CAP)
    return score


def risk_level(score: int) -> str:
    """low, moderate or high from the score (RISK_THRESHOLDS)."""
    for limit, level in RISK_THRESHOLDS:
        if score < limit:
            return level
    return "high"


def impact(inputs: ChangeInputs) -> int:
    """1 when PROD and customers are affected, 2 when one of them is, else 3."""
    hits = int(is_prod(inputs)) + int(bool(inputs.customers))
    return 3 - hits


def change_type(inputs: ChangeInputs, risk: str) -> str:
    """emergency on a hotfix branch; standard when every changed kind is standard, nothing is removed and the risk is
    low; else normal."""
    if inputs.branch.startswith(HOTFIX_PREFIX):
        return "emergency"
    if inputs.only_standard and not diff_counts(inputs)[REMOVED] and risk == "low":
        return "standard"
    return "normal"


def artifacts_by_target(inputs: ChangeInputs) -> dict[str, list[ArtifactChange]]:
    """The artifact diff by target name."""
    by_target: dict[str, list[ArtifactChange]] = defaultdict(list)
    for artifact in inputs.artifacts:
        by_target[artifact.target].append(artifact)
    return by_target


def ordered_targets(inputs: ChangeInputs) -> list[str]:
    """The artifact targets in rollout order: least critical role first, then name; the peer of an MLAG or HA pair
    right after its first member, so one member is verified before the other is touched."""
    facts = {device.name: device for device in inputs.devices}
    targets = sorted(artifacts_by_target(inputs))

    def peer_of(name: str) -> str | None:
        peer = facts[name].peer if name in facts else None
        return peer if peer in targets else None

    def unit_key(name: str) -> tuple[int, str]:
        members = [name] + [peer for peer in (peer_of(name),) if peer]
        return (max(role_weight(facts[m].role if m in facts else None) for m in members), min(members))

    ordered: list[str] = []
    for name in sorted(targets, key=unit_key):
        if name in ordered:
            continue
        ordered.append(name)
        peer = peer_of(name)
        if peer and peer not in ordered:
            ordered.append(peer)
    return ordered


def next_window(now: datetime) -> datetime:
    """The start of the next weekly maintenance window strictly after ``now`` (aware, Europe/Warsaw)."""
    local = now.astimezone(MAINTENANCE_TZ)
    days = (MAINTENANCE_WEEKDAY - local.weekday()) % 7
    start = (local + timedelta(days=days)).replace(hour=MAINTENANCE_HOUR, minute=0, second=0, microsecond=0)
    if start <= local:
        start += timedelta(days=7)
    # Rebuild in the zone so a DST change between now and the window keeps 22:00 local.
    return datetime(start.year, start.month, start.day, MAINTENANCE_HOUR, tzinfo=MAINTENANCE_TZ)


def window_minutes(device_count: int) -> int:
    """The time the steps take: STEP_MINUTES per device, at least MIN_WINDOW_MINUTES."""
    return max(MIN_WINDOW_MINUTES, device_count * STEP_MINUTES)


def planned_window(inputs: ChangeInputs, kind: str) -> tuple[datetime, datetime]:
    """(start, end): a normal change in the next window; a standard or emergency one now. A start a previous run set
    that is already due is kept, so a re-run does not move it."""
    targets = len(artifacts_by_target(inputs))
    if kind == "normal":
        start = next_window(inputs.now)
    elif inputs.current_planned_start and inputs.current_planned_start <= inputs.now:
        start = inputs.current_planned_start
    else:
        start = inputs.now
    return start, start + timedelta(minutes=window_minutes(targets))


def conflicts(inputs: ChangeInputs) -> str:
    """The other open PCs whose request lists an affected device of this one."""
    mine = {device.name for device in inputs.devices}
    lines = [
        f"{pc}: {', '.join(sorted(shared))}"
        for pc, devices in sorted(inputs.conflicts.items())
        if (shared := mine & set(devices))
    ]
    return "\n".join(lines)
