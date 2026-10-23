"""AWS HCL rendering for the cloud VPC Terraform transform."""

import logging
from typing import Any

from transforms.cloud.terraform_common import tf_id

_log = logging.getLogger(__name__)


def _bool_hcl(value: Any) -> str:
    return "true" if value else "false"


def _enable_disable(value: Any) -> str:
    return "enable" if value else "disable"


def _res(resource_type: str, resource_id: str, attrs: list[str], provider_attr: str = "") -> list[str]:
    """Emit a flat HCL resource block with optional provider attribute."""
    lines = [f'resource "{resource_type}" "{resource_id}" {{']
    lines += [f"  {a}" for a in attrs]
    if provider_attr:
        lines.append(f"  provider = {provider_attr}")
    lines += ["}", ""]
    return lines


def _provider_block_aws(regions: list[str]) -> list[str]:
    lines = [
        "terraform {",
        "  required_providers {",
        "    aws = {",
        '      source  = "hashicorp/aws"',
        '      version = "~> 5.0"',
        "    }",
        "  }",
        "}",
        "",
    ]
    default_region = regions[0] if regions else ""
    if default_region:
        lines += [
            'provider "aws" {',
            f'  region = "{default_region}"',
            "}",
            "",
        ]
    else:
        lines += ['provider "aws" {}', ""]
    for region in regions[1:]:
        alias = tf_id(region)
        lines += [
            'provider "aws" {',
            f'  alias  = "{alias}"',
            f'  region = "{region}"',
            "}",
            "",
        ]
    return lines


def _vpc_blocks_aws(vpc: dict, ctx: dict, provider_attr: str = "") -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_id = tf_id(vpc_name)

    lines += _network_blocks_aws(vpc, vpc_name, vpc_id, provider_attr)
    lines += _security_group_blocks_aws(vpc_name, vpc_id, ctx, provider_attr)
    lines += _instance_blocks_aws(vpc_name, ctx, provider_attr)
    lines += _gateway_blocks_aws(vpc_name, vpc_id, ctx, provider_attr)
    lines += _route_table_blocks_aws(vpc_name, vpc_id, ctx, provider_attr)
    lines += _network_acl_blocks_aws(vpc_name, vpc_id, ctx, provider_attr)
    lines += _vpn_gateway_blocks_aws(vpc_name, vpc_id, ctx, provider_attr)
    lines += _autoscaling_blocks_aws(vpc_name, vpc_id, ctx, provider_attr)

    return lines


