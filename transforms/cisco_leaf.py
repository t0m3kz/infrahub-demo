from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


class CiscoLeaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> dict:
        data = get_data(data)

        # Initialize Cisco NX-OS configuration structure
        result = {
            "hostname": data["name"],
            "interfaces": {},
            "bgp": {
                "asn": None,
                "router_id": None,
                "neighbors": [],
                "address_families": {
                    "ipv4_unicast": {"redistribute": ["direct", "ospf"]},
                    "l2vpn_evpn": {"retain_route_targets": "all"},
                },
            },
            "ospf": {"process_id": "1", "router_id": None, "areas": {}},
            "vrf": {
                "Tenant_VRF": {
                    "description": "Tenant Traffic VRF",
                    "rd": "auto",
                    "address_families": {
                        "ipv4_unicast": {
                            "route_target": {"import": "auto", "export": "auto"}
                        },
                        "l2vpn_evpn": {
                            "route_target": {"import": "auto", "export": "auto"}
                        },
                    },
                }
            },
            "nve": {
                "1": {"source_interface": None, "host_reachability_protocol": "bgp"}
            },
            "vlans": {},
            "vlan_interfaces": {},
        }

        # Process OSPF and BGP service configurations
        local_asn = None
        router_id = None
        vtep_loopback = None

        for service in data.get("device_service") or []:
            if not service:
                continue

            match service.get("typename", ""):
                case "ServiceOSPF":
                    if service.get("router_id") and service["router_id"].get("address"):
                        router_id = service["router_id"]["address"].split("/")[0]
                        result["ospf"]["router_id"] = router_id

                    if service.get("area") and service["area"].get("area") is not None:
                        area = str(service["area"]["area"])
                        result["ospf"]["areas"][area] = {"area_type": "normal"}

                case "ServiceBGPSession":
                    if service.get("local_as") and service["local_as"].get("asn"):
                        local_asn = service["local_as"]["asn"]
                        result["bgp"]["asn"] = local_asn
                        result["bgp"]["router_id"] = router_id

                    if service.get("remote_ip") and service.get("remote_as"):
                        remote_ip = (
                            service["remote_ip"]["address"].split("/")[0]
                            if service["remote_ip"].get("address")
                            else None
                        )
                        remote_asn = (
                            service["remote_as"]["asn"]
                            if service["remote_as"].get("asn")
                            else None
                        )

                        if remote_ip and remote_asn:
                            neighbor = {
                                "ip": remote_ip,
                                "asn": remote_asn,
                                "description": f"Spine {remote_ip}",
                                "update_source": "loopback0",
                                "address_families": ["l2vpn_evpn"],
                            }
                            result["bgp"]["neighbors"].append(neighbor)

        # Track VNIs for VXLAN configuration
        vni_map = {}  # Maps VLAN ID to VNI

        # Process interfaces
        for interface in data.get("interfaces") or []:
            if not interface:
                continue

            intf_name = interface.get("name")
            if not intf_name:
                continue

            # Handle loopback interfaces
            if intf_name.startswith("loopback"):
                intf_config = {
                    "description": interface.get("description", ""),
                    "ip_addresses": [],
                }

                if interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            intf_config["ip_addresses"].append(addr)

                            # Check if this is VTEP loopback
                            if interface.get("role") == "loopback-vtep":
                                vtep_loopback = intf_name
                                addr.split("/")[0]
                                # Update NVE source interface
                                result["nve"]["1"]["source_interface"] = intf_name

                result["interfaces"][intf_name] = intf_config
                continue

            # Handle physical interfaces for connections to spines
            if intf_name.startswith(("Ethernet", "eth")):
                intf_config = {
                    "description": interface.get("description", ""),
                    "mode": "routed",  # Default to routed for spine-facing interfaces
                    "ip_unnumbered": None,
                    "ospf": {},
                    "mtu": "9216",
                    "speed": "auto",
                    "ip_addresses": [],
                }

                # Handle interface role for unnumbered configuration
                is_underlay = False

                # Check interface role
                if interface.get("role") == "underlay":
                    is_underlay = True
                elif interface.get("role") == "overlay":
                    pass

                # Configure unnumbered interfaces for underlay
                if is_underlay:
                    intf_config["ip_unnumbered"] = "loopback0"
                elif interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            intf_config["ip_addresses"].append(addr)

                # Add OSPF settings if found in services
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceOSPFArea":
                        if service.get("area") is not None:
                            area = service["area"]
                            intf_config["ospf"] = {
                                "area": str(area),
                                "network_type": "point-to-point",
                            }

                # Process VLANs and Network Segments
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceNetworkSegment":
                        if service.get("vni") is not None:
                            vni = service["vni"]
                            vlan_id = vni  # Using VNI as VLAN ID for simplicity

                            # Store VNI mapping
                            vni_map[str(vlan_id)] = vni

                            # Add to VLANs section
                            result["vlans"][str(vlan_id)] = {
                                "name": f"VLAN{vlan_id}",
                                "vni": vni,
                            }

                            # Create VLAN interface with VRF
                            vlan_intf = f"Vlan{vlan_id}"
                            result["vlan_interfaces"][vlan_intf] = {
                                "description": f"VXLAN VLAN {vlan_id}",
                                "vrf": "Tenant_VRF",
                                "ip_addresses": [],
                            }

                            # Add prefixes if available
                            if service.get("prefixes"):
                                for prefix in service["prefixes"]:
                                    if prefix.get("prefix"):
                                        result["vlan_interfaces"][vlan_intf][
                                            "ip_addresses"
                                        ].append(prefix["prefix"])

                            # Change interface mode to access for this VLAN
                            intf_config["mode"] = "access"
                            intf_config["access_vlan"] = str(vlan_id)

                result["interfaces"][intf_name] = intf_config

        # Generate VXLAN configuration based on collected VNIs
        if vtep_loopback and vni_map:
            for vlan_id, vni in vni_map.items():
                # Associate VNI with VLAN in NVE configuration
                if "vni_config" not in result["nve"]["1"]:
                    result["nve"]["1"]["vni_config"] = {}

                result["nve"]["1"]["vni_config"][str(vni)] = {
                    "associate_vrf": "false",  # Set to true for L3VNI
                    "mcast_group": "",  # Would be set for multicast-based VXLAN
                    "suppress_arp": "true",
                    "ingress_replication": "bgp",
                }

        return result
