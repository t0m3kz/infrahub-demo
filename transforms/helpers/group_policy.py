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
  too, so it needs no unaware group, but every segment a ``from`` line names
  must be defined, so the view lists the local segments and every segment a
  listed row names. It only enforces traffic to a segment, so a row to
  UNCLASSIFIED is no policy; it is kept per source segment as ``to_unclassified``
  because MSS-G is stateless and the reply of such a flow is traffic from
  UNCLASSIFIED to that segment. A prefix cannot tell a component from its
  segment, so a row that names a component group on either side is
  ``unsupported`` (never widened to the whole segment: that would be fail-open). The reply of a
  permit-all row is ``unsupported`` too: a stateless device matches a reply by source port and
  ``any`` has none, so that direction stays closed (never widened to a forward-all policy); and the
  reply of a flow whose every rule is unsupported is dropped.

Rows are forward only: replies are the renderer's (``REPLIES``). Sorted,
de-duplicated and id-free.

The device renderers read the same contracts through one dispatcher, ``group_policy_device_view``
(the gate, the data center's contracts once, then the builder of the data center's kind, ``GPO_RENDERERS``):
``_vxlan_gpo_build`` for NX-OS and ``_segment_security_build`` for EOS MSS-G (their
docstrings say how). ``_vxlan_gpo_build`` is the NX-OS VTEP configs' reader: the ``vxlan_gpo_view`` of the device's
own data center, cut to the VRFs the device carries (a VTEP with none of them still
enables GPO, with no groups or contracts). Every VTEP of the data
center renders the same class-maps, policy-maps and contracts for a VRF; only the
selectors differ, since they name things of one device: ``match vlan`` for a segment
group (a VLAN ID is per VLAN domain) and ``match vlan <vlan> interface <port>`` for a
component group, rendered on the VTEP that owns the host-facing port only.
It renders nothing (None) unless the data center is a reference fabric of a
kind with a renderer, and refuses a row it cannot number, a tag shared by two
segments and a group named like a pseudo-group (``... refusing unprotected
config``).

A component group is classified by its instances' ports (``component_coverage``): the
host an instance is cabled through (utils/app_instances.physical_host, the one the
generator tags ports for) and the VTEP port at the far end of each uplink or bond
member, that carries the component's segment. No IP is used (an instance has none, an
address is spoofable and IPv4 only). A VRF is left unenforced, with the reasons as a
comment, while a component group it names cannot be placed (coverage blockers: an
instance with no host, not cabled to a VTEP, cabled to a switch that is no VTEP or to a
VTEP port that does not carry the segment, a cable without exactly one far end, a bond
member with no port-channel, a port shared by two components of one segment) or a tenant
segment it holds on the device has no classifier.

The coverage part of the verdict is the data center's, identical on every VTEP of a VRF;
the per-device part is not: a claimed port that THIS device lacks, has untagged or has no
VLAN for (``_selectors`` problems) leaves its VRF unenforced here while the other vPC peer
enforces it. An enforced VRF that relies on component classification lists the claimed
ports (data center wide) in a documented-not-lab-verified comment on its enforce line.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import ip_network
from itertools import combinations, product
from typing import Any

from netutils.interface import sort_interface_list

from transforms.helpers.policy import segment_flows
from transforms.helpers.segments import is_inline_terminated
from utils.app_instances import HOSTED_INSTANCE_TYPENAMES, physical_host
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
from utils.segment_addressing import classifier_prefix

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


def get_vrf_contracts(
    segments: Iterable[dict[str, Any]],
    tags: Iterable[dict[str, Any]],
    *,
    local: set[str] | None = None,
    groups: tuple[dict[str, list[Group]], dict[str, Group]] | None = None,
) -> list[dict[str, Any]]:
    """The permit rows of every VRF (module docstring), sorted by (vrf, src, dst).

    ``segments`` are every segment of the fleet whose rules may name a
    classified segment (cleaned SegmentGroupPolicyFields: every rule lives in
    its source segment's policy, so the rules of all segments are all rules);
    ``tags`` every SecurityTag with its components. ``local`` (group names)
    keeps the rows with at least one local end: a data center or a device
    scope; None keeps all, and the local rows of any partition of the groups
    add up to all of them. ``groups``: the ``segment_groups(segments, tags)`` the caller already has.
    """
    segments = list(segments)
    by_segment, by_tag = groups if groups is not None else segment_groups(segments, tags)
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
        entries.update(_entry_set(row["entries"]))
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


_COMPONENT_REASON = "a component group is not expressible by prefix (the row is not widened to its segment)"
_ANY_REPLY_REASON = (
    "the reply of a permit-all flow cannot be mirrored by source port on a stateless device "
    "(the reverse direction is left closed)"
)


