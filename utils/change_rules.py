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
STATUS_WORDS = {ADDED: "Added", UPDATED: "Updated", REMOVED: "Removed"}

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
    {"ManagedBGP", "ManagedBGPPeering", "ManagedVRF", "TopologyCarrierCircuit", "TopologySessionCircuit"}
)
ROUTING_KIND_PREFIXES = ("RoutingPolicy", "RoutingPrefixList")
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

LABELS_PER_KIND = 5
DESCRIPTION_MAX_CHARS = 4000
DIFF_TRUNCATED_NOTE = "(the data diff is longer than the part read; the counts are the server's)"


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


@dataclass(frozen=True)
class DiffNode:
    """One node of the data diff."""

    kind: str
    label: str
    status: str
    uuid: str = ""


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
    diff_nodes: Sequence[DiffNode]
    diff_counts: Mapping[str, int] = field(default_factory=dict)
    diff_truncated: bool = False
    devices: Sequence[DeviceFacts] = ()
    customers: Sequence[str] = ()
    prod_objects: bool = False
    conflicts: Mapping[str, Sequence[str]] = field(default_factory=dict)
    current_planned_start: datetime | None = None


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


def parse_diff_nodes(nodes: Iterable[Mapping[str, Any]]) -> list[DiffNode]:
    """The changed nodes of ``DiffTree``: unchanged parents and bookkeeping kinds dropped."""
    return [
        DiffNode(
            kind=str(n.get("kind")),
            label=str(n.get("label") or n.get("uuid")),
            status=str(n.get("status")),
            uuid=str(n.get("uuid") or ""),
        )
        for n in nodes
        if n.get("status") in STATUS_ORDER and n.get("kind") not in BOOKKEEPING_KINDS
    ]


CUSTOMER_KIND = "OrganizationCustomer"
PAIR_KINDS = frozenset({"ManagedMLAG", "ManagedFirewallHA", "ManagedLoadbalancerHA"})
OFFICE_KIND = "TopologyCustomerOffice"
OWNER_ROOTS = ("ManagedNetworkSegment", "TopologyCustomer", OFFICE_KIND, "AppApplication")
"""The roots of change_request_facts.gql whose owner is an affected customer."""
OWNER_DIFF_KINDS = frozenset(
    {
        "ManagedVxlanSegment",
        "CloudNetworkSegment",
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerCloud",
        OFFICE_KIND,
        "AppApplication",
    }
)
"""The changed kinds of the data diff whose owner the facts query reads (a removed one is gone from the branch)."""


def _customer(owner: Any) -> tuple[str, str] | None:
    """(id, name) of an owner that is a customer (a provider or our own organisation is not)."""
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


def affected_customers(cleaned: Mapping[str, Any]) -> dict[str, str]:
    """Customer id -> name: the owners of the devices, of their footprints and of the changed objects."""
    owners = []
    for row in cleaned.get("DcimDeviceManaged") or []:
        owners += [row.get("owner"), (row.get("deployment") or {}).get("owner")]
    for root in OWNER_ROOTS:
        owners += [row.get("owner") for row in cleaned.get(root) or []]
    return dict(sorted(customer for owner in owners if (customer := _customer(owner))))


def prod_objects(cleaned: Mapping[str, Any]) -> bool:
    """A changed object is PROD: a PROD footprint, or an office (always PROD)."""
    footprints = cleaned.get("TopologyCustomer") or []
    return bool(cleaned.get(OFFICE_KIND)) or any(row.get("environment") == PROD_ENVIRONMENT for row in footprints)


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
    """Added, updated and removed: counted from the nodes read, or the server's summary when the read was cut."""
    if inputs.diff_truncated:
        return {status: int(inputs.diff_counts.get(status, 0)) for status in STATUS_ORDER}
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for node in inputs.diff_nodes:
        counts[node.status] += 1
    return counts


