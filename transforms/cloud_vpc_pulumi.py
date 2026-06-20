"""Transform: CloudAccount → TypeScript Pulumi program (index.ts) for all VPCs."""

from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.helpers.cloud import prepare_cloud_data
from utils.data_cleaning import clean_data


def _ts_id(name: str) -> str:
    return name.lower().replace("-", "_").replace(" ", "_").replace(".", "_")


def _ts_bool(value: Any) -> str:
    return "true" if value else "false"


def _vpc_blocks_aws(
    vpc: dict,
    sgs_by_vpc: dict,
    inst_by_vpc: dict,
    igws_by_vpc: dict,
    nats_by_vpc: dict,
    rts_by_vpc: dict,
    nacls_by_vpc: dict,
    vpngws_by_vpc: dict,
    asgs_by_vpc: dict,
    routes: list,
    public_ips: list,
    provider_var: str = "",
) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_var = _ts_id(vpc_name)
    cidr_blocks = vpc.get("cidr_blocks") or []
    first_cidr = cidr_blocks[0].get("prefix", "") if cidr_blocks else ""

    opts = f", {{ provider: {provider_var} }}" if provider_var else ""
    lines += [
        f'const {vpc_var} = new aws.ec2.Vpc("{vpc_name}", {{',
        f'    cidrBlock: "{first_cidr}",',
        f"    enableDnsSupport: {_ts_bool(vpc.get('dns_support'))},",
        f"    enableDnsHostnames: {_ts_bool(vpc.get('dns_hostnames'))},",
        f'    tags: {{ Name: "{vpc_name}", ManagedBy: "Infrahub" }},',
        "}}" + opts + ");",
        "",
    ]

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_var = f"subnet_{_ts_id(seg_name)}"
        cidr = (seg.get("cidr_block") or {}).get("prefix", "")
        az = (seg.get("availability_zone") or {}).get("name", "")
        lines += [
            f'const {seg_var} = new aws.ec2.Subnet("{seg_name}", {{',
            f"    vpcId: {vpc_var}.id,",
            f'    cidrBlock: "{cidr}",',
            f'    availabilityZone: "{az}",',
            f"    mapPublicIpOnLaunch: {_ts_bool(seg.get('auto_assign_public_ip'))},",
            f'    tags: {{ Name: "{seg_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    for sg in sgs_by_vpc.get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_var = f"sg_{_ts_id(sg_name)}"
        lines += [
            f'const {sg_var} = new aws.ec2.SecurityGroup("{sg_name}", {{',
            f"    vpcId: {vpc_var}.id,",
            f'    tags: {{ Name: "{sg_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    for inst in inst_by_vpc.get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_var = f"instance_{_ts_id(inst_name)}"
        seg_ref = inst.get("network_segment") or {}
        subnet_ref = f"subnet_{_ts_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        sg_ids = ", ".join(
            f"sg_{_ts_id(sg.get('name', ''))}.id" for sg in (inst.get("security_groups") or []) if sg.get("name")
        )
        lines += [
            f'const {inst_var} = new aws.ec2.Instance("{inst_name}", {{',
            f'    ami: "{inst.get("image", "")}",',
            f'    instanceType: "{inst.get("instance_type", "")}",',
            f"    subnetId: {subnet_ref},",
            f"    vpcSecurityGroupIds: [{sg_ids}],",
            f'    tags: {{ Name: "{inst_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    # Internet Gateways
    for igw in igws_by_vpc.get(vpc_name) or []:
        igw_name = igw.get("name", "")
        igw_var = f"igw_{_ts_id(igw_name)}"
        lines += [
            f'const {igw_var} = new aws.ec2.InternetGateway("{igw_name}", {{',
            f"    vpcId: {vpc_var}.id,",
            f'    tags: {{ Name: "{igw_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    # NAT Gateways (with optional EIP)
    for nat in nats_by_vpc.get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_var = f"nat_{_ts_id(nat_name)}"
        nat_segs = nat.get("network_segments") or []
        first_seg_name = nat_segs[0].get("name", "") if nat_segs else ""
        subnet_ref = f"subnet_{_ts_id(first_seg_name)}.id" if first_seg_name else '""'

        if nat.get("public_ip"):
            eip_var = f"eip_{_ts_id(nat_name)}"
            lines += [
                f'const {eip_var} = new aws.ec2.Eip("{nat_name}-eip", {{',
                '    domain: "vpc",',
                f'    tags: {{ Name: "{nat_name}-eip", ManagedBy: "Infrahub" }},',
                "}}" + opts + ");",
                "",
                f'const {nat_var} = new aws.ec2.NatGateway("{nat_name}", {{',
                f"    allocationId: {eip_var}.id,",
                f"    subnetId: {subnet_ref},",
                f'    tags: {{ Name: "{nat_name}", ManagedBy: "Infrahub" }},',
                "}}" + opts + ");",
                "",
            ]
        else:
            lines += [
                f'const {nat_var} = new aws.ec2.NatGateway("{nat_name}", {{',
                f"    subnetId: {subnet_ref},",
                f'    tags: {{ Name: "{nat_name}", ManagedBy: "Infrahub" }},',
                "}}" + opts + ");",
                "",
            ]

    # Route Tables, routes and associations
    for rt in rts_by_vpc.get(vpc_name) or []:
        rt_name = rt.get("name", "")
        rt_var = f"rt_{_ts_id(rt_name)}"
        lines += [
            f'const {rt_var} = new aws.ec2.RouteTable("{rt_name}", {{',
            f"    vpcId: {vpc_var}.id,",
            f'    tags: {{ Name: "{rt_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
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
            inst_ref = (route.get("instance") or {}).get("name", "")
            route_var = f"route_{_ts_id(route_name)}" if route_name else f"route_{_ts_id(rt_name)}_{_ts_id(dest)}"
            route_args = [
                f'const {route_var} = new aws.ec2.Route("{route_name or dest}", {{',
                f"    routeTableId: {rt_var}.id,",
                f'    destinationCidrBlock: "{dest}",',
            ]
            if igw_ref:
                route_args.append(f"    gatewayId: igw_{_ts_id(igw_ref)}.id,")
            elif nat_ref:
                route_args.append(f"    natGatewayId: nat_{_ts_id(nat_ref)}.id,")
            elif inst_ref:
                route_args.append(f"    instanceId: instance_{_ts_id(inst_ref)}.id,")
            route_args += ["}}" + opts + ");", ""]
            lines += route_args

        for seg in rt.get("network_segments") or []:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            assoc_var = f"rta_{_ts_id(seg_name)}_{_ts_id(rt_name)}"
            lines += [
                f'const {assoc_var} = new aws.ec2.RouteTableAssociation("{seg_name}-{rt_name}-assoc", {{',
                f"    subnetId: subnet_{_ts_id(seg_name)}.id,",
                f"    routeTableId: {rt_var}.id,",
                "}}" + opts + ");",
                "",
            ]

    # Network ACLs
    for nacl in nacls_by_vpc.get(vpc_name) or []:
        nacl_name = nacl.get("name", "")
        nacl_var = f"nacl_{_ts_id(nacl_name)}"
        nacl_segs = nacl.get("network_segments") or []
        subnet_ids = ", ".join(f"subnet_{_ts_id(s.get('name', ''))}.id" for s in nacl_segs if s.get("name"))
        lines += [
            f'const {nacl_var} = new aws.ec2.NetworkAcl("{nacl_name}", {{',
            f"    vpcId: {vpc_var}.id,",
            f"    subnetIds: [{subnet_ids}],",
            f'    tags: {{ Name: "{nacl_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    # VPN Gateways
    for vpngw in vpngws_by_vpc.get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_var = f"vpngw_{_ts_id(vpngw_name)}"
        lines += [
            f'const {vpngw_var} = new aws.ec2.VpnGateway("{vpngw_name}", {{',
            f"    vpcId: {vpc_var}.id,",
            f'    tags: {{ Name: "{vpngw_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    # Auto Scaling Groups
    for asg in asgs_by_vpc.get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_var = _ts_id(asg_name)
        lt_var = f"{asg_var}Lt"
        subnet_refs = (
            ", ".join(f"subnet_{_ts_id(s.get('name', ''))}.id" for s in (asg.get("network_segments") or []))
            or f"{vpc_var}SubnetId"
        )
        sg_refs = ", ".join(f"sg_{_ts_id(sg.get('name', ''))}.id" for sg in (asg.get("security_groups") or []))
        lines += [
            f'const {lt_var} = new aws.ec2.LaunchTemplate("{asg_name}-lt", {{',
            f'    instanceType: "{asg.get("instance_type", "")}",',
            f'    imageId: "{asg.get("image", "")}",',
        ]
        if sg_refs:
            lines.append(f"    vpcSecurityGroupIds: [{sg_refs}],")
        lines += [
            f'    tags: {{ Name: "{asg_name}-lt", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
            f'const {asg_var} = new aws.autoscaling.Group("{asg_name}", {{',
            f"    minSize: {asg.get('min_size', 1)},",
            f"    maxSize: {asg.get('max_size', 1)},",
            f"    desiredCapacity: {asg.get('desired_capacity', 1)},",
            f"    vpcZoneIdentifiers: [{subnet_refs}],",
            "    launchTemplate: {",
            f"        id: {lt_var}.id,",
            '        version: "$Latest",',
            "    },",
            '    tags: [{ key: "ManagedBy", value: "Infrahub", propagateAtLaunch: true }],',
            "}}" + opts + ");",
            "",
        ]

    return lines


def _account_blocks_aws(
    tgws: list,
    cgws: list,
    peerings: list,
    direct_connects: list,
    public_ips: list,
    provider_var: str = "",
) -> list[str]:
    lines: list[str] = []
    opts = f", {{ provider: {provider_var} }}" if provider_var else ""

    for pip in public_ips:
        pip_name = pip.get("name", "")
        pip_var = f"eip_{_ts_id(pip_name)}"
        lines += [
            f'const {pip_var} = new aws.ec2.Eip("{pip_name}", {{',
            '    domain: "vpc",',
            f'    tags: {{ Name: "{pip_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    for tgw in tgws:
        tgw_name = tgw.get("name", "")
        tgw_var = f"tgw_{_ts_id(tgw_name)}"
        lines += [
            f'const {tgw_var} = new aws.ec2transitgateway.TransitGateway("{tgw_name}", {{',
            f'    defaultRouteTableAssociation: "{("enable" if tgw.get("default_route_table") else "disable")}",',
            f'    dnsSupport: "{("enable" if tgw.get("dns_support") else "disable")}",',
            f'    vpnEcmpSupport: "{("enable" if tgw.get("vpn_ecmp_support") else "disable")}",',
            f'    tags: {{ Name: "{tgw_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]
        for vpc_obj in tgw.get("attached_virtual_networks") or []:
            vpc_name = vpc_obj.get("name", "")
            if not vpc_name:
                continue
            attach_var = f"tgw_attach_{_ts_id(tgw_name)}_{_ts_id(vpc_name)}"
            lines += [
                f'const {attach_var} = new aws.ec2transitgateway.VpcAttachment("{tgw_name}-{vpc_name}-attach", {{',
                f"    transitGatewayId: {tgw_var}.id,",
                f"    vpcId: {_ts_id(vpc_name)}.id,",
                "    subnetIds: [],",
                f'    tags: {{ Name: "{tgw_name}-{vpc_name}", ManagedBy: "Infrahub" }},',
                "}}" + opts + ");",
                "",
            ]

    for cgw in cgws:
        cgw_name = cgw.get("name", "")
        cgw_var = f"cgw_{_ts_id(cgw_name)}"
        asn_val = (cgw.get("asn") or {}).get("asn", 65000) or 65000
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += [
            f'const {cgw_var} = new aws.ec2.CustomerGateway("{cgw_name}", {{',
            f'    bgpAsn: "{asn_val}",',
            f'    ipAddress: "{ip_val}",',
            '    type: "ipsec.1",',
            f'    tags: {{ Name: "{cgw_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    for peering in peerings:
        peering_name = peering.get("name", "")
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        peering_var = f"peering_{_ts_id(req_vpc)}_{_ts_id(acc_vpc)}"
        lines += [
            f'const {peering_var} = new aws.ec2.VpcPeeringConnection("{peering_name}", {{',
            f"    vpcId: {_ts_id(req_vpc)}.id,",
            f"    peerVpcId: {_ts_id(acc_vpc)}.id,",
            "    autoAccept: true,",
            f'    tags: {{ Name: "{peering_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    for dc in direct_connects:
        dc_name = dc.get("name", "")
        dc_var = f"dx_{_ts_id(dc_name)}"
        lines += [
            f'const {dc_var} = new aws.directconnect.Connection("{dc_name}", {{',
            f'    name: "{dc_name}",',
            f'    bandwidth: "{dc.get("bandwidth", "")}",',
            '    location: "",',
            f'    tags: {{ Name: "{dc_name}", ManagedBy: "Infrahub" }},',
            "}}" + opts + ");",
            "",
        ]

    return lines


def _vpc_blocks_azure(
    vpc: dict,
    sgs_by_vpc: dict,
    inst_by_vpc: dict,
    igws_by_vpc: dict,
    nats_by_vpc: dict,
    rts_by_vpc: dict,
    nacls_by_vpc: dict,
    vpngws_by_vpc: dict,
    asgs_by_vpc: dict,
    routes: list,
    public_ips: list,
    rg_name: str,
    location: str,
) -> list[str]:
    lines: list[str] = []
    vpc_name = vpc.get("name", "")
    vpc_var = f"vnet_{_ts_id(vpc_name)}"
    cidr_blocks = vpc.get("cidr_blocks") or []
    address_spaces = [cb.get("prefix", "") for cb in cidr_blocks if cb.get("prefix")]
    address_space_str = ", ".join(f'"{c}"' for c in address_spaces)

    lines += [
        f'const {vpc_var} = new azure_native.network.VirtualNetwork("{vpc_name}", {{',
        f'    virtualNetworkName: "{vpc_name}",',
        f'    location: "{location}",',
        f'    resourceGroupName: "{rg_name}",',
        f"    addressSpace: {{ addressPrefixes: [{address_space_str}] }},",
        f'    tags: {{ Name: "{vpc_name}", ManagedBy: "Infrahub" }},',
        "});",
        "",
    ]

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_var = f"subnet_{_ts_id(seg_name)}"
        cidr = (seg.get("cidr_block") or {}).get("prefix", "")
        lines += [
            f'const {seg_var} = new azure_native.network.Subnet("{seg_name}", {{',
            f'    subnetName: "{seg_name}",',
            f'    resourceGroupName: "{rg_name}",',
            f"    virtualNetworkName: {vpc_var}.name,",
            f'    addressPrefix: "{cidr}",',
            "});",
            "",
        ]

    for sg in sgs_by_vpc.get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_var = f"nsg_{_ts_id(sg_name)}"
        lines += [
            f'const {sg_var} = new azure_native.network.NetworkSecurityGroup("{sg_name}", {{',
            f'    networkSecurityGroupName: "{sg_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            f'    tags: {{ Name: "{sg_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]

    for inst in inst_by_vpc.get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_var = f"vm_{_ts_id(inst_name)}"
        lines += [
            f'const {inst_var} = new azure_native.compute.VirtualMachine("{inst_name}", {{',
            f'    vmName: "{inst_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            "    hardwareProfile: {",
            f'        vmSize: "{inst.get("instance_type", "")}",',
            "    },",
            "    storageProfile: {",
            "        imageReference: {",
            f'            id: "{inst.get("image", "")}",',
            "        },",
            "    },",
            f'    osProfile: {{ computerName: "{inst_name}", adminUsername: "azureuser" }},',
            f'    tags: {{ Name: "{inst_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]

    # Public IPs
    for pip in public_ips:
        pip_name = pip.get("name", "")
        pip_var = f"pip_{_ts_id(pip_name)}"
        lines += [
            f'const {pip_var} = new azure_native.network.PublicIPAddress("{pip_name}", {{',
            f'    publicIpAddressName: "{pip_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            '    publicIPAllocationMethod: "Static",',
            f'    tags: {{ Name: "{pip_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]

    # NAT Gateways
    for nat in nats_by_vpc.get(vpc_name) or []:
        nat_name = nat.get("name", "")
        nat_var = f"nat_{_ts_id(nat_name)}"
        nat_segs = nat.get("network_segments") or []
        lines += [
            f'const {nat_var} = new azure_native.network.NatGateway("{nat_name}", {{',
            f'    natGatewayName: "{nat_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            f'    tags: {{ Name: "{nat_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]
        for seg in nat_segs:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            assoc_var = f"nat_assoc_{_ts_id(seg_name)}_{_ts_id(nat_name)}"
            lines += [
                f'const {assoc_var} = new azure_native.network.SubnetNatGatewayAssociation("{seg_name}-{nat_name}-assoc", {{',
                f"    subnetId: subnet_{_ts_id(seg_name)}.id,",
                f"    natGatewayId: {nat_var}.id,",
                "});",
                "",
            ]

    # Route Tables and routes
    for rt in rts_by_vpc.get(vpc_name) or []:
        rt_name = rt.get("name", "")
        rt_var = f"rt_{_ts_id(rt_name)}"
        lines += [
            f'const {rt_var} = new azure_native.network.RouteTable("{rt_name}", {{',
            f'    routeTableName: "{rt_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            f'    tags: {{ Name: "{rt_name}", ManagedBy: "Infrahub" }},',
            "});",
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
            next_hop = "Internet" if igw_ref else ("VirtualNetworkGateway" if nat_ref else "None")
            route_var = f"route_{_ts_id(route_name)}" if route_name else f"route_{_ts_id(rt_name)}_{_ts_id(dest)}"
            lines += [
                f'const {route_var} = new azure_native.network.Route("{route_name or dest}", {{',
                f'    routeName: "{route_name or dest}",',
                f"    routeTableName: {rt_var}.name,",
                f'    resourceGroupName: "{rg_name}",',
                f'    addressPrefix: "{dest}",',
                f'    nextHopType: "{next_hop}",',
                "});",
                "",
            ]
        for seg in rt.get("network_segments") or []:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            assoc_var = f"rt_assoc_{_ts_id(seg_name)}_{_ts_id(rt_name)}"
            lines += [
                f'const {assoc_var} = new azure_native.network.SubnetRouteTableAssociation("{seg_name}-{rt_name}-assoc", {{',
                f"    subnetId: subnet_{_ts_id(seg_name)}.id,",
                f"    routeTableId: {rt_var}.id,",
                "});",
                "",
            ]

    # Network ACLs → NSG subnet associations
    for nacl in nacls_by_vpc.get(vpc_name) or []:
        nacl_name = nacl.get("name", "")
        nacl_id = _ts_id(nacl_name)
        for seg in nacl.get("network_segments") or []:
            seg_name = seg.get("name", "")
            if not seg_name:
                continue
            assoc_var = f"nacl_assoc_{_ts_id(seg_name)}_{nacl_id}"
            lines += [
                f'const {assoc_var} = new azure_native.network.SubnetNetworkSecurityGroupAssociation("{seg_name}-{nacl_name}-assoc", {{',
                f"    subnetId: subnet_{_ts_id(seg_name)}.id,",
                f"    networkSecurityGroupId: nsg_{nacl_id}.id,",
                "});",
                "",
            ]

    # VPN Gateways
    for vpngw in vpngws_by_vpc.get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_var = f"vpngw_{_ts_id(vpngw_name)}"
        lines += [
            f'const {vpngw_var} = new azure_native.network.VirtualNetworkGateway("{vpngw_name}", {{',
            f'    virtualNetworkGatewayName: "{vpngw_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            '    gatewayType: "Vpn",',
            '    vpnType: "RouteBased",',
            "    ipConfigurations: [],",
            f'    tags: {{ Name: "{vpngw_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]

    # Auto Scaling Groups → Azure VMSS
    for asg in asgs_by_vpc.get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_var = _ts_id(asg_name)
        subnet_id = (
            f"subnet_{_ts_id((asg.get('network_segments') or [{}])[0].get('name', ''))}.id"
            if asg.get("network_segments")
            else '""'
        )
        lines += [
            f'const {asg_var} = new azure_native.compute.VirtualMachineScaleSet("{asg_name}", {{',
            f'    vmScaleSetName: "{asg_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            "    sku: {",
            f'        name: "{asg.get("instance_type", "")}",',
            f"        capacity: {asg.get('desired_capacity', 1)},",
            "    },",
            "    virtualMachineProfile: {",
            "        storageProfile: {",
            "            imageReference: {",
            f'                id: "{asg.get("image", "")}",',
            "            },",
            "        },",
            "        networkProfile: {",
            "            networkInterfaceConfigurations: [{",
            f'                name: "{asg_name}-nic",',
            "                primary: true,",
            "                ipConfigurations: [{",
            f'                    name: "{asg_name}-ipconfig",',
            f"                    subnetId: {subnet_id},",
            "                }],",
            "            }],",
            "        },",
            "    },",
            '    tags: { ManagedBy: "Infrahub" },',
            "});",
            "",
        ]

    return lines


def _account_blocks_azure(
    tgws: list,
    cgws: list,
    peerings: list,
    direct_connects: list,
    rg_name: str,
    location: str,
) -> list[str]:
    lines: list[str] = []

    for cgw in cgws:
        cgw_name = cgw.get("name", "")
        cgw_var = f"lgw_{_ts_id(cgw_name)}"
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += [
            f'const {cgw_var} = new azure_native.network.LocalNetworkGateway("{cgw_name}", {{',
            f'    localNetworkGatewayName: "{cgw_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            f'    gatewayIpAddress: "{ip_val}",',
            f'    tags: {{ Name: "{cgw_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]

    for peering in peerings:
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        req_var = f"vnet_peer_{_ts_id(req_vpc)}_to_{_ts_id(acc_vpc)}"
        acc_var = f"vnet_peer_{_ts_id(acc_vpc)}_to_{_ts_id(req_vpc)}"
        lines += [
            f'const {req_var} = new azure_native.network.VirtualNetworkPeering("{req_vpc}-to-{acc_vpc}", {{',
            f'    virtualNetworkPeeringName: "{req_vpc}-to-{acc_vpc}",',
            f'    resourceGroupName: "{rg_name}",',
            f"    virtualNetworkName: vnet_{_ts_id(req_vpc)}.name,",
            f"    remoteVirtualNetworkId: vnet_{_ts_id(acc_vpc)}.id,",
            "});",
            "",
            f'const {acc_var} = new azure_native.network.VirtualNetworkPeering("{acc_vpc}-to-{req_vpc}", {{',
            f'    virtualNetworkPeeringName: "{acc_vpc}-to-{req_vpc}",',
            f'    resourceGroupName: "{rg_name}",',
            f"    virtualNetworkName: vnet_{_ts_id(acc_vpc)}.name,",
            f"    remoteVirtualNetworkId: vnet_{_ts_id(req_vpc)}.id,",
            "});",
            "",
        ]

    for dc in direct_connects:
        dc_name = dc.get("name", "")
        dc_var = f"er_{_ts_id(dc_name)}"
        lines += [
            f'const {dc_var} = new azure_native.network.ExpressRouteCircuit("{dc_name}", {{',
            f'    circuitName: "{dc_name}",',
            f'    location: "{location}",',
            f'    resourceGroupName: "{rg_name}",',
            '    sku: { name: "Standard_MeteredData", tier: "Standard", family: "MeteredData" },',
            '    serviceProviderProperties: { serviceProviderName: "", peeringLocation: "", bandwidthInMbps: 0 },',
            f'    tags: {{ Name: "{dc_name}", ManagedBy: "Infrahub" }},',
            "});",
            "",
        ]

    return lines


def _vpc_blocks_gcp(
    vpc: dict,
    sgs_by_vpc: dict,
    inst_by_vpc: dict,
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
    vpc_var = f"network_{_ts_id(vpc_name)}"

    lines += [
        f'const {vpc_var} = new gcp.compute.Network("{vpc_name}", {{',
        f'    name: "{vpc_name}",',
        "    autoCreateSubnetworks: false,",
        "});",
        "",
    ]

    for seg in vpc.get("network_segments") or []:
        seg_name = seg.get("name", "")
        seg_var = f"subnet_{_ts_id(seg_name)}"
        cidr = (seg.get("cidr_block") or {}).get("prefix", "")
        az_name = (seg.get("availability_zone") or {}).get("name", "")
        seg_region = "-".join(az_name.split("-")[:-1]) if az_name else region
        lines += [
            f'const {seg_var} = new gcp.compute.Subnetwork("{seg_name}", {{',
            f'    name: "{seg_name}",',
            f'    ipCidrRange: "{cidr}",',
            f'    region: "{seg_region}",',
            f"    network: {vpc_var}.id,",
            "});",
            "",
        ]

    for sg in sgs_by_vpc.get(vpc_name) or []:
        sg_name = sg.get("name", "")
        sg_var = f"firewall_{_ts_id(sg_name)}"
        lines += [
            f'const {sg_var} = new gcp.compute.Firewall("{sg_name}", {{',
            f'    name: "{sg_name}",',
            f"    network: {vpc_var}.name,",
            '    allows: [{ protocol: "tcp" }],',
            "});",
            "",
        ]

    for inst in inst_by_vpc.get(vpc_name) or []:
        inst_name = inst.get("name", "")
        inst_var = f"instance_{_ts_id(inst_name)}"
        az_name = (inst.get("availability_zone") or {}).get("name", "")
        seg_ref = inst.get("network_segment") or {}
        subnet_ref = f"subnet_{_ts_id(seg_ref.get('name', ''))}.id" if seg_ref.get("name") else '""'
        lines += [
            f'const {inst_var} = new gcp.compute.Instance("{inst_name}", {{',
            f'    name: "{inst_name}",',
            f'    machineType: "{inst.get("instance_type", "")}",',
            f'    zone: "{az_name}",',
            "    bootDisk: {",
            "        initializeParams: {",
            f'            image: "{inst.get("image", "")}",',
            "        },",
            "    },",
            f"    networkInterfaces: [{{ subnetwork: {subnet_ref} }}],",
            f'    labels: {{ name: "{_ts_id(inst_name)}", managed_by: "infrahub" }},',
            "});",
            "",
        ]

    # Public IPs (GCP compute addresses)
    for pip in public_ips:
        pip_name = pip.get("name", "")
        pip_var = f"addr_{_ts_id(pip_name)}"
        lines += [
            f'const {pip_var} = new gcp.compute.Address("{pip_name}", {{',
            f'    name: "{pip_name}",',
            f'    region: "{region}",',
            "});",
            "",
        ]

    # Cloud Router + NAT
    for nat in nats_by_vpc.get(vpc_name) or []:
        nat_name = nat.get("name", "")
        router_var = f"router_{_ts_id(nat_name)}"
        nat_var = f"nat_{_ts_id(nat_name)}"
        lines += [
            f'const {router_var} = new gcp.compute.Router("{nat_name}-router", {{',
            f'    name: "{nat_name}-router",',
            f'    region: "{region}",',
            f"    network: {vpc_var}.id,",
            "});",
            "",
            f'const {nat_var} = new gcp.compute.RouterNat("{nat_name}", {{',
            f'    name: "{nat_name}",',
            f"    router: {router_var}.name,",
            f'    region: "{region}",',
            '    natIpAllocateOption: "AUTO_ONLY",',
            '    sourceSubnetworkIpRangesToNat: "ALL_SUBNETWORKS_ALL_IP_RANGES",',
            "});",
            "",
        ]

    # Routes
    for route in routes:
        route_rt = (route.get("route_table") or {}).get("name", "")
        rt_obj = next((rt for rt in (rts_by_vpc.get(vpc_name) or []) if rt.get("name") == route_rt), None)
        if rt_obj is None:
            continue
        route_name = route.get("name", "")
        dest = (route.get("destination") or {}).get("prefix", "")
        igw_ref = (route.get("internet_gateway") or {}).get("name", "")
        route_var = f"route_{_ts_id(route_name)}" if route_name else f"route_{_ts_id(vpc_name)}_{_ts_id(dest)}"
        route_args = [
            f'const {route_var} = new gcp.compute.Route("{route_name or dest}", {{',
            f'    name: "{route_name or dest}",',
            f"    network: {vpc_var}.id,",
            f'    destRange: "{dest}",',
            "    priority: 1000,",
        ]
        if igw_ref:
            route_args.append('    nextHopGateway: "default-internet-gateway",')
        route_args += ["});", ""]
        lines += route_args

    # VPN Gateways
    for vpngw in vpngws_by_vpc.get(vpc_name) or []:
        vpngw_name = vpngw.get("name", "")
        vpngw_var = f"vpngw_{_ts_id(vpngw_name)}"
        lines += [
            f'const {vpngw_var} = new gcp.compute.HaVpnGateway("{vpngw_name}", {{',
            f'    name: "{vpngw_name}",',
            f"    network: {vpc_var}.id,",
            f'    region: "{region}",',
            "});",
            "",
        ]

    # Auto Scaling Groups → GCP instance group manager + autoscaler
    for asg in asgs_by_vpc.get(vpc_name) or []:
        asg_name = asg.get("name", "")
        asg_var = _ts_id(asg_name)
        it_var = f"{asg_var}Template"
        igm_var = f"{asg_var}Igm"
        lines += [
            f'const {it_var} = new gcp.compute.InstanceTemplate("{asg_name}-template", {{',
            f'    name: "{asg_name}-template",',
            f'    machineType: "{asg.get("instance_type", "")}",',
            "    disks: [{",
            f'        sourceImage: "{asg.get("image", "")}",',
            "        autoDelete: true,",
            "        boot: true,",
            "    }],",
            "    networkInterfaces: [{",
            f"        network: {vpc_var}.id,",
            "    }],",
            "});",
            "",
            f'const {igm_var} = new gcp.compute.RegionInstanceGroupManager("{asg_name}", {{',
            f'    name: "{asg_name}",',
            f'    region: "{region}",',
            f'    baseInstanceName: "{asg_name}",',
            f"    targetSize: {asg.get('desired_capacity', 1)},",
            "    versions: [{",
            f"        instanceTemplate: {it_var}.id,",
            "    }],",
            "});",
            "",
            f'const {asg_var}Scaler = new gcp.compute.RegionAutoscaler("{asg_name}-autoscaler", {{',
            f'    name: "{asg_name}-autoscaler",',
            f'    region: "{region}",',
            f"    target: {igm_var}.id,",
            "    autoscalingPolicy: {",
            f"        minReplicas: {asg.get('min_size', 1)},",
            f"        maxReplicas: {asg.get('max_size', 1)},",
            "        cooldownPeriod: 60,",
            "    },",
            "});",
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
        cgw_var = f"ext_vpngw_{_ts_id(cgw_name)}"
        ip_val = (cgw.get("ip_address") or {}).get("address", "")
        lines += [
            f'const {cgw_var} = new gcp.compute.ExternalVpnGateway("{cgw_name}", {{',
            f'    name: "{cgw_name}",',
            '    redundancyType: "SINGLE_IP_INTERNALLY_REDUNDANT",',
            f'    interfaces: [{{ id: 0, ipAddress: "{ip_val}" }}],',
            "});",
            "",
        ]

    for peering in peerings:
        req_vpc = (peering.get("requester_virtual_network") or {}).get("name", "")
        acc_vpc = (peering.get("accepter_virtual_network") or {}).get("name", "")
        req_var = f"peering_{_ts_id(req_vpc)}_to_{_ts_id(acc_vpc)}"
        acc_var = f"peering_{_ts_id(acc_vpc)}_to_{_ts_id(req_vpc)}"
        lines += [
            f'const {req_var} = new gcp.compute.NetworkPeering("{req_vpc}-to-{acc_vpc}", {{',
            f'    name: "{req_vpc}-to-{acc_vpc}",',
            f"    network: network_{_ts_id(req_vpc)}.id,",
            f"    peerNetwork: network_{_ts_id(acc_vpc)}.id,",
            "});",
            "",
            f'const {acc_var} = new gcp.compute.NetworkPeering("{acc_vpc}-to-{req_vpc}", {{',
            f'    name: "{acc_vpc}-to-{req_vpc}",',
            f"    network: network_{_ts_id(acc_vpc)}.id,",
            f"    peerNetwork: network_{_ts_id(req_vpc)}.id,",
            "});",
            "",
        ]

    for vif in vifs:
        vif_name = vif.get("name", "")
        vif_var = f"interconnect_{_ts_id(vif_name)}"
        lines += [
            f'const {vif_var} = new gcp.compute.InterconnectAttachment("{vif_name}", {{',
            f'    name: "{vif_name}",',
            '    router: "",',
            f'    region: "{region}",',
            f"    vlanTag8021q: {vif.get('vlan_id') or 0},",
            "});",
            "",
        ]

    return lines


class CloudVpcPulumi(InfrahubTransform):
    """Generate a TypeScript Pulumi index.ts for all VPCs in a CloudAccount."""

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

            lines += [
                'import * as aws from "@pulumi/aws";',
                'import * as pulumi from "@pulumi/pulumi";',
                "",
            ]

            # Provider instances per region
            for region in regions_seen:
                pvar = f"aws_{_ts_id(region)}"
                lines += [
                    f'const {pvar} = new aws.Provider("{region}", {{ region: "{region}" }});',
                    "",
                ]

            for vpc in vpcs:
                region = vpc_region_map.get(vpc.get("name", ""), "")
                provider_var = f"aws_{_ts_id(region)}" if region else ""
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
                    provider_var=provider_var,
                )
            lines += _account_blocks_aws(
                transit_gateways, customer_gateways, peerings, direct_connects, public_ips, provider_var=""
            )  # TGW is account-wide; use default

        elif provider_name == "azure":
            account_name = account.get("name", "default")
            region_obj = vpcs[0].get("region") or {}
            location = region_obj.get("name", "eastus") if region_obj else "eastus"
            rg_name = f"{account_name}-rg"
            lines += ['import * as azure_native from "@pulumi/azure-native";', ""]
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
                    rg_name,
                    location,
                )
            lines += _account_blocks_azure(
                transit_gateways, customer_gateways, peerings, direct_connects, rg_name, location
            )

        else:  # gcp
            region_obj = vpcs[0].get("region") or {}
            region = region_obj.get("name", "") if region_obj else ""
            lines += ['import * as gcp from "@pulumi/gcp";', ""]
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