def segment_security_view(
    groups: Iterable[Group],
    rows: Iterable[dict[str, Any]],
    prefixes: dict[str, str | None],
    *,
    local: set[str] | None = None,
) -> dict[str, Any]:
    """The contract set as MSS-G segments by prefix, policies under the destination (module docstring).

    ``groups`` are every group the rows name, ``local`` the segment groups to
    list (None: all) besides every segment a row names (a ``from`` line needs its
    source defined); ``prefixes``: segment name -> its gateway prefix (the
    segment's definition). A row that names a component group is unsupported, and so is the reply of
    a permit-all row (MSS-G is stateless: its reply is not a port the device can mirror).
    """
    by_name = {group.name: group for group in groups}
    segment_group = {group.segment: group.name for group in by_name.values() if group.kind == "segment"}
    policies: dict[tuple[str, str], tuple[set[Entry], set[str]]] = {}
    outbound: dict[str, tuple[set[Entry], set[str]]] = {}
    outbound_rows: list[tuple[str, dict[str, Any]]] = []
    unsupported: list[dict[str, Any]] = []
    into_component: set[str] = set()

    def _component(row: dict[str, Any], reason: str) -> None:
        unsupported.append({"src": row["src"], "dst": row["dst"], "rules": row["rules"], "reason": reason})

    for row in rows:
        dst, src = by_name.get(row["dst"]), by_name.get(row["src"])
        if dst is None:
            # A row to UNCLASSIFIED: no MSS-G hook, but its reply is a fallback of the source segment.
            if src is not None and src.kind == "segment":
                outbound_rows.append((src.name, row))
            elif src is not None:
                _component(row, _COMPONENT_REASON)
            continue
        if "component" in (dst.kind, src.kind if src else None):
            if dst.kind == "component":
                into_component.update(row["rules"])
            same_segment = src is not None and src.segment == dst.segment
            _component(
                row,
                "component groups inside one segment are not expressible by prefix"
                if same_segment
                else _COMPONENT_REASON,
            )
            continue
        source = FALLBACK if src is None else segment_group[src.segment]
        entries, rules = policies.setdefault((segment_group[dst.segment], source), (set(), set()))
        entries.update(_entry_set(row["entries"]))
        rules.update(row["rules"])

    # The reply of a flow whose destination is left closed above (a component group) would only open the source
    # segment to its replies: it is not kept. A rule that also has a row into a segment keeps its reply.
    closed = into_component - {rule for _, rules in policies.values() for rule in rules}
    for segment, row in outbound_rows:
        if row["rules"] and set(row["rules"]) <= closed:
            continue
        entries, rules = outbound.setdefault(segment, (set(), set()))
        entries.update(_entry_set(row["entries"]))
        rules.update(row["rules"])

    # The reply of a permit-all flow is all traffic back, which a stateless device cannot match by source port.
    for (dst_name, source), (entries, rules) in sorted(policies.items()):
        if source != FALLBACK and any(entry[0] == "any" for entry in entries):
            unsupported.append({"src": source, "dst": dst_name, "rules": sorted(rules), "reason": _ANY_REPLY_REASON})
    for segment, (entries, rules) in sorted(outbound.items()):
        if any(entry[0] == "any" for entry in entries):
            unsupported.append(
                {"src": segment, "dst": UNCLASSIFIED, "rules": sorted(rules), "reason": _ANY_REPLY_REASON}
            )

    def _policies(segment: str) -> list[dict[str, Any]]:
        return [
            {"from": source, "entries": _entries(entries), "rules": sorted(rules)}
            for (dst, source), (entries, rules) in sorted(policies.items())
            if dst == segment and source != FALLBACK
        ]

    def _leg(found: tuple[set[Entry], set[str]] | None) -> dict[str, Any] | None:
        return {"entries": _entries(found[0]), "rules": sorted(found[1])} if found else None

    named = {name for pair in policies for name in pair} | set(outbound)
    listed = sorted(
        (
            group
            for group in by_name.values()
            if group.kind == "segment" and (local is None or group.name in local or group.name in named)
        ),
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
                "fallback": _leg(policies.get((group.name, FALLBACK))),
                "to_unclassified": _leg(outbound.get(group.name)),
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


# ---------------------------------------------------------------------------
# Device views (VTEP configs): one dispatcher, one builder per group-policy kind
# ---------------------------------------------------------------------------

GPO_RENDERERS: dict[str, frozenset[str]] = {
    "vxlan_gpo": frozenset({"cisco_nxos"}),
    "segment_security": frozenset({"arista_eos"}),
}
"""Group-policy kind -> the netmiko platforms whose device templates render it (like
BaseDeviceTransform._BORDER_LEAF_ACL_PLATFORMS). A reference site of a kind with no
renderer for the device's platform renders nothing on its devices."""

GPO_CONFIG_KEYS: dict[str, str] = {"vxlan_gpo": "group_policy", "segment_security": "segment_security"}
"""Group-policy kind -> the template context key of its device view (the NX-OS key predates the second kind)."""

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


def _digest(payload: Any) -> str:
    """The first 10 hex of the sha256 of the canonical JSON of ``payload`` (never hash()): the content name of an object."""
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:10]


def class_map_name(entries: Iterable[dict[str, Any]]) -> str:
    """``GPO-`` and the digest of the sorted (protocol, start, end) set."""
    return "GPO-" + _digest(
        [[protocol, start or None, end or None] for protocol, start, end in sorted(map(_entry_key, entries))]
    )


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


def _refuse_cli_clash(who: str, names: Iterable[str], noun: str, called: str) -> None:
    """Two names that become one CLI name (``_cli_name``) would collapse into one object on the device."""
    safe: dict[str, str] = {}
    for name in sorted(names):
        if safe.setdefault(_cli_name(name), name) != name:
            raise _refuse(who, f"{noun} {safe[_cli_name(name)]!r} and {name!r} share the {called} {_cli_name(name)!r}")