def _is_routing_kind(kind: str) -> bool:
    return kind in ROUTING_KINDS or kind.startswith(ROUTING_KIND_PREFIXES)


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
    if any(_is_routing_kind(node.kind) for node in inputs.diff_nodes):
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
    low; else normal. A diff read only in part is never standard: the kinds not read are unknown."""
    if inputs.branch.startswith(HOTFIX_PREFIX):
        return "emergency"
    counts = diff_counts(inputs)
    kinds = {node.kind for node in inputs.diff_nodes}
    if not inputs.diff_truncated and kinds and kinds <= STANDARD_KINDS and not counts[REMOVED] and risk == "low":
        return "standard"
    return "normal"


def short_description(inputs: ChangeInputs) -> str:
    """``<pc>: <n> added, <n> updated, <n> removed objects; <n> device configs``."""
    counts = diff_counts(inputs)
    configs = len(inputs.artifacts)
    return (
        f"{inputs.pc_name}: {counts[ADDED]} added, {counts[UPDATED]} updated, {counts[REMOVED]} removed objects; "
        f"{configs} device config{'' if configs == 1 else 's'}"
    )


def description(inputs: ChangeInputs) -> str:
    """The changed objects by status then kind (count and up to LABELS_PER_KIND labels), then the PC description.

    The object listing is capped at DESCRIPTION_MAX_CHARS; the PC description always follows in full."""
    grouped: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for node in inputs.diff_nodes:
        grouped[node.status][node.kind].append(node.label)
    lines: list[str] = []
    for status in STATUS_ORDER:
        if status not in grouped:
            continue
        lines.append(f"{STATUS_WORDS[status]}:")
        for kind in sorted(grouped[status]):
            labels = sorted(grouped[status][kind])
            shown = ", ".join(labels[:LABELS_PER_KIND])
            more = f" (+{len(labels) - LABELS_PER_KIND} more)" if len(labels) > LABELS_PER_KIND else ""
            lines.append(f"  {kind} ({len(labels)}): {shown}{more}")
    if inputs.diff_truncated:
        lines.append(DIFF_TRUNCATED_NOTE)
    listing = "\n".join(lines) or "No object changes."
    if len(listing) > DESCRIPTION_MAX_CHARS:
        listing = listing[: DESCRIPTION_MAX_CHARS - 4].rstrip() + "\n..."
    pc_text = (inputs.pc_description or "").strip()
    return f"{listing}\n\n{pc_text}" if pc_text else listing


def _artifacts_by_target(inputs: ChangeInputs) -> dict[str, list[ArtifactChange]]:
    by_target: dict[str, list[ArtifactChange]] = defaultdict(list)
    for artifact in inputs.artifacts:
        by_target[artifact.target].append(artifact)
    return by_target


def _ordered_targets(inputs: ChangeInputs) -> list[str]:
    """The artifact targets in rollout order: least critical role first, then name; the peer of an MLAG or HA pair
    right after its first member, so one member is verified before the other is touched."""
    facts = {device.name: device for device in inputs.devices}
    targets = sorted(_artifacts_by_target(inputs))

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


def _checksum_step(artifact: ArtifactChange) -> str:
    if artifact.action == "removed":
        return f"{artifact.definition}: remove config {artifact.checksum_previous or '?'}"
    if not artifact.checksum_previous:
        return f"{artifact.definition}: new config {artifact.checksum_new or '?'}"
    return f"{artifact.definition}: {artifact.checksum_previous} -> {artifact.checksum_new or '?'}"


def implementation_plan(inputs: ChangeInputs) -> str:
    """One step per artifact target in rollout order, each followed by its verification."""
    by_target = _artifacts_by_target(inputs)
    if not by_target:
        return "No device configuration changes: merge the data change."
    lines = []
    step = 1
    for target in _ordered_targets(inputs):
        changes = "; ".join(_checksum_step(a) for a in by_target[target])
        lines.append(f"{step}. Deploy {target}: {changes}")
        lines.append(f"{step + 1}. Verify {target} before the next step")
        step += 2
    return "\n".join(lines)


def backout_plan(inputs: ChangeInputs) -> str:
    """The configuration each target returns to; a warning when objects are removed."""
    lines = ["Revoking the approval rolls every device back automatically."]
    for artifact in inputs.artifacts:
        if artifact.checksum_previous:
            lines.append(
                f"{artifact.target} {artifact.definition}: back to {artifact.checksum_previous} "
                f"(storage {artifact.storage_previous or '?'})"
            )
        else:
            lines.append(f"{artifact.target} {artifact.definition}: new device: config removed")
    if diff_counts(inputs)[REMOVED]:
        lines.append(
            "WARNING: the change removes objects; a configuration rollback does not restore their allocations "
            "(addresses, prefixes, numbers): re-create them on a new branch."
        )
    return "\n".join(lines)


def verification_plan(inputs: ChangeInputs) -> str:
    """The expected state of each affected device after the change."""
    lines = ["Deployment verification: the running config checksum equals the artifact checksum on every device."]
    for device in sorted(inputs.devices, key=lambda d: d.name):
        expected = f"{device.bgp_peerings} BGP peering(s) established" if device.bgp_peerings else "no BGP peering"
        lines.append(f"{device.name} ({device.role or 'no role'}): {expected}")
    return "\n".join(lines)


def communication_plan(inputs: ChangeInputs) -> str:
    """The customers to notify."""
    if not inputs.customers:
        return "No customer is affected: notify the network operations team only."
    return "Notify the affected customers: " + ", ".join(sorted(inputs.customers))


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
    targets = len(_artifacts_by_target(inputs))
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


def derive_change_request(inputs: ChangeInputs) -> dict[str, Any]:
    """Every derived field of the change request, keyed by its attribute name."""
    score = risk_score(inputs)
    risk = risk_level(score)
    kind = change_type(inputs, risk)
    start, end = planned_window(inputs, kind)
    return {
        "short_description": short_description(inputs),
        "description": description(inputs),
        "change_type": kind,
        "risk": risk,
        "risk_score": score,
        "impact": impact(inputs),
        "implementation_plan": implementation_plan(inputs),
        "backout_plan": backout_plan(inputs),
        "test_plan": verification_plan(inputs),
        "communication_plan": communication_plan(inputs),
        "conflicts": conflicts(inputs) or None,
        "planned_start": start,
        "planned_end": end,
        "artifact_fingerprint": artifact_fingerprint(inputs.artifacts),
    }
