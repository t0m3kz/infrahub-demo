"""Group contracts of one fabric, deny by default: the segments' rules as rows between group tags.

One builder for every reader (the segmentation_matrix artifact, the
segment_security consistency check, and the device renderers to come).

Groups (``segment_groups``):

- a **segment group** per routed tenant segment (VLAN/VXLAN, gateway in a
  PROD/NON-PROD namespace) with its SecurityTag; its hosts are classified by
  the segment (VLAN, VNI or prefix). Intra-group traffic is permitted, except
  in a microsegmented segment, where it is denied and only component
  contracts open it;
- a **component group** per tagged AppComponent of a microsegmented segment
  (its instances talk to each other freely: intra-group permit);
- ``UNCLASSIFIED``, a pseudo-group: everything no selector classifies.

Rows (``get_vrf_contracts``), per VRF and ordered (source, destination) pair,
each a permit of exactly the listed (protocol, ports); the VRF is
``DEFAULT_ACTION`` (deny), so a pair without a row is denied. Every row comes
from an enabled permit SecurityPolicyRule (``segment_flows``, the one rule
view of the leaf ACL, the PBR bypass and the firewall):

- a rule between two tagged segments of one VRF: the source side's groups
  to the destination side's (``rule_groups``), bypassed or
  firewall-inspected alike. A side that names a component tag is that
  component's group only: an intra-segment rule of a microsegmented segment,
  and a component dependency into or out of one (the application generator
  writes the tag of every microsegmented side, RulesPlanner.rule_tags), so a
  dependency on one component opens that component, not its neighbours.
  Any other side is every group of its segment, like the SVI ACL;
- **service legs.** The firewall and load-balancer legs and the transit
  VLANs are never classified (their SVIs are shared by contexts and
  customers), so a flow that may cross a firewall context (PBR redirects
  every inter-segment flow by default, the leaf-local transit leg) gets the
  same entries on both legs: source groups -> UNCLASSIFIED and UNCLASSIFIED ->
  destination groups. Whether the SGACL sees the original destination or the
  firewall's next hop (U) then gives the same verdict;
- a rule with one side outside the VRF's tagged segments (internet,
  MANAGEMENT services, another VRF, a cloud, a prefix selector, an untagged
  segment): the tagged side to or from UNCLASSIFIED, since such traffic
  reaches the fabric through a firewall exchange leg or a border;
- nothing else. Two customers' segments with no rule between them have no
  row: denied. A deny rule is no row (the default denies); a deny ordered
  before an overlapping permit in one policy is not modelled (none in data).
  A policy whose default action is permit is not mirrored (none in data):
  group policy stays deny by default.

Platform views map the neutral rows onto one enforcement model:

- ``vxlan_gpo_view`` (NX-OS GPO): group IDs, the profile's reserved groups
  (``unclassified_group_id``, ``unaware_group_id``: data, never a number
  here), and the **policy-unaware rule**: traffic from a site outside the
  reference fabrics (DC12 under DCNM, a colocation BGW) arrives with the
  unaware group, so every row end whose segment is also active on such a site
  gets the same row with the unaware group, and a stretched group with
  intra-group permit gets unaware <-> group permit-all (its other half). The
  unaware group cannot tell customers apart: the union of every such row is
  what any policy-unaware source may send (the reference_fabric check warns
  about each stretched segment; L0 still holds at the source SVI);
- ``segment_security_view`` (EOS MSS-G): segments by prefix, policies under
  the destination segment (``from <source>``), UNCLASSIFIED as the
  destination's fallback policy. MSS-G classifies remote sources by prefix
  too, so it needs no unaware group; it only enforces traffic to a segment, so
  rows to UNCLASSIFIED are dropped; component groups inside one segment are
  not expressible by prefix and are listed as unsupported.

Rows are forward only: replies are the renderer's (``REPLIES``). Sorted,
de-duplicated and id-free.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any

from transforms.helpers.policy import segment_flows
from utils.exchange_transit import is_routed_tenant_segment
from utils.group_policy import segment_sites, tags_by_segment

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

MICROSEGMENTED = "microsegmented"

Entry = tuple[str, int | None, int | None]


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
        microsegmented = segment.get("isolation_mode") == MICROSEGMENTED
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


def get_vrf_contracts(
    segments: Iterable[dict[str, Any]], tags: Iterable[dict[str, Any]], *, local: set[str] | None = None
) -> list[dict[str, Any]]:
    """The permit rows of every VRF (module docstring), sorted by (vrf, src, dst).

    ``segments`` are every segment of the fleet whose rules may name a
    classified segment (cleaned SegmentGroupPolicyFields: every rule lives in
    its source segment's policy, so the rules of all segments are all rules);
    ``tags`` every SecurityTag with its components. ``local`` (group names)
    keeps the rows with at least one local end: a data center or a device
    scope; None keeps all, and the local rows of any partition of the groups
    add up to all of them.
    """
    segments = list(segments)
    by_segment, by_tag = segment_groups(segments, tags)
    rows = _Rows()
    for segment in segments:
        for rule in segment_flows(segment)[0]["rules"]:
            if rule.get("action") != "permit":
                continue
            entry, name = _entry(rule), str(rule.get("name") or rule.get("id"))
            src_id = str((rule.get("source_segment") or {}).get("id") or segment.get("id"))
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


# ---------------------------------------------------------------------------
# Platform views
# ---------------------------------------------------------------------------


def vxlan_gpo_view(
    groups: Iterable[Group],
    rows: Iterable[dict[str, Any]],
    *,
    reserved: dict[str, int | None],
    reference_sites: set[str],
    local: set[str] | None = None,
) -> dict[str, Any]:
    """The contract set by group ID, with the reserved groups of the profile (module docstring).

    ``groups`` are every group the rows name; ``reserved`` the profile's
    ``unclassified`` / ``unaware`` group IDs (None: the row cannot be rendered
    until the profile names one); ``reference_sites`` the site ids whose
    VTEPs are policy-aware; ``local`` the groups whose stretched halves get
    their intra-group row (None: all). A group's own ID is on the wire where
    it is active on a reference site; where it is also (or only) active
    elsewhere, it arrives as the unaware group. A row whose ends would both be
    reserved is dropped.
    """
    by_name = {group.name: group for group in groups}
    unaware = (UNAWARE, reserved.get("unaware"), True)

    def _ends(name: str) -> list[tuple[str, int | None, bool]]:
        """(name, group id, reserved) of every group a row end arrives as."""
        if name == UNCLASSIFIED:
            return [(UNCLASSIFIED, reserved.get("unclassified"), True)]
        group = by_name[name]
        aware, unaware_too = bool(group.sites & reference_sites), bool(group.sites - reference_sites)
        return [
            *([(name, group.group_id, False)] if aware or not group.sites else []),
            *([unaware] if unaware_too else []),
        ]

    merged: dict[tuple[str, str, str], tuple[int | None, int | None, set[Entry], set[str]]] = {}

    def _add(
        vrf: str | None, src: tuple[str, int | None, bool], dst: tuple[str, int | None, bool], row: dict[str, Any]
    ) -> None:
        if src[2] and dst[2]:
            return
        _src_id, _dst_id, entries, rules = merged.setdefault(
            (vrf or "", src[0], dst[0]), (src[1], dst[1], set(), set())
        )
        entries.update((item["protocol"], item["port_start"], item["port_end"]) for item in row["entries"])
        rules.update(row["rules"])

    for row in rows:
        for src, dst in product(_ends(row["src"]), _ends(row["dst"])):
            _add(row["vrf"], src, dst, row)
    permit_all = {"entries": [{"protocol": "any", "port_start": None, "port_end": None}], "rules": []}
    for group in by_name.values():
        stretched = group.sites & reference_sites and group.sites - reference_sites
        if stretched and group.intra_group == "permit" and (local is None or group.name in local):
            own = (group.name, group.group_id, False)
            _add(group.vrf, unaware, own, permit_all)
            _add(group.vrf, own, unaware, permit_all)

    return {
        "default_action": DEFAULT_ACTION,
        "reserved": dict(sorted(reserved.items())),
        "replies": REPLIES,
        "contracts": [
            {
                "vrf": vrf or None,
                "src": src,
                "dst": dst,
                "src_group_id": src_id,
                "dst_group_id": dst_id,
                "entries": _entries(entries),
                "rules": sorted(rules),
            }
            for (vrf, src, dst), (src_id, dst_id, entries, rules) in sorted(merged.items())
        ],
    }


def segment_security_view(
    groups: Iterable[Group],
    rows: Iterable[dict[str, Any]],
    prefixes: dict[str, str | None],
    *,
    local: set[str] | None = None,
) -> dict[str, Any]:
    """The contract set as MSS-G segments by prefix, policies under the destination (module docstring).

    ``groups`` are every group the rows name, ``local`` the segment groups to
    list (None: all); ``prefixes``: segment name -> its gateway prefix (the
    segment's definition). A component group counts as its segment (its hosts
    are in the segment's prefix).
    """
    by_name = {group.name: group for group in groups}
    segment_group = {group.segment: group.name for group in by_name.values() if group.kind == "segment"}
    policies: dict[tuple[str, str], tuple[set[Entry], set[str]]] = {}
    unsupported: list[dict[str, Any]] = []
    for row in rows:
        dst, src = by_name.get(row["dst"]), by_name.get(row["src"])
        if dst is None:
            continue
        if src is not None and src.segment == dst.segment and "component" in (src.kind, dst.kind):
            unsupported.append(
                {
                    "src": row["src"],
                    "dst": row["dst"],
                    "rules": row["rules"],
                    "reason": "component groups inside one segment are not expressible by prefix",
                }
            )
            continue
        source = FALLBACK if src is None else segment_group[src.segment]
        entries, rules = policies.setdefault((segment_group[dst.segment], source), (set(), set()))
        entries.update((item["protocol"], item["port_start"], item["port_end"]) for item in row["entries"])
        rules.update(row["rules"])

    def _policies(segment: str) -> list[dict[str, Any]]:
        return [
            {"from": source, "entries": _entries(entries), "rules": sorted(rules)}
            for (dst, source), (entries, rules) in sorted(policies.items())
            if dst == segment and source != FALLBACK
        ]

    def _fallback(segment: str) -> dict[str, Any] | None:
        found = policies.get((segment, FALLBACK))
        return {"entries": _entries(found[0]), "rules": sorted(found[1])} if found else None

    listed = sorted(
        (group for group in by_name.values() if group.kind == "segment" and (local is None or group.name in local)),
        key=lambda group: group.name,
    )
    return {
        "default_action": DEFAULT_ACTION,
        "replies": "stateless (AVD): a reply needs its own policy at the source segment (I/LS)",
        "segments": [
            {
                "name": group.name,
                "vrf": group.vrf,
                "prefix": prefixes.get(group.segment),
                "intra_segment": group.intra_group,
                "policies": _policies(group.name),
                "fallback": _fallback(group.name),
            }
            for group in listed
        ],
        "unsupported": sorted(unsupported, key=lambda item: (item["src"], item["dst"])),
    }