def _gpo_data_center(
    roots: dict[str, Any], device_name: str, platform: str
) -> tuple[str, str, dict[str, list[dict[str, Any]]], Profiles] | None:
    """(data center id, group-policy kind, the fleet's VTEPs by data center, the profiles) of a device that sits in a
    reference fabric whose kind this platform renders, else None.

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
    return found[0], kind, fleet, profiles


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
    _refuse_cli_clash(who, defined, "groups", "group name")
    return group_ids


def _one_line(text: str) -> str:
    """A name-bearing text as one comment line: whitespace (a newline in a name) collapses to single spaces."""
    return " ".join(text.split())


@dataclass(frozen=True)
class ComponentClaim:
    """One host-facing port a component group classifies: ``match vlan <segment's VLAN> interface <port>`` on ``vtep``."""

    group: str
    segment_id: str
    vtep: str
    port: str


@dataclass(frozen=True)
class ComponentCoverage:
    """Where the component groups of a data center's segments are classified, and what stops them from being.

    ``claims``: the (VTEP, port) pairs, sorted. ``blockers``: component group name -> why it cannot be
    classified completely (sorted texts); the VRFs naming such a group are left unenforced.
    """

    claims: list[ComponentClaim]
    blockers: dict[str, list[str]]


def _capability_ids(interface: dict[str, Any]) -> set[str]:
    """The ids of what an interface is tagged with (segments, among others)."""
    return {str(item["id"]) for item in interface.get("interface_capabilities") or [] if item.get("id")}


def _place_instance(
    instance: dict[str, Any], segment_id: str, *, vtep_names: set[str], other_vtep_names: set[str]
) -> tuple[set[tuple[str, str]], list[str]]:
    """((VTEP, port) pairs carrying the segment, problems) of one instance of a component of that segment.

    The host (utils/app_instances.physical_host) has its uplink/bond-member NICs; the far end of each NIC's
    cable (the near end skipped by id) is a switch port. A far end that is a VTEP of this data center is
    claimed when it, or its port-channel for a LAG member (never the member port: a LAG member without a
    port-channel is a problem), carries the segment; when it does not, that is a problem too (the component's
    traffic enters the VTEP unclassified). A cable that does not have exactly one far end is a problem. A far end that is a VTEP of another data center is that
    data center's to classify and is ignored; a far end that is a VTEP of none (a leaf below a VTEP, a
    firewall) cannot classify, so it is a problem. An instance reaching no VTEP at all is a problem too, in
    every data center of its segment: where it sits is not known.
    """
    typename = instance.get("typename")
    label = str(instance.get("name") or instance.get("id") or "?")
    host = physical_host(instance)
    if host is None:
        if typename in HOSTED_INSTANCE_TYPENAMES:
            return set(), [f"instance {label} has no hosting device"]
        return set(), [f"instance {label} ({typename or 'unknown kind'}) is not an on-prem device"]
    host_label = str(host.get("name") or host.get("id") or "?")
    ports: set[tuple[str, str]] = set()
    problems: list[str] = []
    placed = False
    for nic in host.get("interfaces") or []:
        nic_label = f"{host_label} {nic.get('name') or '?'}"
        endpoints = (nic.get("cable") or {}).get("endpoints") or []
        if not endpoints:
            continue
        # The near end is the NIC itself (by id); an endpoint with no id did not match the query's
        # inline fragment and is no end of anything. A cable that does not leave exactly one other end
        # (a bundle, a patch panel, a half-loaded cable) cannot say where the NIC lands.
        far_ends = [end for end in endpoints if end.get("id") and end["id"] != nic.get("id")]
        if len(far_ends) != 1:
            problems.append(f"{nic_label}: cable has {len(far_ends)} far ends")
            continue
        far_end = far_ends[0]
        switch = (far_end.get("device") or {}).get("name")
        if not switch or not far_end.get("name"):
            problems.append(f"{nic_label} is cabled to something that is not a switch port")
        elif switch in vtep_names:
            placed = True
            if far_end.get("role") == "lag":
                port_channel = far_end.get("lag") or {}
                if not port_channel.get("name"):
                    problems.append(
                        f"{nic_label} reaches {switch} {far_end['name']}, a LAG member with no port-channel"
                    )
                    continue
                port, carried = str(port_channel["name"]), _capability_ids(port_channel)
            else:
                port, carried = str(far_end["name"]), _capability_ids(far_end)
            if segment_id in carried:
                ports.add((str(switch), port))
            else:
                # Cabled to a VTEP of this data center that does not carry the segment on that port: the
                # component's traffic arrives there unclassified, so the group cannot be enforced.
                problems.append(f"{nic_label} reaches {switch} {port}, which is not tagged with the segment")
        elif switch in other_vtep_names:
            placed = True
        else:
            problems.append(f"{nic_label} is cabled to {switch}, which is a VTEP of no data center")
    if not placed and not problems:
        problems.append(f"instance {label} (host {host_label}) is not cabled to a VTEP")
    return ports, problems