def _network_blocks_aws(vpc: dict, vpc_name: str, vpc_id: str, provider_attr: str) -> list[str]:
    lines: list[str] = []
    cidr_blocks = vpc.get("cidr_blocks") or []
    first_cidr = cidr_blocks[0].get("prefix", "") if cidr_blocks else ""

    lines += _res(
        "aws_vpc",
        vpc_id,
        [
            f'cidr_block           = "{first_cidr}"',
            f"enable_dns_support   = {_bool_hcl(vpc.get('dns_support'))}",
            f"enable_dns_hostnames = {_bool_hcl(vpc.get('dns_hostnames'))}",
            f'tags = {{ Name = "{vpc_name}", ManagedBy = "Infrahub" }}',
        ],
        provider_attr,
    )

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_id = tf_id(seg_name)
        cidr = seg.get("cidr_block") or ""
        az = (seg.get("availability_zone") or {}).get("name", "")
        lines += _res(
            "aws_subnet",
            seg_id,
            [
                f"vpc_id                  = aws_vpc.{vpc_id}.id",
                f'cidr_block              = "{cidr}"',
                f'availability_zone       = "{az}"',
                f"map_public_ip_on_launch = {_bool_hcl(seg.get('auto_assign_public_ip'))}",
                f'tags = {{ Name = "{seg_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    return lines


def _security_group_blocks_aws(vpc_name: str, vpc_id: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    for sg in ctx["sgs_by_vpc"].get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_id = tf_id(sg_name)
        lines += _res(
            "aws_security_group",
            sg_id,
            [
                f'name   = "{sg_name}"',
                f"vpc_id = aws_vpc.{vpc_id}.id",
                f'tags   = {{ Name = "{sg_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )
        # Emit inline security group rules
        for rule in sg.get("rules") or []:
            if rule.get("disabled"):
                continue
            rule_name = rule.get("name", "")
            rule_id = tf_id(f"{sg_name}_{rule_name}")
            direction = rule.get("direction", "ingress")
            # AWS uses separate resource types for ingress vs egress
            res_type = (
                "aws_vpc_security_group_ingress_rule"
                if direction == "ingress"
                else "aws_vpc_security_group_egress_rule"
            )
            proto = rule.get("protocol", "tcp")
            aws_proto = "-1" if proto == "any" else proto
            attrs = [
                f"security_group_id = aws_security_group.{sg_id}.id",
                f'ip_protocol       = "{aws_proto}"',
            ]
            if proto != "any":
                p_start = rule.get("port_start")
                p_end = rule.get("port_end") or p_start
                if p_start:
                    attrs += [
                        f"from_port = {p_start}",
                        f"to_port   = {p_end}",
                    ]
            if direction == "ingress" and rule.get("source_cidr"):
                attrs.append(f'cidr_ipv4 = "{rule["source_cidr"]}"')
            elif direction == "egress" and rule.get("dest_cidr"):
                attrs.append(f'cidr_ipv4 = "{rule["dest_cidr"]}"')
            else:
                _log.warning("Skipping %s rule %r of %s: no peer CIDR", direction, rule_name, sg_name)
                continue
            lines += _res(res_type, rule_id, attrs, provider_attr)

    return lines


def _instance_blocks_aws(vpc_name: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    for inst in ctx["inst_by_vpc"].get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_id = tf_id(inst_name)
        seg_ref = inst.get("network_segment") or {}
        subnet_ref = f"aws_subnet.{tf_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        sg_ids = ", ".join(
            f"aws_security_group.{tf_id(sg.get('name', ''))}.id"
            for sg in (inst.get("security_groups") or [])
            if sg.get("name")
        )
        inst_attrs = [
            f'ami                    = "{inst.get("image", "")}"',
            f'instance_type          = "{inst.get("instance_type", "")}"',
            f"subnet_id              = {subnet_ref}",
            f"vpc_security_group_ids = [{sg_ids}]",
        ]
        if inst.get("private_ip"):
            inst_attrs.append(f'private_ip             = "{inst["private_ip"]}"')
        inst_attrs.append(f'tags                   = {{ Name = "{inst_name}", ManagedBy = "Infrahub" }}')
        # aws_instance needs special alignment for provider key — emit manually
        block = [f'resource "aws_instance" "{inst_id}" {{']
        block += [f"  {a}" for a in inst_attrs]
        if provider_attr:
            block.append(f"  provider               = {provider_attr}")
        block += ["}", ""]
        lines += block

    return lines


def _gateway_blocks_aws(vpc_name: str, vpc_id: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    # Internet Gateways
    for igw in ctx["igws_by_vpc"].get(vpc_name) or []:
        igw_name = igw.get("name", "")
        igw_id = tf_id(igw_name)
        lines += _res(
            "aws_internet_gateway",
            igw_id,
            [
                f"vpc_id = aws_vpc.{vpc_id}.id",
                f'tags   = {{ Name = "{igw_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    # NAT Gateways (with optional EIP)
    for nat in ctx["nats_by_vpc"].get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_id = tf_id(nat_name)
        nat_segs = nat.get("network_segments") or []
        first_seg_name = nat_segs[0].get("name", "") if nat_segs else ""
        subnet_ref = f"aws_subnet.{tf_id(first_seg_name)}.id" if first_seg_name else '""'

        if nat.get("public_ip"):
            eip_id = f"eip_{nat_id}"
            lines += _res(
                "aws_eip",
                eip_id,
                [
                    'domain = "vpc"',
                    f'tags   = {{ Name = "{nat_name}-eip", ManagedBy = "Infrahub" }}',
                ],
                provider_attr,
            )
            lines += _res(
                "aws_nat_gateway",
                nat_id,
                [
                    f"allocation_id = aws_eip.{eip_id}.id",
                    f"subnet_id     = {subnet_ref}",
                    f'tags          = {{ Name = "{nat_name}", ManagedBy = "Infrahub" }}',
                ],
                provider_attr,
            )
        else:
            lines += _res(
                "aws_nat_gateway",
                nat_id,
                [
                    f"subnet_id = {subnet_ref}",
                    f'tags      = {{ Name = "{nat_name}", ManagedBy = "Infrahub" }}',
                ],
                provider_attr,
            )

    return lines


def _route_table_blocks_aws(vpc_name: str, vpc_id: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    # Route Tables and their subnet associations
    for rt in ctx["rts_by_vpc"].get(vpc_name) or []:
        rt_name = rt.get("name", "")
        rt_id = tf_id(rt_name)
        lines += _res(
            "aws_route_table",
            rt_id,
            [
                f"vpc_id = aws_vpc.{vpc_id}.id",
                f'tags   = {{ Name = "{rt_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

        # Routes belonging to this route table
        for route in ctx["routes"]:
            route_rt = (route.get("route_table") or {}).get("name", "")
            if route_rt != rt_name:
                continue
            route_name = route.get("name", "")
            route_id = (
                tf_id(route_name)
                if route_name
                else f"{rt_id}_route_{tf_id((route.get('destination') or {}).get('prefix', 'default'))}"
            )
            dest = (route.get("destination") or {}).get("prefix", "")
            igw_ref = (route.get("internet_gateway") or {}).get("name", "")
            nat_ref = (route.get("nat_gateway") or {}).get("name", "")
            inst_ref = (route.get("instance") or {}).get("name", "")
            route_attrs = [
                f"route_table_id         = aws_route_table.{rt_id}.id",
                f'destination_cidr_block = "{dest}"',
            ]
            if igw_ref:
                route_attrs.append(f"gateway_id             = aws_internet_gateway.{tf_id(igw_ref)}.id")
            elif nat_ref:
                route_attrs.append(f"nat_gateway_id         = aws_nat_gateway.{tf_id(nat_ref)}.id")
            elif inst_ref:
                route_attrs.append(f"instance_id            = aws_instance.{tf_id(inst_ref)}.id")
            # Use aligned provider key for aws_route (matches original spacing)
            route_block = [f'resource "aws_route" "{route_id}" {{']
            route_block += [f"  {a}" for a in route_attrs]
            if provider_attr:
                route_block.append(f"  provider               = {provider_attr}")
            route_block += ["}", ""]
            lines += route_block

        # Subnet associations
        for seg in rt.get("network_segments") or []:
            seg_name = seg.get("name", "")
            seg_id = tf_id(seg_name)
            assoc_id = f"{seg_id}_assoc_{rt_id}"
            assoc_block = [f'resource "aws_route_table_association" "{assoc_id}" {{']
            assoc_block += [
                f"  subnet_id      = aws_subnet.{seg_id}.id",
                f"  route_table_id = aws_route_table.{rt_id}.id",
            ]
            if provider_attr:
                assoc_block.append(f"  provider       = {provider_attr}")
            assoc_block += ["}", ""]
            lines += assoc_block

    return lines


def _network_acl_blocks_aws(vpc_name: str, vpc_id: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    # Network ACLs
    for nacl in ctx["nacls_by_vpc"].get(vpc_name) or []:
        nacl_name = nacl.get("name", "")
        nacl_id = tf_id(nacl_name)
        nacl_segs = nacl.get("network_segments") or []
        subnet_ids = ", ".join(f"aws_subnet.{tf_id(s.get('name', ''))}.id" for s in nacl_segs if s.get("name"))
        nacl_block = [f'resource "aws_network_acl" "{nacl_id}" {{']
        nacl_block += [
            f"  vpc_id     = aws_vpc.{vpc_id}.id",
            f"  subnet_ids = [{subnet_ids}]",
            f'  tags       = {{ Name = "{nacl_name}", ManagedBy = "Infrahub" }}',
        ]
        if provider_attr:
            nacl_block.append(f"  provider   = {provider_attr}")
        nacl_block += ["}", ""]
        lines += nacl_block

    return lines


def _vpn_gateway_blocks_aws(vpc_name: str, vpc_id: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    # VPN Gateways (per-VPC)
    for vpngw in ctx["vpngws_by_vpc"].get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_id = tf_id(vpngw_name)
        lines += _res(
            "aws_vpn_gateway",
            vpngw_id,
            [
                f"vpc_id = aws_vpc.{vpc_id}.id",
                f'tags   = {{ Name = "{vpngw_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    return lines


def _autoscaling_blocks_aws(vpc_name: str, vpc_id: str, ctx: dict, provider_attr: str) -> list[str]:
    lines: list[str] = []
    # Auto Scaling Groups
    for asg in ctx["asgs_by_vpc"].get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_id = tf_id(asg_name)
        lt_id = f"{asg_id}_lt"
        subnet_refs = (
            ", ".join(f"aws_subnet.{tf_id(s.get('name', ''))}.id" for s in (asg.get("network_segments") or []))
            or f"aws_subnet.{vpc_id}_subnet.id"
        )
        sg_refs = ", ".join(
            f"aws_security_group.{tf_id(sg.get('name', ''))}.id" for sg in (asg.get("security_groups") or [])
        )
        lines += [
            f'resource "aws_launch_template" "{lt_id}" {{',
            f'  name          = "{asg_name}-lt"',
            f'  instance_type = "{asg.get("instance_type", "")}"',
            f'  image_id      = "{asg.get("image", "")}"',
        ]
        if sg_refs:
            lines.append(f"  vpc_security_group_ids = [{sg_refs}]")
        if provider_attr:
            lines.append(f"  provider = {provider_attr}")
        lines += [
            f'  tags = {{ Name = "{asg_name}-lt", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]
        lines += _res(
            "aws_autoscaling_group",
            asg_id,
            [
                f'name               = "{asg_name}"',
                f"min_size           = {asg.get('min_size', 1)}",
                f"max_size           = {asg.get('max_size', 1)}",
                f"desired_capacity   = {asg.get('desired_capacity', 1)}",
                f"vpc_zone_identifier = [{subnet_refs}]",
                "launch_template {",
                f"  id      = aws_launch_template.{lt_id}.id",
                '  version = "$Latest"',
                "}",
                'tag { key = "ManagedBy" value = "Infrahub" propagate_at_launch = true }',
            ],
            provider_attr,
        )

    return lines


def _account_blocks_aws(ctx: dict, provider_attr: str = "") -> list[str]:
    """Emit account-scoped AWS resources that are not tied to a single VPC."""
    lines: list[str] = []

    for tgw in ctx["transit_gateways"]:
        tgw_name = tgw.get("name", "")
        tgw_id = tf_id(tgw_name)
        # aws_transit_gateway uses wide alignment for provider key — emit manually
        tgw_block = [f'resource "aws_transit_gateway" "{tgw_id}" {{']
        tgw_block += [
            f'  default_route_table_association = "{_enable_disable(tgw.get("default_route_table"))}"',
            f'  dns_support                     = "{_enable_disable(tgw.get("dns_support"))}"',
            f'  vpn_ecmp_support                = "{_enable_disable(tgw.get("vpn_ecmp_support"))}"',
            f'  tags                            = {{ Name = "{tgw_name}", ManagedBy = "Infrahub" }}',
        ]
        if provider_attr:
            tgw_block.append(f"  provider                        = {provider_attr}")
        tgw_block += ["}", ""]
        lines += tgw_block

        for vpc_obj in tgw.get("attached_virtual_networks") or []:
            vpc_name = vpc_obj.get("name", "")
            if not vpc_name:
                continue
            attachment_id = f"{tgw_id}_{tf_id(vpc_name)}"
            attach_block = [f'resource "aws_transit_gateway_vpc_attachment" "{attachment_id}" {{']
            attach_block += [
                f"  transit_gateway_id = aws_transit_gateway.{tgw_id}.id",
                f"  vpc_id             = aws_vpc.{tf_id(vpc_name)}.id",
                "  subnet_ids         = []",
                f'  tags               = {{ Name = "{tgw_name}-{vpc_name}", ManagedBy = "Infrahub" }}',
            ]
            if provider_attr:
                attach_block.append(f"  provider           = {provider_attr}")
            attach_block += ["}", ""]
            lines += attach_block

    for cgw in ctx["customer_gateways"]:
        cgw_name = cgw.get("name", "")
        cgw_id = tf_id(cgw_name)
        asn_val = (cgw.get("asn") or {}).get("asn", 65000) or 65000
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += _res(
            "aws_customer_gateway",
            cgw_id,
            [
                f"bgp_asn    = {asn_val}",
                f'ip_address = "{ip_val}"',
                'type       = "ipsec.1"',
                f'tags       = {{ Name = "{cgw_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    for peering in ctx["peerings"]:
        peering_name = peering.get("name", "")
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        peering_id = tf_id(f"{req_vpc}_{acc_vpc}")
        lines += _res(
            "aws_vpc_peering_connection",
            peering_id,
            [
                f"vpc_id      = aws_vpc.{tf_id(req_vpc)}.id",
                f"peer_vpc_id = aws_vpc.{tf_id(acc_vpc)}.id",
                "auto_accept = true",
                f'tags        = {{ Name = "{peering_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    for dc in ctx["direct_connects"]:
        # A hosted connection is created by the Direct Connect partner on the customer's behalf, so
        # `aws_dx_connection` (which orders a dedicated port) cannot provision it. Terraform can only
        # accept it through `aws_dx_hosted_*` resources, which this transform does not emit.
        if dc.get("connection_type") == "hosted":
            continue
        dc_name = dc.get("name", "")
        dc_id = tf_id(dc_name)
        lines += _res(
            "aws_dx_connection",
            dc_id,
            [
                f'name      = "{dc_name}"',
                f'bandwidth = "{dc.get("bandwidth", "")}"',
                'location  = ""',
                f'tags      = {{ Name = "{dc_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    return lines
