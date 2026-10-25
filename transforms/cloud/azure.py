"""Azure HCL rendering for the cloud VPC Terraform transform."""

from transforms.cloud.terraform_common import tf_id


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


def _vpc_blocks_azure(vpc: dict, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_id = tf_id(vpc_name)

    lines += _network_blocks_azure(vpc, vpc_name, vpc_id, rg_ref, location)
    lines += _security_group_blocks_azure(vpc_name, ctx, rg_ref, location)
    lines += _instance_blocks_azure(vpc_name, ctx, rg_ref, location)

    return lines


def _network_blocks_azure(vpc: dict, vpc_name: str, vpc_id: str, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
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
        seg_id = tf_id(seg_name)
        cidr = seg.get("cidr_block") or ""
        lines += [
            f'resource "azurerm_subnet" "{seg_id}" {{',
            f'  name                 = "{seg_name}"',
            f"  resource_group_name  = {rg_ref}",
            f"  virtual_network_name = azurerm_virtual_network.{vpc_id}.name",
            f'  address_prefixes     = ["{cidr}"]',
            "}",
            "",
        ]

    return lines


def _security_group_blocks_azure(vpc_name: str, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    for sg in ctx["sgs_by_vpc"].get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_id = tf_id(sg_name)
        lines += [
            f'resource "azurerm_network_security_group" "{sg_id}" {{',
            f'  name                = "{sg_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f'  tags = {{ Name = "{sg_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]
        for rule in sg.get("rules") or []:
            rule_name = rule.get("name", "")
            rule_res_id = tf_id(f"{sg_name}_{rule_name}")
            direction = rule.get("direction", "ingress")
            az_dir = "Inbound" if direction == "ingress" else "Outbound"
            proto = rule.get("protocol", "tcp")
            az_proto = "*" if proto == "any" else proto.capitalize()
            p_start = rule.get("port_start")
            p_end = rule.get("port_end") or p_start
            port_range = (
                f"{p_start}-{p_end}" if p_start and p_end and p_end != p_start else (str(p_start) if p_start else "*")
            )
            src_prefix = rule.get("source_cidr", "*") if direction == "ingress" else "*"
            dst_prefix = rule.get("dest_cidr", "*") if direction == "egress" else "*"
            priority = rule["priority"]
            lines += [
                f'resource "azurerm_network_security_rule" "{rule_res_id}" {{',
                f'  name                        = "{rule_name}"',
                f"  resource_group_name         = {rg_ref}",
                f"  network_security_group_name = azurerm_network_security_group.{sg_id}.name",
                f"  priority                    = {priority}",
                f'  direction                   = "{az_dir}"',
                '  access                      = "Allow"',
                f'  protocol                    = "{az_proto}"',
                '  source_port_range           = "*"',
                f'  destination_port_range      = "{port_range}"',
                f'  source_address_prefix       = "{src_prefix}"',
                f'  destination_address_prefix  = "{dst_prefix}"',
                "}",
                "",
            ]
        # The group guards its segment: Azure binds an NSG to the subnet.
        seg_id = tf_id(sg["segment"])
        lines += [
            f'resource "azurerm_subnet_network_security_group_association" "{seg_id}_sg_{sg_id}" {{',
            f"  subnet_id                 = azurerm_subnet.{seg_id}.id",
            f"  network_security_group_id = azurerm_network_security_group.{sg_id}.id",
            "}",
            "",
        ]

    return lines


def _instance_blocks_azure(vpc_name: str, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    for inst in ctx["inst_by_vpc"].get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_id = tf_id(inst_name)
        os_type = (inst.get("os_type") or "").lower()
        resource_type = "azurerm_windows_virtual_machine" if os_type == "windows" else "azurerm_linux_virtual_machine"
        seg_ref = inst.get("network_segment") or {}
        subnet_id_ref = f"azurerm_subnet.{tf_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        lines += [
            f'resource "{resource_type}" "{inst_id}" {{',
            f'  name                  = "{inst_name}"',
            f'  location              = "{location}"',
            f"  resource_group_name   = {rg_ref}",
            f'  size                  = "{inst.get("instance_type", "")}"',
            f"  network_interface_ids = [{subnet_id_ref}]",
            "  os_disk {",
            "    disk_size_gb         = 30",
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

    return lines


def _account_blocks_azure(ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []

    for dc in ctx["direct_connects"]:
        dc_name = dc.get("name", "")
        dc_id = tf_id(dc_name)
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