def component_coverage(
    contracts: DataCenterContracts, *, vtep_names: set[str], other_vtep_names: set[str]
) -> ComponentCoverage:
    """The ports each component group of this data center's segments is classified on, and the blockers.

    Per component group (a tagged AppComponent of a microsegmented segment active in this data center):
    the (VTEP, port) pairs its instances' hosts are cabled to (``_place_instance``), keyed by
    (VTEP, port, segment): the interface selector cannot tell two components of one segment apart, so a
    port two of them land on is claimed by neither and blocks both. The same port in two segments is fine
    (a selector names the segment's VLAN). A component with no instance in this data center, and the
    component groups of a remote segment (the far end of a local row), have no blocker: nothing to
    classify here. An instance whose host resolves nowhere blocks every data center of its segment, since
    where it sits is not known (conservative).

    ``vtep_names`` are this data center's VTEPs, ``other_vtep_names`` those of the rest of the fleet.
    Computed from the fleet roots alone, so every VTEP of the data center gets the same coverage.
    """
    components_of = contracts.components_of
    owners: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    problems: dict[str, set[str]] = defaultdict(set)
    for segment in contracts.segments:
        segment_id = str(segment.get("id"))
        component_groups = {
            group.name for group in contracts.by_segment.get(segment_id, []) if group.kind == "component"
        }
        for tag in components_of.get(segment_id, []):
            group = str(tag.get("name"))
            if group not in component_groups:
                continue
            for component in tag.get("components") or []:
                if (component.get("network_segment") or {}).get("id") != segment_id:
                    continue
                for instance in component.get("instances") or []:
                    ports, found = _place_instance(
                        instance, segment_id, vtep_names=vtep_names, other_vtep_names=other_vtep_names
                    )
                    problems[group].update(found)
                    for vtep, port in ports:
                        owners[(vtep, port, segment_id)].add(group)
    claims: list[ComponentClaim] = []
    for (vtep, port, segment_id), groups in sorted(owners.items()):
        if len(groups) > 1:
            names = ", ".join(_label(group) for group in sorted(groups))
            for group in groups:
                problems[group].add(
                    f"port {port} of {vtep} is shared by components {names} of one segment "
                    "(an interface selector cannot tell them apart)"
                )
        else:
            claims.append(ComponentClaim(next(iter(groups)), segment_id, vtep, port))
    return ComponentCoverage(
        claims=sorted(claims, key=lambda claim: (claim.group, claim.vtep, claim.port)),
        blockers={group: sorted(found) for group, found in sorted(problems.items()) if found},
    )


@dataclass
class _Selectors:
    """What one device classifies: ``vlans`` (segment group -> its VLAN here), ``ports`` (component group -> its
    ``{vlan_id, name}`` host ports here), ``unclassified`` (VRF -> its segments with no classifier here) and
    ``problems`` (component group -> why a claimed port of this device cannot be rendered)."""

    vlans: dict[str, int]
    ports: dict[str, list[dict[str, Any]]]
    unclassified: dict[str, set[str]]
    problems: dict[str, list[str]]


def _selectors(
    contracts: DataCenterContracts,
    activations: Iterable[dict[str, Any]],
    *,
    claims: Iterable[ComponentClaim],
    device_name: str,
    interfaces: Iterable[dict[str, Any]],
) -> _Selectors:
    """The selectors of one device (``_Selectors``).

    A routed tenant segment activated on the device is classified by ``match vlan`` only when it has a
    group and a VLAN here and is not inline-terminated (pure L2 on a fabric switch). A component group is
    classified by ``match vlan <its segment's VLAN here> interface <port>`` on each port the coverage
    claims on THIS device, provided the device has that interface, tagged with the segment, and the
    segment a VLAN here; otherwise the claim is a problem of this device (inconsistent or not yet
    converged data) and the VRF stays unenforced.
    """
    segments = {str(segment.get("id")): segment for segment in contracts.all_segments}
    vlans: dict[str, int] = {}
    vlan_of_segment: dict[str, int] = {}
    unclassified: dict[str, set[str]] = {}
    for activation in activations:
        segment = activation.get("segment") or {}
        groups = contracts.by_segment.get(str(segment.get("id")))
        if groups and activation.get("vlan_id") and not is_inline_terminated(segment):
            vlans[groups[0].name] = int(activation["vlan_id"])
            vlan_of_segment[str(segment.get("id"))] = int(activation["vlan_id"])
        elif (found := segments.get(str(segment.get("id")))) is not None and is_routed_tenant_segment(found):
            unclassified.setdefault(segment_vrf(found) or "", set()).add(str(found.get("name")))
    own = {str(item["name"]): item for item in interfaces if item.get("name")}
    claimed: dict[str, set[str]] = defaultdict(set)
    problems: dict[str, list[str]] = defaultdict(list)
    vlan_of_group: dict[str, int] = {}
    for claim in claims:
        if claim.vtep != device_name:
            continue
        interface, vlan = own.get(claim.port), vlan_of_segment.get(claim.segment_id)
        if interface is None or claim.segment_id not in _capability_ids(interface):
            problems[claim.group].append(
                f"port {claim.port} is not an interface of {device_name} tagged with the segment"
            )
        elif vlan is None:
            problems[claim.group].append(
                f"the segment has no VLAN on {device_name}, so port {claim.port} cannot be matched"
            )
        else:
            claimed[claim.group].add(claim.port)
            vlan_of_group[claim.group] = vlan
    ports = {
        group: [{"vlan_id": vlan_of_group[group], "name": name} for name in sort_interface_list(list(names))]
        for group, names in claimed.items()
    }
    return _Selectors(vlans=vlans, ports=ports, unclassified=unclassified, problems=dict(problems))


_PORT_LIST_LIMIT = 8


def _claimed_ports_text(claims: Iterable[ComponentClaim], groups: Iterable[str]) -> str | None:
    """``<device> <port>, ...`` of the ports the given component groups are classified on, data-center wide
    (the same on every VTEP), sorted by device and naturally by port, at most ``_PORT_LIST_LIMIT`` listed."""
    wanted = set(groups)
    by_device: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        if claim.group in wanted:
            by_device[claim.vtep].add(claim.port)
    pairs = [
        f"{device} {port}" for device in sorted(by_device) for port in sort_interface_list(list(by_device[device]))
    ]
    if not pairs:
        return None
    more = len(pairs) - _PORT_LIST_LIMIT
    return ", ".join(pairs[:_PORT_LIST_LIMIT]) + (f" and {more} more" if more > 0 else "")


