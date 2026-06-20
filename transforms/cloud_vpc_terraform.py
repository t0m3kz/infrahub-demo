"""Transform: CloudAccount → native HCL Terraform configuration (all VPCs in account)."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.helpers.cloud import prepare_cloud_data
from utils.data_cleaning import clean_data


def _tf_id(name: str) -> str:
    return name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")


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
        alias = _tf_id(region)
        lines += [
            'provider "aws" {',
            f'  alias  = "{alias}"',
            f'  region = "{region}"',
            "}",
            "",
        ]
    return lines


def _provider_block_azure(account_name: str) -> list[str]:
    return [
        "terraform {",
        "  required_providers {",
        "    azurerm = {",
        '      source  = "hashicorp/azurerm"',
        '      version = "~> 3.0"',
        "    }",
        "  }",
        "}",
        "",
        'provider "azurerm" {',
        "  features {}",
        "}",
        "",
        'variable "resource_group_name" {',
        f'  default = "{account_name}-rg"',
        "}",
        "",
    ]


def _provider_block_gcp(project: str, region: str) -> list[str]:
    return [
        "terraform {",
        "  required_providers {",
        "    google = {",
        '      source  = "hashicorp/google"',
        '      version = "~> 5.0"',
        "    }",
        "  }",
        "}",
        "",
        'provider "google" {',
        f'  project = "{project}"',
        f'  region  = "{region}"',
        "}",
        "",
    ]


def _vpc_blocks_aws(
    vpc: dict,
    sgs_by_vpc: dict,
    instances_by_vpc: dict,
    igws_by_vpc: dict,
    nats_by_vpc: dict,
    rts_by_vpc: dict,
    nacls_by_vpc: dict,
    vpngws_by_vpc: dict,
    asgs_by_vpc: dict,
    routes: list,
    public_ips: list,
    tgws: list,
    cgws: list,
    peerings: list,
    direct_connects: list,
    provider_attr: str = "",
) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_id = _tf_id(vpc_name)
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
        seg_id = _tf_id(seg_name)
        cidr = (seg.get("cidr_block") or {}).get("prefix", "")
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

    for sg in sgs_by_vpc.get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_id = _tf_id(sg_name)
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

    for inst in instances_by_vpc.get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_id = _tf_id(inst_name)
        seg_ref = inst.get("network_segment") or {}
        subnet_ref = f"aws_subnet.{_tf_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        sg_ids = ", ".join(
            f"aws_security_group.{_tf_id(sg.get('name', ''))}.id"
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

    # Internet Gateways
    for igw in igws_by_vpc.get(vpc_name) or []:
        igw_name = igw.get("name", "")
        igw_id = _tf_id(igw_name)
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
    for nat in nats_by_vpc.get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_id = _tf_id(nat_name)
        nat_segs = nat.get("network_segments") or []
        first_seg_name = nat_segs[0].get("name", "") if nat_segs else ""
        subnet_ref = f"aws_subnet.{_tf_id(first_seg_name)}.id" if first_seg_name else '""'

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

    # Route Tables and their subnet associations
    for rt in rts_by_vpc.get(vpc_name) or []:
        rt_name = rt.get("name", "")
        rt_id = _tf_id(rt_name)
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
        for route in routes:
            route_rt = (route.get("route_table") or {}).get("name", "")
            if route_rt != rt_name:
                continue
            route_name = route.get("name", "")
            route_id = (
                _tf_id(route_name)
                if route_name
                else f"{rt_id}_route_{_tf_id((route.get('destination') or {}).get('prefix', 'default'))}"
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
                route_attrs.append(f"gateway_id             = aws_internet_gateway.{_tf_id(igw_ref)}.id")
            elif nat_ref:
                route_attrs.append(f"nat_gateway_id         = aws_nat_gateway.{_tf_id(nat_ref)}.id")
            elif inst_ref:
                route_attrs.append(f"instance_id            = aws_instance.{_tf_id(inst_ref)}.id")
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
            seg_id = _tf_id(seg_name)
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

    # Network ACLs
    for nacl in nacls_by_vpc.get(vpc_name) or []:
        nacl_name = nacl.get("name", "")
        nacl_id = _tf_id(nacl_name)
        nacl_segs = nacl.get("network_segments") or []
        subnet_ids = ", ".join(f"aws_subnet.{_tf_id(s.get('name', ''))}.id" for s in nacl_segs if s.get("name"))
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

    # VPN Gateways (per-VPC)
    for vpngw in vpngws_by_vpc.get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_id = _tf_id(vpngw_name)
        lines += _res(
            "aws_vpn_gateway",
            vpngw_id,
            [
                f"vpc_id = aws_vpc.{vpc_id}.id",
                f'tags   = {{ Name = "{vpngw_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    # Auto Scaling Groups
    for asg in asgs_by_vpc.get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_id = _tf_id(asg_name)
        lt_id = f"{asg_id}_lt"
        subnet_refs = (
            ", ".join(f"aws_subnet.{_tf_id(s.get('name', ''))}.id" for s in (asg.get("network_segments") or []))
            or f"aws_subnet.{vpc_id}_subnet.id"
        )
        sg_refs = ", ".join(
            f"aws_security_group.{_tf_id(sg.get('name', ''))}.id" for sg in (asg.get("security_groups") or [])
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


def _account_blocks_aws(
    tgws: list,
    cgws: list,
    peerings: list,
    direct_connects: list,
    provider_attr: str = "",
) -> list[str]:
    """Emit account-scoped AWS resources that are not tied to a single VPC."""
    lines: list[str] = []

    for tgw in tgws:
        tgw_name = tgw.get("name", "")
        tgw_id = _tf_id(tgw_name)
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
            attachment_id = f"{tgw_id}_{_tf_id(vpc_name)}"
            attach_block = [f'resource "aws_transit_gateway_vpc_attachment" "{attachment_id}" {{']
            attach_block += [
                f"  transit_gateway_id = aws_transit_gateway.{tgw_id}.id",
                f"  vpc_id             = aws_vpc.{_tf_id(vpc_name)}.id",
                "  subnet_ids         = []",
                f'  tags               = {{ Name = "{tgw_name}-{vpc_name}", ManagedBy = "Infrahub" }}',
            ]
            if provider_attr:
                attach_block.append(f"  provider           = {provider_attr}")
            attach_block += ["}", ""]
            lines += attach_block

    for cgw in cgws:
        cgw_name = cgw.get("name", "")
        cgw_id = _tf_id(cgw_name)
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

    for peering in peerings:
        peering_name = peering.get("name", "")
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        peering_id = _tf_id(f"{req_vpc}_{acc_vpc}")
        lines += _res(
            "aws_vpc_peering_connection",
            peering_id,
            [
                f"vpc_id      = aws_vpc.{_tf_id(req_vpc)}.id",
                f"peer_vpc_id = aws_vpc.{_tf_id(acc_vpc)}.id",
                "auto_accept = true",
                f'tags        = {{ Name = "{peering_name}", ManagedBy = "Infrahub" }}',
            ],
            provider_attr,
        )

    for dc in direct_connects:
        dc_name = dc.get("name", "")
        dc_id = _tf_id(dc_name)
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


def _vpc_blocks_azure(
    vpc: dict,
    sgs_by_vpc: dict,
    instances_by_vpc: dict,
    igws_by_vpc: dict,
    nats_by_vpc: dict,
    rts_by_vpc: dict,
    nacls_by_vpc: dict,
    vpngws_by_vpc: dict,
    asgs_by_vpc: dict,
    routes: list,
    public_ips: list,
    rg_ref: str,
    location: str,
) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_id = _tf_id(vpc_name)
    cidr_blocks = vpc.get("cidr_blocks") or []
    address_spaces = [cb.get("prefix", "") for cb in cidr_blocks if cb.get("prefix")]
    address_space_str = ", ".join(f'"{c}"' for c in address_spaces)

    lines += [
        f'resource "azurerm_virtual_network" "{vpc_id}" {{',
        f'  name                = "{vpc_name}"',
        f'  location            = "{location}"',
        f"  resource_group_name = {rg_ref}",
        f"  address_space       = [{address_space_str}]",
        f'  tags = {{ Name = "{vpc_name}", ManagedBy = "Infrahub" }}',
        "}",
        "",
    ]

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_id = _tf_id(seg_name)
        cidr = (seg.get("cidr_block") or {}).get("prefix", "")
        lines += [
            f'resource "azurerm_subnet" "{seg_id}" {{',
            f'  name                 = "{seg_name}"',
            f"  resource_group_name  = {rg_ref}",
            f"  virtual_network_name = azurerm_virtual_network.{vpc_id}.name",
            f'  address_prefixes     = ["{cidr}"]',
            "}",
            "",
        ]

    for sg in sgs_by_vpc.get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_id = _tf_id(sg_name)
        lines += [
            f'resource "azurerm_network_security_group" "{sg_id}" {{',
            f'  name                = "{sg_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f'  tags = {{ Name = "{sg_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]

    for inst in instances_by_vpc.get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_id = _tf_id(inst_name)
        os_type = (inst.get("os_type") or "").lower()
        resource_type = "azurerm_windows_virtual_machine" if os_type == "windows" else "azurerm_linux_virtual_machine"
        seg_ref = inst.get("network_segment") or {}
        subnet_id_ref = f"azurerm_subnet.{_tf_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        lines += [
            f'resource "{resource_type}" "{inst_id}" {{',
            f'  name                  = "{inst_name}"',
            f'  location              = "{location}"',
            f"  resource_group_name   = {rg_ref}",
            f'  size                  = "{inst.get("instance_type", "")}"',
            f"  network_interface_ids = [{subnet_id_ref}]",
            "  os_disk {",
            f"    disk_size_gb         = {inst.get('root_volume_size_gb') or 30}",
            '    caching              = "ReadWrite"',
            '    storage_account_type = "Standard_LRS"',
            "  }",
            "  source_image_reference {",
            f'    id = "{inst.get("image", "")}"',
            "  }",
            f'  tags = {{ Name = "{inst_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]

    # Public IPs (account-scoped but emitted per-VPC context not needed; emit all here)
    for pip in public_ips:
        pip_name = pip.get("name", "")
        pip_id = _tf_id(pip_name)
        lines += [
            f'resource "azurerm_public_ip" "{pip_id}" {{',
            f'  name                = "{pip_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            '  allocation_method   = "Static"',
            f'  tags                = {{ Name = "{pip_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]

    # NAT Gateways
    for nat in nats_by_vpc.get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_id = _tf_id(nat_name)
        nat_segs = nat.get("network_segments") or []
        lines += [
            f'resource "azurerm_nat_gateway" "{nat_id}" {{',
            f'  name                = "{nat_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f'  tags                = {{ Name = "{nat_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]
        if nat.get("public_ip"):
            # Associate with a pre-existing public IP resource named after the NAT
            pip_ref_id = _tf_id(f"{nat_name}_pip")
            lines += [
                f'resource "azurerm_nat_gateway_public_ip_association" "{nat_id}_pip_assoc" {{',
                f"  nat_gateway_id       = azurerm_nat_gateway.{nat_id}.id",
                f"  public_ip_address_id = azurerm_public_ip.{pip_ref_id}.id",
                "}",
                "",
            ]
        for seg in nat_segs:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            seg_id = _tf_id(seg_name)
            lines += [
                f'resource "azurerm_subnet_nat_gateway_association" "{seg_id}_nat_assoc" {{',
                f"  subnet_id      = azurerm_subnet.{seg_id}.id",
                f"  nat_gateway_id = azurerm_nat_gateway.{nat_id}.id",
                "}",
                "",
            ]

    # Route Tables and routes
    for rt in rts_by_vpc.get(vpc_name) or []:
        rt_name = rt.get("name", "")
        rt_id = _tf_id(rt_name)
        lines += [
            f'resource "azurerm_route_table" "{rt_id}" {{',
            f'  name                = "{rt_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f'  tags                = {{ Name = "{rt_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]
        for route in routes:
            route_rt = (route.get("route_table") or {}).get("name", "")
            if route_rt != rt_name:
                continue
            route_name = route.get("name", "")
            dest = (route.get("destination") or {}).get("prefix", "")
            igw_ref = (route.get("internet_gateway") or {}).get("name", "")
            nat_ref = (route.get("nat_gateway") or {}).get("name", "")
            route_res_id = _tf_id(route_name) if route_name else f"{rt_id}_route_{_tf_id(dest)}"
            next_hop_type = "Internet" if igw_ref else ("VirtualNetworkGateway" if nat_ref else "None")
            lines += [
                f'resource "azurerm_route" "{route_res_id}" {{',
                f'  name                = "{route_name or dest}"',
                f"  resource_group_name = {rg_ref}",
                f"  route_table_name    = azurerm_route_table.{rt_id}.name",
                f'  address_prefix      = "{dest}"',
                f'  next_hop_type       = "{next_hop_type}"',
                "}",
                "",
            ]
        for seg in rt.get("network_segments") or []:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            seg_id = _tf_id(seg_name)
            lines += [
                f'resource "azurerm_subnet_route_table_association" "{seg_id}_rt_assoc" {{',
                f"  subnet_id      = azurerm_subnet.{seg_id}.id",
                f"  route_table_id = azurerm_route_table.{rt_id}.id",
                "}",
                "",
            ]

    # Network ACLs → subnet NSG associations in Azure
    for nacl in nacls_by_vpc.get(vpc_name) or []:
        nacl_name = nacl.get("name", "")
        nacl_id = _tf_id(nacl_name)
        for seg in nacl.get("network_segments") or []:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            seg_id = _tf_id(seg_name)
            lines += [
                f'resource "azurerm_subnet_network_security_group_association" "{seg_id}_nacl_{nacl_id}" {{',
                f"  subnet_id                 = azurerm_subnet.{seg_id}.id",
                f"  network_security_group_id = azurerm_network_security_group.{nacl_id}.id",
                "}",
                "",
            ]

    # VPN Gateways
    for vpngw in vpngws_by_vpc.get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_id = _tf_id(vpngw_name)
        lines += [
            f'resource "azurerm_virtual_network_gateway" "{vpngw_id}" {{',
            f'  name                = "{vpngw_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            '  type                = "Vpn"',
            '  vpn_type            = "RouteBased"',
            "  ip_configuration {",
            '    subnet_id            = ""',
            '    public_ip_address_id = ""',
            "  }",
            f'  tags = {{ Name = "{vpngw_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]

    # Auto Scaling Groups → Azure VMSS
    for asg in asgs_by_vpc.get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_id = _tf_id(asg_name)
        subnet_id = (
            f"azurerm_subnet.{_tf_id((asg.get('network_segments') or [{}])[0].get('name', ''))}.id"
            if asg.get("network_segments")
            else '""'
        )
        lines += [
            f'resource "azurerm_linux_virtual_machine_scale_set" "{asg_id}" {{',
            f'  name                = "{asg_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f"  instances           = {asg.get('desired_capacity', 1)}",
            f'  sku                 = "{asg.get("instance_type", "")}"',
            '  admin_username      = "adminuser"',
            "  source_image_reference {",
            f'    id = "{asg.get("image", "")}"',
            "  }",
            "  os_disk {",
            '    caching              = "ReadWrite"',
            '    storage_account_type = "Standard_LRS"',
            "  }",
            "  network_interface {",
            f'    name    = "{asg_id}-nic"',
            "    primary = true",
            "    ip_configuration {",
            f'      name      = "{asg_id}-ipconfig"',
            "      primary   = true",
            f"      subnet_id = {subnet_id}",
            "    }",
            "  }",
            '  tags = { ManagedBy = "Infrahub" }',
            "}",
            "",
        ]

    return lines


def _account_blocks_azure(
    tgws: list,
    cgws: list,
    peerings: list,
    direct_connects: list,
    rg_ref: str,
    location: str,
) -> list[str]:
    lines: list[str] = []

    for cgw in cgws:
        cgw_name = cgw.get("name", "")
        cgw_id = _tf_id(cgw_name)
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += [
            f'resource "azurerm_local_network_gateway" "{cgw_id}" {{',
            f'  name                = "{cgw_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f'  gateway_address     = "{ip_val}"',
            f'  tags                = {{ Name = "{cgw_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]

    for peering in peerings:
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        req_id = _tf_id(f"{req_vpc}_to_{acc_vpc}")
        acc_id = _tf_id(f"{acc_vpc}_to_{req_vpc}")
        lines += [
            f'resource "azurerm_virtual_network_peering" "{req_id}" {{',
            f'  name                      = "{req_vpc}-to-{acc_vpc}"',
            f"  resource_group_name       = {rg_ref}",
            f"  virtual_network_name      = azurerm_virtual_network.{_tf_id(req_vpc)}.name",
            f"  remote_virtual_network_id = azurerm_virtual_network.{_tf_id(acc_vpc)}.id",
            "}",
            "",
            f'resource "azurerm_virtual_network_peering" "{acc_id}" {{',
            f'  name                      = "{acc_vpc}-to-{req_vpc}"',
            f"  resource_group_name       = {rg_ref}",
            f"  virtual_network_name      = azurerm_virtual_network.{_tf_id(acc_vpc)}.name",
            f"  remote_virtual_network_id = azurerm_virtual_network.{_tf_id(req_vpc)}.id",
            "}",
            "",
        ]

    for dc in direct_connects:
        dc_name = dc.get("name", "")
        dc_id = _tf_id(dc_name)
        lines += [
            f'resource "azurerm_express_route_circuit" "{dc_id}" {{',
            f'  name                = "{dc_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            "  service_provider_properties {",
            '    service_provider_name = ""',
            '    peering_location      = ""',
            "    bandwidth_in_mbps     = 0",
            "  }",
            "  sku {",
            '    tier   = "Standard"',
            '    family = "MeteredData"',
            "  }",
            f'  tags = {{ Name = "{dc_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]

    return lines


def _vpc_blocks_gcp(
    vpc: dict,
    sgs_by_vpc: dict,
    instances_by_vpc: dict,
    nats_by_vpc: dict,
    rts_by_vpc: dict,
    vpngws_by_vpc: dict,
    asgs_by_vpc: dict,
    routes: list,
    public_ips: list,
    region: str,
) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_id = _tf_id(vpc_name)

    lines += [
        f'resource "google_compute_network" "{vpc_id}" {{',
        f'  name                    = "{vpc_name}"',
        "  auto_create_subnetworks = false",
        "}",
        "",
    ]

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_id = _tf_id(seg_name)
        cidr = (seg.get("cidr_block") or {}).get("prefix", "")
        az_name = (seg.get("availability_zone") or {}).get("name", "")
        seg_region = "-".join(az_name.split("-")[:-1]) if az_name else region
        lines += [
            f'resource "google_compute_subnetwork" "{seg_id}" {{',
            f'  name          = "{seg_name}"',
            f'  ip_cidr_range = "{cidr}"',
            f'  region        = "{seg_region}"',
            f"  network       = google_compute_network.{vpc_id}.id",
            "}",
            "",
        ]

    for sg in sgs_by_vpc.get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_id = _tf_id(sg_name)
        lines += [
            f'resource "google_compute_firewall" "{sg_id}" {{',
            f'  name    = "{sg_name}"',
            f"  network = google_compute_network.{vpc_id}.name",
            "  allow {",
            '    protocol = "tcp"',
            "  }",
            "}",
            "",
        ]

    for inst in instances_by_vpc.get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_id = _tf_id(inst_name)
        az_name = (inst.get("availability_zone") or {}).get("name", "")
        seg_ref = inst.get("network_segment") or {}
        subnetwork_ref = (
            f"google_compute_subnetwork.{_tf_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        )
        lines += [
            f'resource "google_compute_instance" "{inst_id}" {{',
            f'  name         = "{inst_name}"',
            f'  machine_type = "{inst.get("instance_type", "")}"',
            f'  zone         = "{az_name}"',
            "  boot_disk {",
            "    initialize_params {",
            f'      image = "{inst.get("image", "")}"',
            "    }",
            "  }",
            "  network_interface {",
            f"    subnetwork = {subnetwork_ref}",
            "  }",
            f'  labels = {{ name = "{_tf_id(inst_name)}", managed_by = "infrahub" }}',
            "}",
            "",
        ]

    # Public IPs (GCP compute addresses)
    for pip in public_ips:
        pip_name = pip.get("name", "")
        pip_id = _tf_id(pip_name)
        lines += [
            f'resource "google_compute_address" "{pip_id}" {{',
            f'  name   = "{pip_name}"',
            f'  region = "{region}"',
            "}",
            "",
        ]

    # Cloud Router + NAT
    for nat in nats_by_vpc.get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_id = _tf_id(nat_name)
        router_id = f"router_{nat_id}"
        lines += [
            f'resource "google_compute_router" "{router_id}" {{',
            f'  name    = "{nat_name}-router"',
            f'  region  = "{region}"',
            f"  network = google_compute_network.{vpc_id}.id",
            "}",
            "",
            f'resource "google_compute_router_nat" "{nat_id}" {{',
            f'  name                               = "{nat_name}"',
            f"  router                             = google_compute_router.{router_id}.name",
            f'  region                             = "{region}"',
            '  nat_ip_allocate_option             = "AUTO_ONLY"',
            '  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"',
            "}",
            "",
        ]

    # Routes
    for route in routes:
        route_rt = (route.get("route_table") or {}).get("name", "")
        # For GCP, filter routes belonging to this VPC via route table grouping
        rt_obj = next((rt for rt in (rts_by_vpc.get(vpc_name) or []) if rt.get("name") == route_rt), None)
        if rt_obj is None:
            continue
        route_name = route.get("name", "")
        dest = (route.get("destination") or {}).get("prefix", "")
        igw_ref = (route.get("internet_gateway") or {}).get("name", "")
        route_id = _tf_id(route_name) if route_name else f"route_{vpc_id}_{_tf_id(dest)}"
        lines += [
            f'resource "google_compute_route" "{route_id}" {{',
            f'  name             = "{route_name or dest}"',
            f"  network          = google_compute_network.{vpc_id}.id",
            f'  dest_range       = "{dest}"',
            "  priority         = 1000",
        ]
        if igw_ref:
            lines.append('  next_hop_gateway = "default-internet-gateway"')
        lines += ["}", ""]

    # VPN Gateways
    for vpngw in vpngws_by_vpc.get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_id = _tf_id(vpngw_name)
        lines += [
            f'resource "google_compute_ha_vpn_gateway" "{vpngw_id}" {{',
            f'  name    = "{vpngw_name}"',
            f"  network = google_compute_network.{vpc_id}.id",
            f'  region  = "{region}"',
            "}",
            "",
        ]

    # Auto Scaling Groups → GCP instance group manager + autoscaler
    for asg in asgs_by_vpc.get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_id = _tf_id(asg_name)
        it_id = f"{asg_id}_template"
        igm_id = f"{asg_id}_igm"
        lines += [
            f'resource "google_compute_instance_template" "{it_id}" {{',
            f'  name         = "{asg_name}-template"',
            f'  machine_type = "{asg.get("instance_type", "")}"',
            f'  region       = "{region}"',
            "  disk {",
            f'    source_image = "{asg.get("image", "")}"',
            "    auto_delete  = true",
            "    boot         = true",
            "  }",
            "  network_interface {",
            f"    network = google_compute_network.{vpc_id}.id",
            "  }",
            "}",
            "",
            f'resource "google_compute_region_instance_group_manager" "{igm_id}" {{',
            f'  name               = "{asg_name}"',
            f'  region             = "{region}"',
            f'  base_instance_name = "{asg_name}"',
            f"  target_size        = {asg.get('desired_capacity', 1)}",
            "  version {",
            f"    instance_template = google_compute_instance_template.{it_id}.id",
            "  }",
            "}",
            "",
            f'resource "google_compute_region_autoscaler" "{asg_id}" {{',
            f'  name   = "{asg_name}-autoscaler"',
            f'  region = "{region}"',
            f"  target = google_compute_region_instance_group_manager.{igm_id}.id",
            "  autoscaling_policy {",
            f"    min_replicas = {asg.get('min_size', 1)}",
            f"    max_replicas = {asg.get('max_size', 1)}",
            "    cooldown_period = 60",
            "  }",
            "}",
            "",
        ]

    return lines


def _account_blocks_gcp(
    tgws: list,
    cgws: list,
    peerings: list,
    direct_connects: list,
    vifs: list,
    region: str,
) -> list[str]:
    lines: list[str] = []

    for cgw in cgws:
        cgw_name = cgw.get("name", "")
        cgw_id = _tf_id(cgw_name)
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += [
            f'resource "google_compute_external_vpn_gateway" "{cgw_id}" {{',
            f'  name            = "{cgw_name}"',
            '  redundancy_type = "SINGLE_IP_INTERNALLY_REDUNDANT"',
            "  interface {",
            "    id         = 0",
            f'    ip_address = "{ip_val}"',
            "  }",
            "}",
            "",
        ]

    for peering in peerings:
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        req_id = _tf_id(f"{req_vpc}_to_{acc_vpc}")
        acc_id = _tf_id(f"{acc_vpc}_to_{req_vpc}")
        lines += [
            f'resource "google_compute_network_peering" "{req_id}" {{',
            f'  name         = "{req_vpc}-to-{acc_vpc}"',
            f"  network      = google_compute_network.{_tf_id(req_vpc)}.id",
            f"  peer_network = google_compute_network.{_tf_id(acc_vpc)}.id",
            "}",
            "",
            f'resource "google_compute_network_peering" "{acc_id}" {{',
            f'  name         = "{acc_vpc}-to-{req_vpc}"',
            f"  network      = google_compute_network.{_tf_id(acc_vpc)}.id",
            f"  peer_network = google_compute_network.{_tf_id(req_vpc)}.id",
            "}",
            "",
        ]

    for vif in vifs:
        vif_name = vif.get("name", "")
        vif_id = _tf_id(vif_name)
        lines += [
            f'resource "google_compute_interconnect_attachment" "{vif_id}" {{',
            f'  name         = "{vif_name}"',
            '  router       = ""',
            f'  region       = "{region}"',
            '  interconnect = ""',
            f"  vlan_tag8021q = {vif.get('vlan_id') or 0}",
            "}",
            "",
        ]

    return lines


class CloudVpcTerraform(InfrahubTransform):
    """Generate native HCL Terraform for all VPCs in a CloudAccount."""

    query = "cloud_vpc_config"

    async def transform(self, data: Any) -> str:
        cleaned = clean_data(data)
        ctx = prepare_cloud_data(cleaned)
        if not ctx["vpcs"]:
            raise ValueError("No CloudVirtualNetwork found for this account")

        vpcs = ctx["vpcs"]
        provider_name = ctx["provider_name"]
        account = ctx["account"]
        public_ips = ctx["public_ips"]
        transit_gateways = ctx["transit_gateways"]
        customer_gateways = ctx["customer_gateways"]
        direct_connects = ctx["direct_connects"]
        routes = ctx["routes"]
        peerings = ctx["peerings"]
        vifs = ctx["vifs"]
        sgs_by_vpc = ctx["sgs_by_vpc"]
        igws_by_vpc = ctx["igws_by_vpc"]
        nats_by_vpc = ctx["nats_by_vpc"]
        rts_by_vpc = ctx["rts_by_vpc"]
        nacls_by_vpc = ctx["nacls_by_vpc"]
        vpngws_by_vpc = ctx["vpngws_by_vpc"]
        inst_by_vpc = ctx["inst_by_vpc"]
        asgs_by_vpc = ctx["asgs_by_vpc"]

        lines: list[str] = []

        if provider_name == "aws":
            regions_seen = ctx["regions_seen"]
            vpc_region_map = ctx["vpc_region_map"]
            default_region = regions_seen[0] if regions_seen else ""
            lines += _provider_block_aws(regions_seen)

            for vpc in vpcs:
                region = vpc_region_map.get(vpc.get("name", ""), "")
                alias = _tf_id(region) if region else ""
                provider_attr = f"aws.{alias}" if region != default_region and alias else ""
                lines += _vpc_blocks_aws(
                    vpc,
                    sgs_by_vpc,
                    inst_by_vpc,
                    igws_by_vpc,
                    nats_by_vpc,
                    rts_by_vpc,
                    nacls_by_vpc,
                    vpngws_by_vpc,
                    asgs_by_vpc,
                    routes,
                    public_ips,
                    transit_gateways,
                    customer_gateways,
                    peerings,
                    direct_connects,
                    provider_attr=provider_attr,
                )
            lines += _account_blocks_aws(
                transit_gateways, customer_gateways, peerings, direct_connects, provider_attr=""
            )  # TGW is account-wide; use default

        elif provider_name == "azure":
            account_name = account.get("name", "default")
            region_obj = vpcs[0].get("region") or {}
            location = region_obj.get("name", "eastus") if region_obj else "eastus"
            rg_ref = "var.resource_group_name"
            lines += _provider_block_azure(account_name)
            for vpc in vpcs:
                lines += _vpc_blocks_azure(
                    vpc,
                    sgs_by_vpc,
                    inst_by_vpc,
                    igws_by_vpc,
                    nats_by_vpc,
                    rts_by_vpc,
                    nacls_by_vpc,
                    vpngws_by_vpc,
                    asgs_by_vpc,
                    routes,
                    public_ips,
                    rg_ref,
                    location,
                )
            lines += _account_blocks_azure(
                transit_gateways, customer_gateways, peerings, direct_connects, rg_ref, location
            )

        else:  # gcp
            project = account.get("account_id", "")
            region_obj = vpcs[0].get("region") or {}
            region = region_obj.get("name", "") if region_obj else ""
            lines += _provider_block_gcp(project, region)
            for vpc in vpcs:
                lines += _vpc_blocks_gcp(
                    vpc,
                    sgs_by_vpc,
                    inst_by_vpc,
                    nats_by_vpc,
                    rts_by_vpc,
                    vpngws_by_vpc,
                    asgs_by_vpc,
                    routes,
                    public_ips,
                    region,
                )
            lines += _account_blocks_gcp(transit_gateways, customer_gateways, peerings, direct_connects, vifs, region)

        return "\n".join(lines)
