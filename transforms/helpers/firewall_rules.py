"""The rules of a firewall's served segments: finding the ones the query named by id, and putting them back."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


class FirewallRulesError(RuntimeError):
    """A rule the registered query named could not be read back, or there is no client to read it."""


def bare_rule_edges(data: Any) -> Iterator[tuple[dict[str, Any], bool]]:
    """Every rule edge that carries only an id, and whether it is an inbound rule (it then carries its policy)."""
    if isinstance(data, dict):
        policy = data["security_policy"].get("node") if isinstance(data.get("security_policy"), dict) else None
        if isinstance(policy, dict):
            for edge in (policy.get("rules") or {}).get("edges") or []:
                if _id_only(edge):
                    yield edge, False
        if isinstance(data.get("inbound_rules"), dict):
            for edge in data["inbound_rules"].get("edges") or []:
                if _id_only(edge):
                    yield edge, True
        for value in data.values():
            yield from bare_rule_edges(value)
    elif isinstance(data, list):
        for value in data:
            yield from bare_rule_edges(value)


def _id_only(edge: Any) -> bool:
    node = edge.get("node") if isinstance(edge, dict) else None
    return isinstance(node, dict) and set(node) == {"id"}


def rule_ids(edges: list[tuple[dict[str, Any], bool]]) -> list[str]:
    """The distinct ids of the rules the edges name, sorted."""
    return sorted({edge["node"]["id"] for edge, _ in edges})


def segment_ids(rules: dict[str, dict[str, Any]]) -> list[str]:
    """The distinct ids of the segments the rules name: their two ends and the segment of their policy, sorted."""
    return sorted(
        {
            end["node"]["id"]
            for rule in rules.values()
            for end in (
                rule.get("source_segment"),
                rule.get("destination_segment"),
                ((rule.get("policy") or {}).get("node") or {}).get("segment"),
            )
            if isinstance(end, dict) and end.get("node")
        }
    )


def chunks(ids: list[str], size: int) -> list[list[str]]:
    """``ids`` in consecutive lists of at most ``size``."""
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _with_segment(relation: Any, segments: dict[str, dict[str, Any]]) -> Any:
    """A ``{node: {id}}`` segment end with the segment read in full (an empty end stays empty)."""
    node = (relation or {}).get("node") if isinstance(relation, dict) else None
    if not node:
        return relation
    return {**relation, "node": segments[node["id"]]}


def put_rules_back(
    edges: list[tuple[dict[str, Any], bool]],
    rules: dict[str, dict[str, Any]],
    segments: dict[str, dict[str, Any]],
) -> int:
    """Replace the node of every bare rule edge by the rule read in full; returns how many edges were filled."""
    missing = sorted(set(rule_ids(edges)) - set(rules))
    if missing:
        raise FirewallRulesError(f"{len(missing)} rules named by the query could not be read back: {missing[:5]}")
    for edge, inbound in edges:
        rule = rules[edge["node"]["id"]]
        node = {key: value for key, value in rule.items() if key != "policy"}
        node["source_segment"] = _with_segment(rule.get("source_segment"), segments)
        node["destination_segment"] = _with_segment(rule.get("destination_segment"), segments)
        if inbound:
            policy = (rule.get("policy") or {}).get("node")
            node["policy"] = (
                {"node": {**policy, "segment": _with_segment(policy.get("segment"), segments)}}
                if policy
                else rule.get("policy")
            )
        edge["node"] = node
    return len(edges)
