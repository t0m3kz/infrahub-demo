"""The EOS MSS-G (segment_security) view and the device builder of its VTEP configs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from ipaddress import ip_network
from itertools import combinations
from typing import Any

from transforms.helpers.group_policy.common import (
    _UNVERIFIED,
    _cli_name,
    _DeviceScope,
    _digest,
    _label,
    _one_line,
    _refuse,
    _refuse_ambiguous_groups,
    _refuse_cli_clash,
    _staged_reason,
)
from transforms.helpers.group_policy.model import (
    DEFAULT_ACTION,
    FALLBACK,
    UNCLASSIFIED,
    Entry,
    Group,
    _entries,
    _entry_set,
)
from utils.limits import EOS_MSSG_MAX_SEGMENTS
from utils.segment_addressing import classifier_prefix

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
      ``from`` line, so those get an explicit ``policy-drop-all``); at most ``EOS_MSSG_MAX_SEGMENTS``;
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
    if count > EOS_MSSG_MAX_SEGMENTS:
        raise _refuse(
            who,
            f"{count} segments to define exceed the {EOS_MSSG_MAX_SEGMENTS} an EOS device holds across all VRFs "
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