def _vrf_view(
    who: str,
    vrf: str,
    vrf_rows: list[dict[str, Any]],
    *,
    by_name: dict[str, Group],
    group_ids: dict[str, int],
    unclassified_id: int | None,
    no_classifier: set[str],
    component_blockers: dict[str, list[str]],
    claims: Iterable[ComponentClaim],
    staged: str | None,
) -> dict[str, Any]:
    """The contracts and the enforcement of one VRF (``_vxlan_gpo_build``).

    Enforced when the data center enforces (``staged`` is the reason it does not, listed first), no
    component group the VRF's contracts name has a blocker (``component_coverage``, plus the problems of
    this device's claimed ports) and no tenant segment it holds here lacks a classifier.

    The coverage part of that verdict is the data center's, identical on every VTEP of the VRF. The
    per-device part is not: ``component_blockers`` also holds the problems of THIS device's claimed ports
    (missing, untagged, no VLAN here), so one vPC peer can be left unenforced while the other enforces.
    ``classified_ports`` (data center wide, identical on every VTEP) lists the ports the VRF's enforcement
    relies on: component classification by interface + VLAN is documented-not-lab-verified.
    """
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
            end
            for row in vrf_rows
            for end in (row["src"], row["dst"])
            if end in by_name and by_name[end].kind == "component"
        }
    )
    reasons = [staged] if staged else []
    reasons += [
        f"component group {_label(name)}: {text}" for name in components for text in component_blockers.get(name, [])
    ]
    reasons += [f"segment {name} has no group classifier on this device" for name in sorted(no_classifier)]
    # A staged data center always has its reason: the refusal is for a VRF that would really enforce.
    if not reasons and unclassified_id is None:
        raise _refuse(who, f"VRF {vrf} is enforced but the profile names no unclassified group id")
    return {
        "contracts": contract_views,
        "unenforced": "; ".join(_one_line(reason) for reason in reasons) or None,
        "enforce_tag": None if reasons else unclassified_id,
        "classified_ports": _one_line(ports) if (ports := _claimed_ports_text(claims, components)) else None,
    }


def _staged_reason(contracts: DataCenterContracts, dc_id: str, consequence: str) -> str | None:
    """Why a data center configures but does not enforce (``group_policy_enforce`` false), None when it enforces."""
    if contracts.enforce:
        return None
    return f"data center {contracts.name or dc_id} stages group policy (group_policy_enforce is false): {consequence}"


@dataclass(frozen=True)
class _DeviceScope:
    """What a device builder reads: the device, its data center's contracts and what the device carries."""

    who: str
    kind: str
    dc_id: str
    fleet: dict[str, list[dict[str, Any]]]
    contracts: DataCenterContracts
    device_name: str
    activations: list[dict[str, Any]]
    vrfs: frozenset[str]
    interfaces: list[dict[str, Any]]


def _vxlan_gpo_build(scope: _DeviceScope) -> dict[str, Any]:
    """The NX-OS group policy one VTEP renders (module docstring).

    A VTEP of a reference fabric always gets it: with no contract row for the VRFs it carries the view is
    enablement only (empty ``class_maps``, ``groups`` and ``vrfs``).

    The scope's ``contracts`` are the cleaned fleet roots' contracts of the device's data center,
    ``activations`` the device's segment activations (``vlan_id`` and ``segment.id``: the VLAN
    ID of a group's segment here, which is per VLAN domain), ``vrfs`` the
    VRFs it carries (``vxlan.l3_vni_mappings``): a segment's VRF is its
    gateway namespace, the ``vrf_name`` of its L3 VNI mapping, and ``interfaces``
    its own cleaned interfaces (the host ports a component selector names must be
    among them, tagged with the segment).

    Each group dict carries ``vlan_id`` (its segment's VLAN here, segment groups)
    and ``interfaces`` (``{vlan_id, name}`` of the host ports of this device that
    classify it, component groups).
    A VRF is enforced (``security enforce``, deny by default) only when every
    component group its contracts name can be classified (no coverage blocker)
    and every routed tenant segment it holds on this device is classified: a
    segment activated here without a group or a VLAN has no classifier. Such a
    VRF gets its groups and contracts but is left unenforced, with the reasons. So does every VRF of a
    data center that does not enforce (``group_policy_enforce`` false, A6): that reason comes first.
    """
    who, dc_id, fleet, contracts = scope.who, scope.dc_id, scope.fleet, scope.contracts
    device_name, activations, interfaces = scope.device_name, scope.activations, scope.interfaces
    reserved = reserved_groups(contracts.profiles, scope.kind)
    view = vxlan_gpo_view(
        contracts.all_groups,
        contracts.rows,
        reserved=reserved,
        reference_sites=contracts.reference_sites,
        local=contracts.local_names,
    )
    on_device = scope.vrfs
    rows_by_vrf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in view["contracts"]:
        if row["vrf"] in on_device:
            rows_by_vrf[row["vrf"]].append(row)
    if not rows_by_vrf:
        # A VTEP with no contracts still enables GPO: a policy-unaware VTEP in a reference
        # site is the mixed state the profile excludes (spec 2.9). Nothing else is rendered,
        # so none of the group data is validated (the refusals below guard what is rendered).
        return {"class_maps": [], "groups": [], "vrfs": {}}
    _refuse_ambiguous_groups(who, contracts.all_groups)
    rows = [row for vrf_rows in rows_by_vrf.values() for row in vrf_rows]
    by_name = contracts.groups_by_name

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

    coverage = component_coverage(
        contracts,
        vtep_names={str(vtep.get("name")) for vtep in contracts.vteps},
        other_vtep_names={
            str(vtep.get("name")) for other_id, vteps in fleet.items() if other_id != dc_id for vtep in vteps
        },
    )
    selectors = _selectors(
        contracts, activations, claims=coverage.claims, device_name=device_name, interfaces=interfaces
    )
    component_blockers = {
        group: sorted({*coverage.blockers.get(group, []), *selectors.problems.get(group, [])})
        for group in {*coverage.blockers, *selectors.problems}
    }
    group_defs = []
    for name in sorted(defined, key=lambda item: group_ids[item]):
        group = by_name[name]
        notes = []
        if group.intra_group == "deny":
            notes.append(
                f"{_UNVERIFIED}: {_label(name)}: intra-group deny of the microsegmented segment {group.segment} is "
                "not rendered (10.6(3)F intra-group isolation); its hosts talk freely until it is"
            )
        group_defs.append(
            {
                "id": group_ids[name],
                "name": _label(name),
                "vlan_id": selectors.vlans.get(name),
                "interfaces": selectors.ports.get(name, []),
                "notes": notes,
            }
        )

    staged = _staged_reason(contracts, dc_id, "contracts are configured, security enforce is not")
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
                no_classifier=selectors.unclassified.get(vrf, set()),
                component_blockers=component_blockers,
                claims=coverage.claims,
                staged=staged,
            )
            for vrf, vrf_rows in sorted(rows_by_vrf.items())
        },
    }


