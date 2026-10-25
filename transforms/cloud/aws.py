"""AWS HCL rendering for the cloud VPC Terraform transform."""

from typing import Any

from transforms.cloud.terraform_common import tf_id


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
            attrs.append(f'cidr_ipv4 = "{rule["source_cidr"] if direction == "ingress" else rule["dest_cidr"]}"')
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


def _account_blocks_aws(ctx: dict) -> list[str]:
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
            attach_block += ["}", ""]
            lines += attach_block

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
        )

    return lines
