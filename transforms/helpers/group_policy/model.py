"""The neutral group-policy model: groups, permit rows per VRF and one data center's contracts.

See the package docstring (``transforms.helpers.group_policy``) for the model.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any

from transforms.helpers.policy import segment_flows
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import (
    Profiles,
    is_microsegmented,
    profiles_by_platform,
    segment_sites,
    site_reason,
    tags_by_segment,
    vteps_by_data_center,
)

UNCLASSIFIED = "unclassified"
"""Pseudo-group of every endpoint no selector classifies (service and transit legs, outside the VRF's segments)."""

UNAWARE = "unaware"
"""Pseudo-group of traffic from a policy-unaware site, in the views that need it (vxlan_gpo_view)."""

DEFAULT_ACTION = "deny"
"""The VRF posture every contract set is built for: what no row permits is denied."""

REPLIES = "bidir (I/LS)"
"""How a renderer answers the reverse direction of a forward row: NX-OS renders the
contract bidir (D: its default); whether bidir reverses the L4 ports is I/LS. A
stateless renderer without bidir adds mirrored rows itself."""

FALLBACK = "fallback"
"""The MSS-G source of traffic from a prefix in no segment: the destination segment's fallback policy."""

Entry = tuple[str, int | None, int | None]


def _entry_set(items: Iterable[dict[str, Any]] | None) -> set[Entry]:
    """The set of (protocol, port_start, port_end) of the entry dicts of a row."""
    return {(item["protocol"], item["port_start"], item["port_end"]) for item in items or []}


@dataclass(frozen=True)
class Group:
    """One classified group: a segment's or a component's SecurityTag."""

    name: str
    group_id: int | None
    kind: str
    segment: str
    vrf: str | None
    customer: str | None
    intra_group: str
    sites: frozenset[str]

    def as_dict(self) -> dict[str, Any]:
        """The artifact view: everything but the site ids (id-free)."""
        return {
            "name": self.name,
            "group_id": self.group_id,
            "kind": self.kind,
            "segment": self.segment,
            "vrf": self.vrf,
            "customer": self.customer,
            "intra_group": self.intra_group,
        }


def segment_vrf(segment: dict[str, Any]) -> str | None:
    """The VRF (IPAM namespace name) of a segment's gateway."""
    prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
    return (prefix.get("ip_namespace") or {}).get("name")


def segment_groups(
    segments: Iterable[dict[str, Any]], tags: Iterable[dict[str, Any]]
) -> tuple[dict[str, list[Group]], dict[str, Group]]:
    """(segment id -> its groups, segment group first; component tag id -> its group).

    Only a routed tenant segment with a security_tag is classified; its
    component groups exist only while it is microsegmented. ``segments`` are
    cleaned SegmentGroupPolicyFields, ``tags`` cleaned SecurityTags with their
    components' segment.
    """
    components_of = tags_by_segment(tags)
    by_segment: dict[str, list[Group]] = {}
    by_component_tag: dict[str, Group] = {}
    for segment in segments:
        tag = segment.get("security_tag") or {}
        if not segment.get("id") or not tag.get("name") or not is_routed_tenant_segment(segment):
            continue
        microsegmented = is_microsegmented(segment)
        common = {
            "segment": str(segment.get("name")),
            "vrf": segment_vrf(segment),
            "customer": (segment.get("owner") or {}).get("org_id"),
            "sites": frozenset(segment_sites(segment)),
        }
        groups = [
            Group(
                name=str(tag["name"]),
                group_id=tag.get("group_id"),
                kind="segment",
                intra_group="deny" if microsegmented else "permit",
                **common,
            )
        ]
        if microsegmented:
            for component_tag in sorted(components_of.get(segment["id"], []), key=lambda item: str(item.get("name"))):
                group = Group(
                    name=str(component_tag.get("name")),
                    group_id=component_tag.get("group_id"),
                    kind="component",
                    intra_group="permit",
                    **common,
                )
                groups.append(group)
                by_component_tag[str(component_tag.get("id"))] = group
        by_segment[str(segment["id"])] = groups
    return by_segment, by_component_tag


def _entry(rule: dict[str, Any]) -> Entry:
    """(protocol, port_start, port_end) of a rule; a single port has port_end == port_start."""
    protocol = str(rule.get("protocol") or "any")
    port_start = rule.get("port_start")
    if protocol not in ("tcp", "udp") or not port_start:
        return protocol, None, None
    return protocol, int(port_start), int(rule.get("port_end") or port_start)


def _entries(entries: set[Entry]) -> list[dict[str, Any]]:
    """Sorted entry dicts; ``any`` collapses the row to permit-all."""
    if any(protocol == "any" for protocol, _start, _end in entries):
        entries = {("any", None, None)}
    return [
        {"protocol": protocol, "port_start": start, "port_end": end}
        for protocol, start, end in sorted(entries, key=lambda item: (item[0], item[1] or 0, item[2] or 0))
    ]


class _Rows:
    """Rows keyed by (vrf, src, dst), merging entries and rule names."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], tuple[set[Entry], set[str]]] = {}

    def add(self, vrf: str | None, src: str, dst: str, entry: Entry, rule: str | None) -> None:
        entries, rules = self.rows.setdefault((vrf or "", src, dst), (set(), set()))
        entries.add(entry)
        if rule:
            rules.add(rule)

    def emit(self, keep: Any = None) -> list[dict[str, Any]]:
        return [
            {"vrf": vrf or None, "src": src, "dst": dst, "entries": _entries(entries), "rules": sorted(rules)}
            for (vrf, src, dst), (entries, rules) in sorted(self.rows.items())
            if keep is None or keep(src, dst)
        ]


def rule_groups(
    rule: dict[str, Any],
    side: str,
    segment_id: str | None,
    by_segment: dict[str, list[Group]],
    by_tag: dict[str, Group],
) -> list[Group]:
    """The groups one rule side ("source"/"destination") matches ([] when its segment is not classified).

    The side's component group when the rule names one of that segment's
    component tags (an intra-segment contract, or a component dependency into
    or out of a microsegmented segment: RulesPlanner.rule_tags), else every
    group of its segment: a hand-written rule into a microsegmented segment
    opens all of it (checks/segment_security.py warns).
    """
    groups = by_segment.get(segment_id or "", [])
    tagged = by_tag.get(str((rule.get(f"{side}_security_tag") or {}).get("id")))
    return [tagged] if tagged is not None and tagged in groups else groups


def _contract_rules(segment: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """(rule, id of its default source segment) for the rules ``segment`` contributes rows for.

    Its own rules, sourced by the segment unless a rule names another; and the
    rules into it of a policy with no segment (a load-balancer context's
    ``lb-<context>``: the leg /29 -> this segment), which no segment's own policy
    holds. Such a rule has no source segment: its source is UNCLASSIFIED.
    """
    egress, ingress = segment_flows(segment)
    own = [(rule, str(segment.get("id") or "")) for rule in egress["rules"]]
    return own + [(rule, "") for rule in ingress if not (rule.get("policy") or {}).get("segment")]


def get_vrf_contracts(
    segments: Iterable[dict[str, Any]],
    tags: Iterable[dict[str, Any]],
    *,
    local: set[str] | None = None,
    groups: tuple[dict[str, list[Group]], dict[str, Group]] | None = None,
) -> list[dict[str, Any]]:
    """The permit rows of every VRF (module docstring), sorted by (vrf, src, dst).

    ``segments`` are every segment of the fleet whose rules may name a
    classified segment (cleaned SegmentGroupPolicyFields: a rule lives in its
    source segment's policy, or in a segment-less one and then is read from its
    destination's inbound_rules, so the rules of all segments are all rules);
    ``tags`` every SecurityTag with its components. ``local`` (group names)
    keeps the rows with at least one local end: a data center or a device
    scope; None keeps all, and the local rows of any partition of the groups
    add up to all of them. ``groups``: the ``segment_groups(segments, tags)`` the caller already has.
    """
    segments = list(segments)
    by_segment, by_tag = groups if groups is not None else segment_groups(segments, tags)
    rows = _Rows()
    for segment in segments:
        for rule, default_src in _contract_rules(segment):
            if rule.get("action") != "permit":
                continue
            entry, name = _entry(rule), str(rule.get("name") or rule.get("id"))
            src_id = str((rule.get("source_segment") or {}).get("id") or default_src)
            dst_id = str((rule.get("destination_segment") or {}).get("id") or "")
            src = rule_groups(rule, "source", src_id, by_segment, by_tag)
            dst = rule_groups(rule, "destination", dst_id, by_segment, by_tag)
            if src and dst and src[0].vrf == dst[0].vrf:
                for src_group, dst_group in product(src, dst):
                    if src_group != dst_group or src_group.intra_group == "deny":
                        rows.add(src_group.vrf, src_group.name, dst_group.name, entry, name)
                if src_id == dst_id:
                    continue
            # The service legs of a flow between two segments, and a flow
            # with one side outside the VRF's classified segments.
            for src_group in src:
                rows.add(src_group.vrf, src_group.name, UNCLASSIFIED, entry, name)
            for dst_group in dst:
                rows.add(dst_group.vrf, UNCLASSIFIED, dst_group.name, entry, name)
    keep = None if local is None else (lambda src, dst: src in local or dst in local)
    customer = {group.name: group.customer for groups in by_segment.values() for group in groups}

    def _cross(row: dict[str, Any]) -> bool:
        src, dst = customer.get(row["src"]), customer.get(row["dst"])
        return bool(src and dst and src != dst)

    return [{**row, "cross_customer": _cross(row)} for row in rows.emit(keep)]


@dataclass(frozen=True)
class DataCenterContracts:
    """One data center's group contracts, from the fleet-wide query roots.

    The one composition both readers share: the segmentation_matrix artifact and
    the device renderers. ``reference_sites`` is the fleet as it is (every site
    whose VTEPs are all in their platform's profile row); a reader that wants
    this data center treated as one adds it itself.

    ``enforce`` is the data center's staging switch (``group_policy_enforce``, A6): False renders the
    contracts but not ``security enforce``. ``name`` is the data center's, for that reason's text.
    """

    name: str | None
    enforce: bool
    profiles: Profiles
    vteps: list[dict[str, Any]]
    reason: str | None
    reference_sites: set[str]
    segments: list[dict[str, Any]]
    all_segments: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    components_of: dict[str, list[dict[str, Any]]]
    by_segment: dict[str, list[Group]]
    all_groups: list[Group]
    groups_by_name: dict[str, Group]
    local: list[Group]
    local_names: set[str]
    rows: list[Any]


def data_center_contracts(
    roots: dict[str, Any],
    dc_id: str,
    *,
    fleet: dict[str, list[dict[str, Any]]] | None = None,
    profiles: Profiles | None = None,
) -> DataCenterContracts:
    """The groups and contract rows of one data center (``roots``: cleaned query data).

    ``fleet`` is the fleet's VTEPs by data center (``vteps_by_data_center``) and ``profiles`` the
    ``profiles_by_platform`` of ``roots`` when the caller has already derived them.
    """
    if profiles is None:
        profiles, _problems = profiles_by_platform(roots.get("DcimGroupPolicyProfile") or [])
    vteps_of = (
        fleet
        if fleet is not None
        else vteps_by_data_center(roots.get("TopologyDataCenter") or [], roots.get("TopologyPod") or [])
    )
    vteps = vteps_of.get(dc_id, [])
    all_segments = roots.get("ManagedNetworkSegment") or []
    tags = roots.get("SecurityTag") or []
    groups = segment_groups(all_segments, tags)
    by_segment = groups[0]
    segments = sorted(
        (segment for segment in all_segments if dc_id in segment_sites(segment)),
        key=lambda segment: str(segment.get("name")),
    )
    local = [group for segment in segments for group in by_segment.get(str(segment.get("id")), [])]
    local_names = {group.name for group in local}
    data_center = next(
        (node for node in roots.get("TopologyDataCenter") or [] if str(node.get("id")) == dc_id),
        {},
    )
    reasons = {site: site_reason(found, profiles) for site, found in vteps_of.items()}
    all_groups = [group for found in by_segment.values() for group in found]
    return DataCenterContracts(
        name=data_center.get("name"),
        enforce=bool(data_center.get("group_policy_enforce")),
        profiles=profiles,
        vteps=vteps,
        reason=reasons[dc_id] if dc_id in reasons else site_reason(vteps, profiles),
        reference_sites={site for site, reason in reasons.items() if reason is None},
        segments=segments,
        all_segments=all_segments,
        tags=tags,
        components_of=tags_by_segment(tags),
        by_segment=by_segment,
        all_groups=all_groups,
        groups_by_name={group.name: group for group in all_groups},
        local=local,
        local_names=local_names,
        rows=get_vrf_contracts(all_segments, tags, local=local_names, groups=groups),
    )