# ---------------------------------------------------------------------------
# EOS MSS-G (segment_security)
# ---------------------------------------------------------------------------

MSSG_MAX_SEGMENTS = 60
"""Segments one device can define across all VRFs (spec 1.4, D: the manual's limit)."""

_MSSG_FORWARD_ALL = "policy-forward-all"
_MSSG_DROP_ALL = "policy-drop-all"
_MSSG_ANY_APPLICATION = "app-match-all"
_MSSG_PROTOCOLS = frozenset({"tcp", "udp", "icmp"})
"""The rule protocols an application renders: the schema's choices but ``any`` (a built-in policy)."""


def _has_any(entries: set[Entry]) -> bool:
    return any(entry[0] == "any" for entry in entries)


class _MssgCatalog:
    """The global objects the custom policies of one device name: field-sets, applications and policies.

    Every name is the hash of its content, so equal content is one object on every device of a data center and
    a re-render is byte-identical. A policy is the entries a source may send to the destination (``forward``,
    matched on the destination port) and the replies of the entries the destination may send to the source
    (``reply``, matched on the source port: MSS-G is stateless, spec 3.7), ended by a drop of everything else
    (documented-not-lab-verified, spec 3.9).
    """

    def __init__(self, who: str) -> None:
        self.who = who
        self.field_sets: dict[str, dict[str, Any]] = {}
        self.applications: dict[str, dict[str, Any]] = {}
        self.policies: dict[str, dict[str, Any]] = {}

    def policy(self, forward: set[Entry], reply: set[Entry]) -> str | None:
        """The policy name for the entry sets, None when they allow nothing.

        The built-in forward-all only when the FORWARD set (what the contract permits into the destination) has
        ``any``. A reply of ``any`` cannot be matched by source port: it is left closed, never widened into the
        forward direction.
        """
        if _has_any(forward):
            return _MSSG_FORWARD_ALL
        reply = set() if _has_any(reply) else reply
        if not forward and not reply:
            return None
        found: dict[str, dict[str, Any]] = {}
        for application in (*self._applications("destination", forward), *self._applications("source", reply)):
            found.setdefault(application["name"], application)
        applications = sorted(
            found.values(), key=lambda item: (item["protocol"], item["ports"] or [], item["direction"])
        )
        names = [item["name"] for item in applications]
        name = "MSSG-POL-" + _digest([names])
        if name not in self.policies:
            lines = [
                {"seq": 10 * (index + 1), "application": app_name, "action": "forward stateless"}
                for index, app_name in enumerate(names)
            ]
            lines.append(
                {"seq": 10 * (len(names) + 1), "application": _MSSG_ANY_APPLICATION, "action": "drop stateless"}
            )
            self.policies[name] = {
                "name": name,
                "text": "; ".join(item["text"] for item in applications),
                "lines": lines,
            }
        return name

    def _applications(self, direction: str, entries: set[Entry]) -> list[dict[str, Any]]:
        """One application per protocol of ``entries``: all its ports (a field-set) or, port-less, the protocol."""
        ports: dict[str, list[tuple[int, int]]] = defaultdict(list)
        whole: set[str] = set()
        for protocol, start, end in entries:
            if protocol not in _MSSG_PROTOCOLS:
                raise _refuse(self.who, f"protocol {protocol!r} has no segment security application")
            if start is None or protocol == "icmp":
                whole.add(protocol)
            elif end is not None and end < start:
                raise _refuse(self.who, f"{protocol} port range {start}-{end} ends before it starts")
            else:
                ports[protocol].append((start, end or start))
        found = []
        for protocol in sorted(whole):
            found.append(self._application(protocol, direction, None))
        for protocol in sorted(set(ports) - whole):
            found.append(self._application(protocol, direction, sorted(set(ports[protocol]))))
        return found

    def _application(self, protocol: str, direction: str, ports: list[tuple[int, int]] | None) -> dict[str, Any]:
        field_set = None
        if ports is not None:
            spans = [f"{start}" if end == start else f"{start}-{end}" for start, end in ports]
            field_set = "MSSG-FS-" + _digest([spans])
            self.field_sets.setdefault(field_set, {"name": field_set, "ports": ", ".join(spans)})
            name = "MSSG-APP-" + _digest([protocol, direction, spans])
            text = f"{protocol}/{','.join(spans)}"
        else:
            name = "MSSG-APP-" + _digest([protocol])
            text = protocol
        text = ("reply " if direction == "source" else "") + text
        self.applications.setdefault(
            name,
            {
                "name": name,
                "protocol": protocol,
                "port_side": direction if ports is not None else None,
                "field_set": field_set,
            },
        )
        return {
            "name": name,
            "protocol": protocol,
            "ports": [list(span) for span in ports] if ports is not None else None,
            "direction": direction,
            "text": text,
        }


