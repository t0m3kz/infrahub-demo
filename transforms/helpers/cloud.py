"""Shared helpers for cloud transforms (Terraform and Pulumi)."""

import logging
from typing import Any

from transforms.helpers.policy import rule_endpoint, segment_flows

_log = logging.getLogger(__name__)

# Terraform-side priorities for policy-derived rules: one number per rule, in
# rule order within a direction (Azure needs 100-4096, unique per direction).
_PRIORITY_BASE = 100
_PRIORITY_STEP = 10


def _sg_rule(direction: str, position: int, rule: dict[str, Any], peer: str) -> dict[str, Any]:
    """One security group rule from a policy rule permit and its resolved peer CIDR."""
    return {
        "name": f"{direction}-{position}-{rule.get('name') or rule.get('index')}",
        "direction": direction,
        "priority": _PRIORITY_BASE + _PRIORITY_STEP * position,
        "protocol": rule.get("protocol") or "any",
        "port_start": rule.get("port_start"),
        "port_end": rule.get("port_end"),
        "action": "permit",
        "source_cidr": peer if direction == "ingress" else None,
        "dest_cidr": peer if direction == "egress" else None,
    }


def policy_security_group(segment: dict[str, Any], vpc_name: str) -> dict[str, Any] | None:
    """The security group a cloud segment's policy rules render to, or None.

    A stateful consumer of ``segment_flows``: egress rules are the segment's own
    permits (peer = destination), ingress rules the permits into it (peer =
    source), both in index order and without reply legs (the session carries
    the reply). The peer is the rule's segment prefix (gateway, else
    cidr_block) or its prefix/IP selector; a rule whose peer does not resolve
    is skipped with a warning, never widened to 0.0.0.0/0. A segment with no
    rule that resolves yields no group, so segments without policy data render
    exactly as before.
    """
    sg_name = f"{segment.get('name') or ''}-sg"
    egress, ingress = segment_flows(segment)
    flows = (
        ("egress", "destination", [r for r in egress["rules"] if r.get("action") == "permit"]),
        ("ingress", "source", sorted(ingress, key=lambda r: (r.get("index") or 0, r.get("name") or ""))),
    )
    rules: list[dict[str, Any]] = []
    for direction, peer_side, flow in flows:
        position = 0
        for rule in flow:
            peer = rule_endpoint(rule, peer_side)
            if not peer:
                _log.warning(
                    "Skipping %s rule %r of %s: its %s does not resolve to an address",
                    direction,
                    rule.get("name"),
                    sg_name,
                    peer_side,
                )
                continue
            rules.append(_sg_rule(direction, position, rule, peer))
            position += 1
    if not rules:
        return None
    return {
        "name": sg_name,
        "virtual_network": {"name": vpc_name},
        "segment": segment.get("name") or "",
        "rules": rules,
    }


def _attach_policy_security_groups(vpcs: list[dict[str, Any]], instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Policy-derived groups for every segment of `vpcs`, bound to the segment's instances.

    Instances reach their group through ``network_segment`` (the same
    relationship their subnet comes from); the group is set as the
    instance's ``security_groups``.
    """
    groups: list[dict[str, Any]] = []
    by_segment: dict[tuple[str, str], str] = {}
    for vpc in vpcs:
        vpc_name = vpc.get("name", "")
        for segment in vpc.get("network_segments") or []:
            group = policy_security_group(segment, vpc_name)
            if group:
                groups.append(group)
                by_segment[(vpc_name, segment.get("name", ""))] = group["name"]
    for inst in instances:
        seg = inst.get("network_segment") or {}
        key = ((seg.get("virtual_network") or {}).get("name", ""), seg.get("name", ""))
        sg_name = by_segment.get(key)
        if sg_name:
            inst["security_groups"] = [{"name": sg_name}]
    return groups


def prepare_cloud_data(cleaned: dict[str, Any]) -> dict[str, Any]:
    """Extract, filter and group all cloud resources from a cleaned GraphQL response.

    Returns a dict with keys:
      vpcs, provider_name, account,
      transit_gateways, direct_connects, vifs,
      sgs_by_vpc, inst_by_vpc,
      regions_seen, vpc_region_map
    """
    vpcs = cleaned.get("CloudVirtualNetwork") or []

    instances = cleaned.get("CloudInstance") or []
    transit_gateways = cleaned.get("CloudTransitGateway") or []
    direct_connects = cleaned.get("CloudDirectConnect") or []

    dc_names: set[str] = {dc.get("name", "") for dc in direct_connects}
    vifs = [
        v
        for v in (cleaned.get("CloudVirtualInterface") or [])
        if (v.get("direct_connect") or {}).get("name", "") in dc_names
    ]

    account = vpcs[0].get("account") or {} if vpcs else {}
    raw_provider = (account.get("provider") or {}).get("name", "aws").lower()
    provider_name = raw_provider if raw_provider in ("aws", "azure", "gcp") else "aws"

    sgs_by_vpc: dict[str, list] = {}
    for sg in _attach_policy_security_groups(vpcs, instances):
        sgs_by_vpc.setdefault((sg.get("virtual_network") or {}).get("name", ""), []).append(sg)

    inst_by_vpc: dict[str, list] = {}
    for inst in instances:
        seg = inst.get("network_segment") or {}
        k = (seg.get("virtual_network") or {}).get("name", "")
        inst_by_vpc.setdefault(k, []).append(inst)

    regions_seen: list[str] = []
    vpc_region_map: dict[str, str] = {}
    for vpc in vpcs:
        region = (vpc.get("region") or {}).get("name", "")
        vpc_region_map[vpc.get("name", "")] = region
        if region and region not in regions_seen:
            regions_seen.append(region)

    return {
        "vpcs": vpcs,
        "provider_name": provider_name,
        "account": account,
        "transit_gateways": transit_gateways,
        "direct_connects": direct_connects,
        "vifs": vifs,
        "sgs_by_vpc": sgs_by_vpc,
        "inst_by_vpc": inst_by_vpc,
        "regions_seen": regions_seen,
        "vpc_region_map": vpc_region_map,
    }
