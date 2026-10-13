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
    lines += _public_ip_blocks_azure(ctx, rg_ref, location)
    lines += _nat_gateway_blocks_azure(vpc_name, ctx, rg_ref, location)
    lines += _route_table_blocks_azure(vpc_name, ctx, rg_ref, location)
    lines += _network_acl_blocks_azure(vpc_name, ctx)
    lines += _vpn_gateway_blocks_azure(vpc_name, ctx, rg_ref, location)
    lines += _scale_set_blocks_azure(vpc_name, ctx, rg_ref, location)

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
        for i, rule in enumerate(sg.get("rules") or []):
            if rule.get("disabled"):
                continue
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
            priority = rule.get("priority") or (100 + i * 10)
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
        if sg.get("policy_derived") and sg.get("segment"):
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

    return lines


def _public_ip_blocks_azure(ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    # Public IPs (account-scoped but emitted per-VPC context not needed; emit all here)
    for pip in ctx["public_ips"]:
        pip_name = pip.get("name", "")
        pip_id = tf_id(pip_name)
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

    return lines


def _nat_gateway_blocks_azure(vpc_name: str, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    # NAT Gateways
    for nat in ctx["nats_by_vpc"].get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_id = tf_id(nat_name)
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
            pip_ref_id = tf_id(f"{nat_name}_pip")
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
            seg_id = tf_id(seg_name)
            lines += [
                f'resource "azurerm_subnet_nat_gateway_association" "{seg_id}_nat_assoc" {{',
                f"  subnet_id      = azurerm_subnet.{seg_id}.id",
                f"  nat_gateway_id = azurerm_nat_gateway.{nat_id}.id",
                "}",
                "",
            ]

    return lines


def _route_table_blocks_azure(vpc_name: str, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    # Route Tables and routes
    for rt in ctx["rts_by_vpc"].get(vpc_name) or []:
        rt_name = rt.get("name", "")
        rt_id = tf_id(rt_name)
        lines += [
            f'resource "azurerm_route_table" "{rt_id}" {{',
            f'  name                = "{rt_name}"',
            f'  location            = "{location}"',
            f"  resource_group_name = {rg_ref}",
            f'  tags                = {{ Name = "{rt_name}", ManagedBy = "Infrahub" }}',
            "}",
            "",
        ]
        for route in ctx["routes"]:
            route_rt = (route.get("route_table") or {}).get("name", "")
            if route_rt != rt_name:
                continue
            route_name = route.get("name", "")
            dest = (route.get("destination") or {}).get("prefix", "")
            igw_ref = (route.get("internet_gateway") or {}).get("name", "")
            nat_ref = (route.get("nat_gateway") or {}).get("name", "")
            route_res_id = tf_id(route_name) if route_name else f"{rt_id}_route_{tf_id(dest)}"
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
            seg_id = tf_id(seg_name)
            lines += [
                f'resource "azurerm_subnet_route_table_association" "{seg_id}_rt_assoc" {{',
                f"  subnet_id      = azurerm_subnet.{seg_id}.id",
                f"  route_table_id = azurerm_route_table.{rt_id}.id",
                "}",
                "",
            ]

    return lines


def _network_acl_blocks_azure(vpc_name: str, ctx: dict) -> list[str]:
    lines: list[str] = []
    # Network ACLs → subnet NSG associations in Azure
    for nacl in ctx["nacls_by_vpc"].get(vpc_name) or []:
        nacl_name = nacl.get("name", "")
        nacl_id = tf_id(nacl_name)
        for seg in nacl.get("network_segments") or []:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            seg_id = tf_id(seg_name)
            lines += [
                f'resource "azurerm_subnet_network_security_group_association" "{seg_id}_nacl_{nacl_id}" {{',
                f"  subnet_id                 = azurerm_subnet.{seg_id}.id",
                f"  network_security_group_id = azurerm_network_security_group.{nacl_id}.id",
                "}",
                "",
            ]

    return lines


def _vpn_gateway_blocks_azure(vpc_name: str, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    # VPN Gateways
    for vpngw in ctx["vpngws_by_vpc"].get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_id = tf_id(vpngw_name)
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

    return lines


def _scale_set_blocks_azure(vpc_name: str, ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []
    # Auto Scaling Groups → Azure VMSS
    for asg in ctx["asgs_by_vpc"].get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_id = tf_id(asg_name)
        subnet_id = (
            f"azurerm_subnet.{tf_id((asg.get('network_segments') or [{}])[0].get('name', ''))}.id"
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


def _account_blocks_azure(ctx: dict, rg_ref: str, location: str) -> list[str]:
    lines: list[str] = []

    for cgw in ctx["customer_gateways"]:
        cgw_name = cgw.get("name", "")
        cgw_id = tf_id(cgw_name)
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

    for peering in ctx["peerings"]:
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        req_id = tf_id(f"{req_vpc}_to_{acc_vpc}")
        acc_id = tf_id(f"{acc_vpc}_to_{req_vpc}")
        lines += [
            f'resource "azurerm_virtual_network_peering" "{req_id}" {{',
            f'  name                      = "{req_vpc}-to-{acc_vpc}"',
            f"  resource_group_name       = {rg_ref}",
            f"  virtual_network_name      = azurerm_virtual_network.{tf_id(req_vpc)}.name",
            f"  remote_virtual_network_id = azurerm_virtual_network.{tf_id(acc_vpc)}.id",
            "}",
            "",
            f'resource "azurerm_virtual_network_peering" "{acc_id}" {{',
            f'  name                      = "{acc_vpc}-to-{req_vpc}"',
            f"  resource_group_name       = {rg_ref}",
            f"  virtual_network_name      = azurerm_virtual_network.{tf_id(acc_vpc)}.name",
            f"  remote_virtual_network_id = azurerm_virtual_network.{tf_id(req_vpc)}.id",
            "}",
            "",
        ]

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