def _fallback_entries(item: dict[str, Any]) -> tuple[set[Entry], set[Entry]]:
    """(forward, reply) of a destination's fallback: what UNCLASSIFIED may send in; the reply of what it may send out."""
    forward = _entry_set((item["fallback"] or {}).get("entries"))
    reply = _entry_set((item["to_unclassified"] or {}).get("entries"))
    return forward, set() if _has_any(reply) else reply


def _segment_security_build(scope: _DeviceScope) -> dict[str, Any] | None:
    """The EOS MSS-G view one VTEP renders, or None when it renders none.

    The view is the data center's ``segment_security_view`` cut to the VRFs the device carries
    (``scope.vrfs``, as for ``_vxlan_gpo_build``), with:

    - every segment named by a ``from`` line defined, remote ones included, and, in a VRF with a
      fallback, every segment of the VRF (a fallback also catches a source in a segment without a
      ``from`` line, so those get an explicit ``policy-drop-all``); at most ``MSSG_MAX_SEGMENTS``;
    - a VRF holding a segment with no gateway prefix not rendered at all (``skipped``): enforcement is
      box-wide, so a segment that cannot be classified would be dropped to, or from, wholesale;
    - the reply of every permitted flow as a mirrored policy on the source port (MSS-G is stateless),
      the built-in ``policy-forward-all`` for a permit-all entry, a content-hashed custom policy
      (AVD's model; documented-not-lab-verified) for the rest;
    - ``enforce`` False when the data center stages group policy (``shutdown`` instead of
      ``no shutdown``). Never rendered: ``no segment policy policy-drop-all default``, redirect,
      log, ``match interface`` (spec 3.5, 3.4, 2.7).

    None when the device has no VRF with a segment to define.
    """
    who, contracts = scope.who, scope.contracts
    prefixes = {str(segment.get("name")): classifier_prefix(segment) for segment in contracts.all_segments}
    view = segment_security_view(contracts.all_groups, contracts.rows, prefixes, local=contracts.local_names)
    listed = {item["name"]: item for item in view["segments"] if item["vrf"] in scope.vrfs}
    if not listed:
        return None
    _refuse_ambiguous_groups(who, contracts.all_groups)
    by_name = contracts.groups_by_name

    # A destination's fallback also catches a source in a segment this device does not define (spec 3.14),
    # so a VRF with a fallback defines every segment of the VRF, to drop the ones with no row.
    fallback_vrfs = {item["vrf"] for item in listed.values() if any(_fallback_entries(item))}
    for group in contracts.all_groups:
        if group.kind == "segment" and group.vrf in fallback_vrfs and group.name not in listed:
            listed[group.name] = {
                "name": group.name,
                "vrf": group.vrf,
                "prefix": prefixes.get(group.segment),
                "intra_segment": group.intra_group,
                "policies": [],
                "fallback": None,
                "to_unclassified": None,
            }

    by_vrf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in sorted(listed):
        by_vrf[listed[name]["vrf"]].append(listed[name])
    skipped = {
        vrf: sorted(item["name"] for item in items if not item["prefix"])
        for vrf, items in by_vrf.items()
        if any(not item["prefix"] for item in items)
    }
    rendered = {vrf: items for vrf, items in sorted(by_vrf.items()) if vrf not in skipped}
    count = sum(len(items) for items in rendered.values())
    if count > MSSG_MAX_SEGMENTS:
        raise _refuse(
            who,
            f"{count} segments to define exceed the {MSSG_MAX_SEGMENTS} an EOS device holds across all VRFs "
            "(a VRF with a fallback defines every segment of the VRF)",
        )

    _refuse_cli_clash(who, (item["name"] for items in rendered.values() for item in items), "segments", "name")
    nested: dict[str, list[str]] = defaultdict(list)
    for vrf, items in rendered.items():
        owners: dict[Any, str] = {}
        for item in items:
            try:
                network = ip_network(item["prefix"], strict=False)
            except ValueError as error:
                raise _refuse(who, f"segment {item['name']!r} has the prefix {item['prefix']!r}: {error}") from error
            if owners.setdefault(network, item["name"]) != item["name"]:
                raise _refuse(
                    who,
                    f"segments {owners[network]!r} and {item['name']!r} share the prefix {network} in VRF {vrf} "
                    "(a prefix can be in one segment per VRF)",
                )
        for (net_a, name_a), (net_b, name_b) in combinations(sorted(owners.items(), key=lambda pair: pair[1]), 2):
            if net_a.version == net_b.version and net_a.overlaps(net_b):
                for own, other, other_name in ((name_a, net_b, name_b), (name_b, net_a, name_a)):
                    nested[own].append(
                        f"{_UNVERIFIED}: this prefix overlaps {other} of segment {other_name}; nested prefixes are "
                        "expected to match longest-prefix first, which is not documented (spec 2.5)"
                    )

    catalog = _MssgCatalog(who)
    match_lists = []
    vrf_views: list[dict[str, Any]] = []
    for vrf, items in rendered.items():
        entries_to = {
            (item["name"], policy["from"]): _entry_set(policy["entries"])
            for item in items
            for policy in item["policies"]
        }
        segment_views = []
        in_vrf = {item["name"] for item in items}
        for item in items:
            name = item["name"]
            family = "ipv6" if ":" in item["prefix"] else "ipv4"
            comments = list(nested.get(name, []))
            comments += [
                f"not rendered (source in another VRF): from {_cli_name(policy['from'])} "
                f"(rules: {', '.join(policy['rules'])})"
                for policy in item["policies"]
                if policy["from"] not in in_vrf
            ]
            match_lists.append({"name": _cli_name(name), "family": family, "prefix": item["prefix"]})
            froms: dict[str, str] = {}
            if item["intra_segment"] == "permit":
                froms[name] = _MSSG_FORWARD_ALL
            for other in items:
                forward, reply = entries_to.get((name, other["name"])), entries_to.get((other["name"], name))
                if (forward or reply) and other["name"] != name:
                    policy = catalog.policy(forward or set(), reply or set())
                    if policy:
                        froms[other["name"]] = policy
            fallback = None
            notes = []
            if any(_fallback_entries(item)):
                fallback = catalog.policy(*_fallback_entries(item))
                dropped = [other["name"] for other in items if other["name"] not in froms]
                froms.update({name: _MSSG_DROP_ALL for name in dropped})
                notes = [f"{_UNVERIFIED}: fallback policy is documented by AVD only (R3 series)"]
                if dropped:
                    notes.append(
                        f"{_UNVERIFIED}: the fallback also catches a source in a segment without a from line, "
                        "so each of those gets an explicit policy-drop-all (expected to win over the fallback)"
                    )
            segment_views.append(
                {
                    "name": _cli_name(name),
                    "match_list": _cli_name(name),
                    "family": family,
                    "prefix": item["prefix"],
                    "remote": name not in contracts.local_names,
                    "comments": comments,
                    "froms": [
                        {"source": _cli_name(source), "policy": froms[source]}
                        for source in sorted(froms, key=_cli_name)
                    ],
                    "fallback": fallback,
                    "notes": notes,
                }
            )
        vrf_views.append({"name": vrf, "segments": segment_views})

    unsupported = [
        _one_line(f"{_label(row['src'])} -> {_label(row['dst'])}: {row['reason']} (rules: {', '.join(row['rules'])})")
        for row in view["unsupported"]
        if (by_name.get(row["dst"]) or by_name[row["src"]]).vrf in rendered
    ]
    staged = _staged_reason(
        contracts, scope.dc_id, "segments and policies are configured, segment security is shut down"
    )
    return {
        "enforce": contracts.enforce,
        "staged": staged,
        "avd_only": bool(catalog.policies or catalog.field_sets or catalog.applications)
        or any(segment["fallback"] for vrf_view in vrf_views for segment in vrf_view["segments"]),
        "match_lists": sorted(match_lists, key=lambda item: (item["name"], item["family"])),
        "field_sets": [catalog.field_sets[name] for name in sorted(catalog.field_sets)],
        "applications": [catalog.applications[name] for name in sorted(catalog.applications)],
        "policies": [catalog.policies[name] for name in sorted(catalog.policies)],
        "vrfs": vrf_views,
        "skipped": [
            {
                "vrf": vrf,
                "reason": _one_line(
                    f"segment(s) {', '.join(names)} have no gateway prefix to classify by, and segment security is "
                    "enforced box-wide, so the VRF is left out"
                ),
            }
            for vrf, names in sorted(skipped.items())
        ],
        "unsupported": unsupported,
    }


