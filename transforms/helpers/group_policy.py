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

``vxlan_gpo_device_view`` is the device renderers' reader of the same contracts
(NX-OS VTEP configs, ``GPO_RENDERERS``): the ``vxlan_gpo_view`` of the device's
own data center, cut to the VRFs the device carries. Every VTEP of the data
center renders the same text for a VRF (class-maps, policy-maps, contracts);
only the ``match vlan`` selectors differ, since a VLAN ID is per VLAN domain.
It renders nothing (None) unless the data center is a reference fabric of a
kind with a renderer, and refuses a row it cannot number, a tag shared by two
segments and a group named like a pseudo-group (``... refusing unprotected
config``). A VRF is left unenforced, with the reason as a comment, while a group
it names or a tenant segment it holds on the device has no classifier.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import product
from typing import Any

from transforms.helpers.policy import segment_flows
from transforms.helpers.segments import is_inline_terminated
from utils.exchange_transit import is_routed_tenant_segment
from utils.fabric_roles import DC_VTEP_ROLES
from utils.group_policy import (
    Profiles,
    is_microsegmented,
    profiles_by_platform,
    reserved_groups,
    segment_sites,
    site_kind,
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


@dataclass(frozen=True)
class DataCenterContracts:
    """One data center's group contracts, from the fleet-wide query roots.

    The one composition both readers share: the segmentation_matrix artifact and
    the device renderers. ``reference_sites`` is the fleet as it is (every site
    whose VTEPs are all in their platform's profile row); a reader that wants
    this data center treated as one adds it itself.
    """

    profiles: Profiles
    vteps: list[dict[str, Any]]
    reason: str | None
    reference_sites: set[str]
    segments: list[dict[str, Any]]
    all_segments: list[dict[str, Any]]
    tags: list[dict[str, Any]]
    by_segment: dict[str, list[Group]]
    all_groups: list[Group]
    local: list[Group]
    local_names: set[str]
    rows: list[Any]


def data_center_contracts(
    roots: dict[str, Any], dc_id: str, *, fleet: dict[str, list[dict[str, Any]]] | None = None
) -> DataCenterContracts:
    """The groups and contract rows of one data center (``roots``: cleaned query data).

    ``fleet`` is the fleet's VTEPs by data center (``vteps_by_data_center``) when the caller has
    already derived it from ``roots``.
    """
    profiles, _problems = profiles_by_platform(roots.get("DcimGroupPolicyProfile") or [])
    vteps_of = (
        fleet
        if fleet is not None
        else vteps_by_data_center(roots.get("TopologyDataCenter") or [], roots.get("TopologyPod") or [])
    )
    vteps = vteps_of.get(dc_id, [])
    all_segments = roots.get("ManagedNetworkSegment") or []
    tags = roots.get("SecurityTag") or []
    by_segment, _by_tag = segment_groups(all_segments, tags)
    segments = sorted(
        (segment for segment in all_segments if dc_id in segment_sites(segment)),
        key=lambda segment: str(segment.get("name")),
    )
    local = [group for segment in segments for group in by_segment.get(str(segment.get("id")), [])]
    local_names = {group.name for group in local}
    return DataCenterContracts(
        profiles=profiles,
        vteps=vteps,
        reason=site_reason(vteps, profiles),
        reference_sites={site for site, found in vteps_of.items() if site_reason(found, profiles) is None},
        segments=segments,
        all_segments=all_segments,
        tags=tags,
        by_segment=by_segment,
        all_groups=[group for groups in by_segment.values() for group in groups],
        local=local,
        local_names=local_names,
        rows=get_vrf_contracts(all_segments, tags, local=local_names),
    )


# ---------------------------------------------------------------------------
# Device view (NX-OS VTEP configs)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Device view (NX-OS VTEP configs)
# ---------------------------------------------------------------------------

GPO_RENDERERS: dict[str, frozenset[str]] = {"vxlan_gpo": frozenset({"cisco_nxos"})}
"""Group-policy kind -> the netmiko platforms whose device templates render it (like
BaseDeviceTransform._BORDER_LEAF_ACL_PLATFORMS). A reference site of a kind with no
renderer here (EOS segment_security) renders nothing on its devices."""

_UNVERIFIED = "documented-not-lab-verified"
_RESERVED_LABELS = {UNCLASSIFIED: "UNCLASSIFIED", UNAWARE: "UNAWARE"}
_RESERVED_NAMES = frozenset({UNCLASSIFIED, UNAWARE, FALLBACK})

# Class-map match line per protocol of an entry without ports, from ONE table, with why it
# is not verified. A tcp/udp entry with a port is D (the guide's `match ipv4|ipv6 tcp dport N
# [to M]`, built in _class_map); the rest is the guide's silence (spec 4.1/4.3/4.12): the
# sample `show run` prints `match ip tcp` / `match ip udp` without saying how to configure
# them, and icmp and a permit-all class are not documented at all. None of these forms has
# an ipv6 variant, so IPv6 for such an entry is denied by the VRF's default deny.
_UNVERIFIED_CLASS_LINES: dict[str, tuple[str, str]] = {
    "tcp": ("match ip tcp", "only in the guide's sample show running-config"),
    "udp": ("match ip udp", "only in the guide's sample show running-config"),
    "icmp": ("match ip icmp", "not in the guide"),
    "any": ("match ip", "not in the guide"),
}
_FAMILIES = ("ipv4", "ipv6")


def _entry_key(entry: dict[str, Any]) -> tuple[str, int, int]:
    return entry["protocol"], entry["port_start"] or 0, entry["port_end"] or 0


def _refuse(who: str, why: str) -> ValueError:
    return ValueError(f"{who}: {why}; refusing unprotected config")


def _port_span(start: int, end: int | None, separator: str) -> str:
    """``443`` for a single port, ``8000<separator>8010`` for a range."""
    return f"{start}" if end in (None, start) else f"{start}{separator}{end}"


def _entry_text(entry: dict[str, Any]) -> str:
    """``tcp/443``, ``udp/5000-5010``, ``tcp`` (port-less), ``any``."""
    protocol, start = entry["protocol"], entry["port_start"]
    return protocol if start is None else f"{protocol}/{_port_span(start, entry['port_end'], '-')}"


def class_map_name(entries: Iterable[dict[str, Any]]) -> str:
    """``GPO-`` and the first 10 hex of the sha256 of the sorted (protocol, start, end) set (never hash())."""
    canonical = json.dumps(
        [[protocol, start or None, end or None] for protocol, start, end in sorted(map(_entry_key, entries))],
        separators=(",", ":"),
    )
    return "GPO-" + hashlib.sha256(canonical.encode()).hexdigest()[:10]


def _class_map(who: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """One class-map: name, entry list and `match` lines (tcp/udp with ports for ipv4 and ipv6)."""
    lines: list[str] = []
    notes: list[str] = []
    for entry in sorted(entries, key=_entry_key):
        protocol, start, end = entry["protocol"], entry["port_start"], entry["port_end"]
        if protocol in ("tcp", "udp") and start is not None:
            if end is not None and end < start:
                raise _refuse(who, f"{protocol} port range {start}-{end} ends before it starts")
            lines.extend(f"match {family} {protocol} dport {_port_span(start, end, ' to ')}" for family in _FAMILIES)
        else:
            line, why = _UNVERIFIED_CLASS_LINES[protocol]
            lines.append(line)
            notes.append(
                f"`{line}` for {_entry_text(entry)} is {why}; it has no ipv6 form here, "
                "so IPv6 for this entry is denied by the default deny (fail closed)"
            )
    return {
        "name": class_map_name(entries),
        "entries": ", ".join(_entry_text(entry) for entry in sorted(entries, key=_entry_key)),
        "lines": lines,
        "notes": sorted(set(notes)),
    }


def _cli_name(name: str) -> str:
    """A SecurityTag name as a CLI-safe group name: ``[A-Za-z0-9_-]`` (the guide's examples; its rules are U)."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)


def _label(name: str) -> str:
    return _RESERVED_LABELS.get(name) or _cli_name(name)


def _gpo_data_center(
    roots: dict[str, Any], device_name: str, platform: str
) -> tuple[str, str, dict[str, list[dict[str, Any]]]] | None:
    """(data center id, group-policy kind, the fleet's VTEPs by data center) of a device that sits in a reference fabric
    whose kind this platform renders, else None.

    The cheap gate, before the contracts are built: a fleet of old fixtures has no profile rows, and most
    data centers are no reference fabric. The device must be a VTEP of exactly one data center.
    """
    profiles, _problems = profiles_by_platform(roots.get("DcimGroupPolicyProfile") or [])
    if not profiles:
        return None
    fleet = vteps_by_data_center(roots.get("TopologyDataCenter") or [], roots.get("TopologyPod") or [])
    found = [dc_id for dc_id, vteps in fleet.items() if any(vtep.get("name") == device_name for vtep in vteps)]
    if len(found) != 1:
        return None
    vteps = fleet[found[0]]
    kind = site_kind(vteps, profiles)
    if site_reason(vteps, profiles) is not None or kind is None or platform not in GPO_RENDERERS.get(kind, ()):
        return None
    return found[0], kind, fleet


def _refuse_ambiguous_groups(who: str, groups: Iterable[Group]) -> None:
    """A tag shared by two segments or named like a pseudo-group would collapse into one entry of the views."""
    first: dict[str, Group] = {}
    for group in groups:
        if group.name.lower() in _RESERVED_NAMES:
            raise _refuse(who, f"group {group.name!r} is named like the reserved pseudo-group {group.name.upper()}")
        other = first.setdefault(group.name, group)
        if (other.segment, other.vrf) != (group.segment, group.vrf):
            raise _refuse(
                who,
                f"group {group.name!r} is the tag of segment {other.segment} (VRF {other.vrf}) "
                f"and of segment {group.segment} (VRF {group.vrf})",
            )


def _validate_group_ids(
    who: str, rows: list[dict[str, Any]], defined: set[str], by_name: dict[str, Group]
) -> dict[str, int]:
    """Group name -> its ID for every group the rows name or the VRFs define; refuses what cannot be numbered.

    A group without an ID (tag or profile reserved group not allocated), two groups on one ID and two groups
    with one CLI name are refused. A row end carries the ID the view gave it (a reserved group's, a stretched
    group's unaware half).
    """
    named_ids: dict[str, int | None] = {name: by_name[name].group_id for name in defined}
    for row in rows:
        named_ids[row["src"]], named_ids[row["dst"]] = row["src_group_id"], row["dst_group_id"]
    group_ids: dict[str, int] = {}
    for name, group_id in sorted(named_ids.items()):
        if group_id is None:
            raise _refuse(who, f"group {name} has no group id (tag or profile reserved group not allocated)")
        group_ids[name] = group_id
    by_id: dict[int, set[str]] = {}
    for name, group_id in group_ids.items():
        by_id.setdefault(group_id, set()).add(name)
    for group_id, names in sorted(by_id.items()):
        if len(names) > 1:
            raise _refuse(who, f"groups {sorted(names)} share group id {group_id}")
    safe: dict[str, str] = {}
    for name in sorted(defined):
        if safe.setdefault(_cli_name(name), name) != name:
            raise _refuse(
                who, f"groups {safe[_cli_name(name)]!r} and {name!r} share the group name {_cli_name(name)!r}"
            )
    return group_ids


def _selectors(
    contracts: DataCenterContracts, activations: Iterable[dict[str, Any]]
) -> tuple[dict[str, int], dict[str, set[str]]]:
    """(group name -> this device's VLAN of its segment, VRF -> segments with no such classifier).

    A routed tenant segment activated on the device is classified by ``match vlan`` only when it has a
    group and a VLAN here and is not inline-terminated (pure L2 on a fabric switch).
    """
    segments = {str(segment.get("id")): segment for segment in contracts.all_segments}
    selectors: dict[str, int] = {}
    unclassified: dict[str, set[str]] = {}
    for activation in activations:
        segment = activation.get("segment") or {}
        groups = contracts.by_segment.get(str(segment.get("id")))
        if groups and activation.get("vlan_id") and not is_inline_terminated(segment):
            selectors[groups[0].name] = int(activation["vlan_id"])
        elif (found := segments.get(str(segment.get("id")))) is not None and is_routed_tenant_segment(found):
            unclassified.setdefault(segment_vrf(found) or "", set()).add(str(found.get("name")))
    return selectors, unclassified


def _vrf_view(
    who: str,
    vrf: str,
    vrf_rows: list[dict[str, Any]],
    *,
    by_name: dict[str, Group],
    group_ids: dict[str, int],
    unclassified_id: int | None,
    no_classifier: set[str],
) -> dict[str, Any]:
    """The contracts and the enforcement of one VRF (``vxlan_gpo_device_view``)."""
    vrf_rows = sorted(vrf_rows, key=lambda row: (row["src_group_id"], row["dst_group_id"]))
    forward = {(row["src"], row["dst"]) for row in vrf_rows}
    contract_views = []
    for row in vrf_rows:
        notes = []
        for end, label in ((row["src"], "source"), (row["dst"], "destination")):
            if end == UNCLASSIFIED:
                notes.append(
                    f"{_UNVERIFIED}: reserved group {group_ids[end]} (UNCLASSIFIED) as the {label} of a contract; "
                    "the guide does not define it"
                )
        if row["src"] != row["dst"] and (row["dst"], row["src"]) in forward:
            notes.append(f"{_UNVERIFIED}: two bidir contracts on one pair")
        contract_views.append(
            {
                "src": row["src_group_id"],
                "dst": row["dst_group_id"],
                "src_name": _label(row["src"]),
                "dst_name": _label(row["dst"]),
                "policy": class_map_name(row["entries"]),
                "notes": notes,
            }
        )
    components = sorted(
        {
            _label(end)
            for row in vrf_rows
            for end in (row["src"], row["dst"])
            if end in by_name and by_name[end].kind == "component"
        }
    )
    reasons = [f"component groups {', '.join(components)} have no classifier until D2"] if components else []
    reasons += [f"segment {name} has no group classifier on this device" for name in sorted(no_classifier)]
    if not reasons and unclassified_id is None:
        raise _refuse(who, f"VRF {vrf} is enforced but the profile names no unclassified group id")
    return {
        "contracts": contract_views,
        "unenforced": "; ".join(reasons) or None,
        "enforce_tag": None if reasons else unclassified_id,
    }


def vxlan_gpo_device_view(
    roots: dict[str, Any],
    *,
    device_name: str,
    role: str,
    platform: str,
    activations: Iterable[dict[str, Any]],
    vrfs: Iterable[str],
) -> dict[str, Any] | None:
    """The group policy one VTEP renders, or None when it renders none (module docstring).

    ``roots`` are the cleaned fleet roots of the device query, ``activations``
    the device's segment activations (``vlan_id`` and ``segment.id``: the VLAN
    ID of a group's segment here, which is per VLAN domain) and ``vrfs`` the
    VRFs it carries (``vxlan.l3_vni_mappings``): a segment's VRF is its
    gateway namespace, the ``vrf_name`` of its L3 VNI mapping.

    None unless the role is a data-center VTEP, the device sits in exactly one
    data center, that data center is a reference fabric (utils/group_policy)
    and its kind has a renderer for the platform (``GPO_RENDERERS``).
    A VRF is enforced (``security enforce``, deny by default) only when every
    group its contracts name and every routed tenant segment it holds on this
    device is classified: a component group has no classifier until the
    component selectors exist (D2), and a segment activated here without a
    group or a VLAN has none either. Such a VRF gets its groups and contracts
    but is left unenforced, with the reason.
    """
    found = _gpo_data_center(roots, device_name, platform) if role in DC_VTEP_ROLES else None
    if found is None:
        return None
    dc_id, kind, fleet = found
    who = f"{platform} {role} {device_name}"
    contracts = data_center_contracts(roots, dc_id, fleet=fleet)
    _refuse_ambiguous_groups(who, contracts.all_groups)
    reserved = reserved_groups(contracts.profiles, kind)
    view = vxlan_gpo_view(
        contracts.all_groups,
        contracts.rows,
        reserved=reserved,
        reference_sites=contracts.reference_sites,
        local=contracts.local_names,
    )
    on_device = set(vrfs)
    rows_by_vrf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in view["contracts"]:
        if row["vrf"] in on_device:
            rows_by_vrf[row["vrf"]].append(row)
    if not rows_by_vrf:
        return None
    rows = [row for vrf_rows in rows_by_vrf.values() for row in vrf_rows]
    by_name = {group.name: group for group in contracts.all_groups}

    # Every group the VRFs hold on this data center must be defined: an enforced
    # VRF denies what no selector classified, so a rule-less segment would lose
    # its own intra-group traffic.
    defined = {row[end] for row in rows for end in ("src", "dst") if row[end] in by_name}
    defined |= {group.name for group in contracts.local if group.vrf in rows_by_vrf}
    group_ids = _validate_group_ids(who, rows, defined, by_name)

    class_maps: dict[str, dict[str, Any]] = {}
    for row in rows:
        class_map = _class_map(who, row["entries"])
        class_maps.setdefault(class_map["name"], class_map)

    selectors, unclassified = _selectors(contracts, activations)
    group_defs = []
    for name in sorted(defined, key=lambda item: group_ids[item]):
        group = by_name[name]
        notes = []
        if group.kind == "component":
            notes.append(f"{_label(name)}: component group of segment {group.segment}, no classifier until D2")
        elif group.intra_group == "deny":
            notes.append(
                f"{_UNVERIFIED}: {_label(name)}: intra-group deny of the microsegmented segment {group.segment} is "
                "not rendered (10.6(3)F intra-group isolation); its hosts talk freely until D2"
            )
        group_defs.append({"id": group_ids[name], "name": _label(name), "vlan_id": selectors.get(name), "notes": notes})

    return {
        "class_maps": [class_maps[name] for name in sorted(class_maps)],
        "groups": group_defs,
        "vrfs": {
            vrf: _vrf_view(
                who,
                vrf,
                vrf_rows,
                by_name=by_name,
                group_ids=group_ids,
                unclassified_id=reserved["unclassified"],
                no_classifier=unclassified.get(vrf, set()),
            )
            for vrf, vrf_rows in sorted(rows_by_vrf.items())
        },
    }
