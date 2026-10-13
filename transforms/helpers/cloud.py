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
        "disabled": False,
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
        "policy_derived": True,
    }


def _attach_policy_security_groups(vpcs: list[dict[str, Any]], instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Policy-derived groups for every segment of `vpcs`, bound to the segment's instances.

    Instances reach their group through ``network_segment`` (the same
    relationship their subnet comes from); the group is appended to the
    instance's own ``security_groups``.
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
        attached = inst.get("security_groups") or []
        if sg_name and sg_name not in {sg.get("name") for sg in attached}:
            inst["security_groups"] = [*attached, {"name": sg_name}]
    return groups


def prepare_cloud_data(cleaned: dict[str, Any]) -> dict[str, Any]:
    """Extract, filter and group all cloud resources from a cleaned GraphQL response.

    Returns a dict with keys:
      vpcs, provider_name, account,
      security_groups, instances, internet_gateways, nat_gateways,
      public_ips, transit_gateways, customer_gateways, direct_connects,
      route_tables, routes, network_acls, vpn_gateways, peerings, vifs,
      auto_scaling_groups,
      sgs_by_vpc, igws_by_vpc, nats_by_vpc, rts_by_vpc, nacls_by_vpc,
      vpngws_by_vpc, inst_by_vpc, asgs_by_vpc,
      regions_seen, vpc_region_map
    """
    vpcs = cleaned.get("CloudVirtualNetwork") or []

    instances = cleaned.get("CloudInstance") or []
    security_groups = [*(cleaned.get("CloudSecurityGroup") or []), *_attach_policy_security_groups(vpcs, instances)]
    internet_gateways = cleaned.get("CloudInternetGateway") or []
    nat_gateways = cleaned.get("CloudNATGateway") or []
    public_ips = cleaned.get("CloudPublicIP") or []
    transit_gateways = cleaned.get("CloudTransitGateway") or []
    customer_gateways = cleaned.get("CloudCustomerGateway") or []
    direct_connects = cleaned.get("CloudDirectConnect") or []
    # VirtCluster replaces CloudAutoScalingGroup. Flatten node pools so the rest
    # of the transforms keep working with the same asg dict shape.
    auto_scaling_groups: list[dict] = []
    for cluster in cleaned.get("VirtCluster") or []:
        cluster_vnet = cluster.get("virtual_network")
        for pool in cluster.get("node_pools") or []:
            auto_scaling_groups.append(
                {
                    "name": pool.get("name", ""),
                    "instance_type": pool.get("instance_flavor", ""),
                    "image": pool.get("image", ""),
                    "os_type": pool.get("os_type", ""),
                    "min_size": pool.get("min_size"),
                    "max_size": pool.get("max_size"),
                    "desired_capacity": pool.get("desired_capacity"),
                    "virtual_network": cluster_vnet,
                    "network_segments": pool.get("network_segments") or [],
                    "security_groups": pool.get("security_groups") or [],
                }
            )

    vpc_names: set[str] = {v.get("name", "") for v in vpcs}

    route_tables = [
        rt
        for rt in (cleaned.get("CloudRouteTable") or [])
        if (rt.get("virtual_network") or {}).get("name", "") in vpc_names
    ]
    network_acls = [
        n
        for n in (cleaned.get("CloudNetworkACL") or [])
        if (n.get("virtual_network") or {}).get("name", "") in vpc_names
    ]
    vpn_gateways = [
        g
        for g in (cleaned.get("CloudVPNGateway") or [])
        if (g.get("virtual_network") or {}).get("name", "") in vpc_names
    ]
    peerings = [
        p
        for p in (cleaned.get("CloudVirtualNetworkPeering") or [])
        if (p.get("requester_virtual_network") or {}).get("name", "") in vpc_names
    ]

    dc_names: set[str] = {dc.get("name", "") for dc in direct_connects}
    vifs = [
        v
        for v in (cleaned.get("CloudVirtualInterface") or [])
        if (v.get("direct_connect") or {}).get("name", "") in dc_names
    ]

    rt_names: set[str] = {rt.get("name", "") for rt in route_tables}
    routes = [r for r in (cleaned.get("CloudRoute") or []) if (r.get("route_table") or {}).get("name", "") in rt_names]

    account = vpcs[0].get("account") or {} if vpcs else {}
    raw_provider = (account.get("provider") or {}).get("name", "aws").lower()
    provider_name = raw_provider if raw_provider in ("aws", "azure", "gcp") else "aws"

    def _group(items: list, key: str = "virtual_network") -> dict[str, list]:
        result: dict[str, list] = {}
        for item in items:
            k = (item.get(key) or {}).get("name", "")
            result.setdefault(k, []).append(item)
        return result

    inst_by_vpc: dict[str, list] = {}
    for inst in instances:
        seg = inst.get("network_segment") or {}
        k = (seg.get("virtual_network") or {}).get("name", "")
        inst_by_vpc.setdefault(k, []).append(inst)

    asgs_by_vpc: dict[str, list] = {}
    for asg in auto_scaling_groups:
        k = (asg.get("virtual_network") or {}).get("name", "")
        asgs_by_vpc.setdefault(k, []).append(asg)

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
        "security_groups": security_groups,
        "instances": instances,
        "internet_gateways": internet_gateways,
        "nat_gateways": nat_gateways,
        "public_ips": public_ips,
        "transit_gateways": transit_gateways,
        "customer_gateways": customer_gateways,
        "direct_connects": direct_connects,
        "route_tables": route_tables,
        "routes": routes,
        "network_acls": network_acls,
        "vpn_gateways": vpn_gateways,
        "peerings": peerings,
        "vifs": vifs,
        "auto_scaling_groups": auto_scaling_groups,
        "sgs_by_vpc": _group(security_groups),
        "igws_by_vpc": _group(internet_gateways),
        "nats_by_vpc": _group(nat_gateways),
        "rts_by_vpc": _group(route_tables),
        "nacls_by_vpc": _group(network_acls),
        "vpngws_by_vpc": _group(vpn_gateways),
        "inst_by_vpc": inst_by_vpc,
        "asgs_by_vpc": asgs_by_vpc,
        "regions_seen": regions_seen,
        "vpc_region_map": vpc_region_map,
    }