# ---------------------------------------------------------------------------
# The dispatcher
# ---------------------------------------------------------------------------

_BUILDERS = {"vxlan_gpo": _vxlan_gpo_build, "segment_security": _segment_security_build}


def group_policy_device_view(
    roots: dict[str, Any],
    *,
    device_name: str,
    role: str,
    platform: str,
    activations: Iterable[dict[str, Any]],
    vrfs: Iterable[str],
    interfaces: Iterable[dict[str, Any]] = (),
) -> tuple[str, dict[str, Any]] | None:
    """(group-policy kind, its device view) of a VTEP that renders group policy, else None.

    The one gate of every renderer: the role is a data-center VTEP, the device sits in exactly one data
    center, that data center is a reference fabric (utils/group_policy) and its kind has a renderer for the
    platform (``GPO_RENDERERS``). Then the data center's contracts are built once and the kind's builder
    cuts them to this device: ``_vxlan_gpo_build`` (NX-OS) or ``_segment_security_build`` (EOS).
    A refusal (``... refusing unprotected config``) can only come from a device that does render.
    """
    found = _gpo_data_center(roots, device_name, platform) if role in DC_VTEP_ROLES else None
    if found is None:
        return None
    dc_id, kind, fleet, profiles = found
    scope = _DeviceScope(
        who=f"{platform} {role} {device_name}",
        kind=kind,
        dc_id=dc_id,
        fleet=fleet,
        contracts=data_center_contracts(roots, dc_id, fleet=fleet, profiles=profiles),
        device_name=device_name,
        activations=list(activations),
        vrfs=frozenset(vrfs),
        interfaces=list(interfaces),
    )
    view = _BUILDERS[kind](scope)
    return None if view is None else (kind, view)
