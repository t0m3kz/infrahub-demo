"""Group contracts between the component tags of one segment (stateless, forward rows).

Built from the segment's own rules (``segment_flows``), the one rule view
every enforcement point reads. Only an intra-segment flow is a contract:
two components of one segment talk bridged, past the SVI ACL, while every
routed flow (to another segment, through PBR to a firewall, to the outside)
meets that ACL and no contract. So the firewall's
flows are exempt by construction and nothing here re-classifies them.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any

from transforms.helpers.policy import segment_flows

REPLIES = "bidir (I/LS)"
"""How a renderer answers the reverse direction of a forward row: NX-OS renders the
contract bidir (D: its default); whether bidir reverses the L4 ports is I/LS. A
stateless renderer without bidir adds mirrored rows itself."""


def _entry(rule: dict[str, Any]) -> tuple[str, int | None, int | None]:
    """(protocol, port_start, port_end) of a rule; a single port has port_end == port_start."""
    protocol = str(rule.get("protocol") or "any")
    port_start = rule.get("port_start")
    if protocol not in ("tcp", "udp") or not port_start:
        return protocol, None, None
    return protocol, int(port_start), int(rule.get("port_end") or port_start)


def _row(
    segment: str,
    src_tag: int,
    dst_tag: int,
    action: str,
    entries: set[tuple[str, int | None, int | None]],
    rules: set[str],
) -> dict[str, Any]:
    if any(protocol == "any" for protocol, _start, _end in entries):
        entries = {("any", None, None)}
    return {
        "segment": segment,
        "src_tag": src_tag,
        "dst_tag": dst_tag,
        "action": action,
        "entries": [
            {"protocol": protocol, "port_start": start, "port_end": end}
            for protocol, start, end in sorted(entries, key=lambda item: (item[0], item[1] or 0, item[2] or 0))
        ],
        "rules": sorted(rules),
        "replies": REPLIES,
    }


def get_group_contracts(segment: dict[str, Any], tags: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The contract rows between the tags of one segment that needs group policy.

    ``segment`` is a cleaned segment with its ``security_policy`` (rules with
    their ``source_security_tag`` / ``destination_security_tag``); ``tags``
    are the SecurityTags of its components (``id``, ``group_id``).

    - A permit rule from the segment to itself whose two tags are both the
      segment's and differ adds its (protocol, ports) to the row of that
      ordered tag pair; disabled rules and policies are already gone
      (segment_flows). ``any`` collapses the row to permit-all.
    - Every unordered pair of the segment's tags with no rule either way gets
      a deny-all row (no entries): the VRF is default permit.
    - The same tag (instances of one component) is never a row: permitted.

    Rows are sorted by (src_tag, dst_tag), id-free, entries and rule names
    de-duplicated and sorted.
    """
    segment_id = segment.get("id")
    segment_name = str(segment.get("name") or segment_id)
    group_ids = {
        str(tag["id"]): int(tag["group_id"]) for tag in tags if tag.get("id") and tag.get("group_id") is not None
    }

    permits: dict[tuple[int, int], tuple[set[tuple[str, int | None, int | None]], set[str]]] = {}
    for rule in segment_flows(segment)[0]["rules"]:
        if rule.get("action") != "permit" or (rule.get("destination_segment") or {}).get("id") != segment_id:
            continue
        src = group_ids.get(str((rule.get("source_security_tag") or {}).get("id")))
        dst = group_ids.get(str((rule.get("destination_security_tag") or {}).get("id")))
        if src is None or dst is None or src == dst:
            continue
        entries, names = permits.setdefault((src, dst), (set(), set()))
        entries.add(_entry(rule))
        names.add(str(rule.get("name") or rule.get("id")))

    rows = {pair: _row(segment_name, *pair, "permit", entries, names) for pair, (entries, names) in permits.items()}
    for low, high in combinations(sorted(set(group_ids.values())), 2):
        if (low, high) not in permits and (high, low) not in permits:
            rows[(low, high)] = _row(segment_name, low, high, "deny", set(), set())
    return [rows[pair] for pair in sorted(rows)]
