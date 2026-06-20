"""Shared helpers for cloud transforms (Terraform and Pulumi)."""

from typing import Any


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

    security_groups = cleaned.get("CloudSecurityGroup") or []
    instances = cleaned.get("CloudInstance") or []
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
