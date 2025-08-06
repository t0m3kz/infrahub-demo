from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


class AristaLeaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> dict:
        data = get_data(data)

        # Initialize Arista EOS configuration structure
        result = {
            "hostname": data["name"],
            "interfaces": {},
            "router_bgp": {
                "asn": None,
                "router_id": None,
                "bgp": {"default": {"ipv4_unicast": False}},
                "neighbors": {},
                "address_family": {
                    "ipv4": {"activate": False, "redistribute": ["connected", "ospf"]},
                    "evpn": {"activate": True},
                },
                "vrf": {},
            },
            "router_ospf": {
                "process_id": 1,
                "router_id": None,
                "areas": {},
                "interfaces": {},
            },
            "vrfs": {
                "Tenant_VRF": {
                    "rd": "auto",
                    "route_targets": {"import": ["evpn"], "export": ["evpn"]},
                }
            },
            "vlans": {},
            "vlan_interfaces": {},
            "vxlan_interface": {
                "Vxlan1": {
                    "vxlan": {
                        "source_interface": None,
                        "udp_port": 4789,
                        "vrfs": {},
                        "vlans": {},
                    }
                }
            },
        }

        # Process OSPF and BGP service configurations
        local_asn = None
        router_id = None

        for service in data.get("device_service") or []:
            if not service:
                continue

            match service.get("typename", ""):
                case "ServiceOSPF":
                    if service.get("router_id") and service["router_id"].get("address"):
                        router_id = service["router_id"]["address"].split("/")[0]
                        result["router_ospf"]["router_id"] = router_id

                    if service.get("area") and service["area"].get("area") is not None:
                        area = str(service["area"]["area"])
                        result["router_ospf"]["areas"][area] = {"area_type": "normal"}

                case "ServiceBGPSession":
                    if service.get("local_as") and service["local_as"].get("asn"):
                        local_asn = service["local_as"]["asn"]
                        result["router_bgp"]["asn"] = local_asn
                        result["router_bgp"]["router_id"] = router_id

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
                            result["router_bgp"]["neighbors"][remote_ip] = {
                                "remote_as": remote_asn,
                                "description": f"Spine {remote_ip}",
                                "update_source": "Loopback0",
                                "send_community": "extended",
                                "maximum_routes": 12000,
                                "address_family": {"evpn": {"activate": True}},
                            }

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
            if intf_name.startswith(("Loopback", "loopback")):
                # Standardize name to Arista format
                intf_name = intf_name.replace("loopback", "Loopback")

                intf_config = {
                    "description": interface.get("description", ""),
                    "ip_address": None,
                }

                if interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            intf_config["ip_address"] = addr

                            # Check if this is VTEP loopback
                            if interface.get("role") == "loopback-vtep":
                                addr.split("/")[0]
                                # Update VXLAN source interface
                                result["vxlan_interface"]["Vxlan1"]["vxlan"][
                                    "source_interface"
                                ] = intf_name

                result["interfaces"][intf_name] = intf_config
                continue

            # Handle physical interfaces for connections to spines
            if intf_name.startswith(("Ethernet", "eth")):
                # Standardize name to Arista format
                intf_name = intf_name.replace("eth", "Ethernet")

                intf_config = {
                    "description": interface.get("description", ""),
                    "type": "routed",  # Default to routed for spine-facing interfaces
                    "mtu": 9214,
                    "ip_address": None,
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
                    intf_config["ip_address"] = "unnumbered Loopback0"
                    # Add to OSPF interface list for proper unnumbered config
                    result["router_ospf"]["interfaces"][intf_name] = {
                        "network_type": "point-to-point"
                    }
                elif interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            intf_config["ip_address"] = addr

                # Add OSPF settings if found in services
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceOSPFArea":
                        if service.get("area") is not None:
                            area = service["area"]
                            # Add to OSPF interface config
                            result["router_ospf"]["interfaces"][intf_name] = {
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
                                "ip_address": None,
                            }

                            # Add prefixes if available
                            if service.get("prefixes"):
                                for prefix in service["prefixes"]:
                                    if prefix.get("prefix"):
                                        result["vlan_interfaces"][vlan_intf][
                                            "ip_address"
                                        ] = prefix["prefix"]

                            # Add VLAN to VXLAN configuration
                            result["vxlan_interface"]["Vxlan1"]["vxlan"]["vlans"][
                                str(vlan_id)
                            ] = {"vni": vni}

                            # Change interface type to access or trunk as needed
                            intf_config["type"] = "switched"
                            intf_config["switchport_mode"] = "access"
                            intf_config["switchport_access_vlan"] = str(vlan_id)

                result["interfaces"][intf_name] = intf_config

        # Add BGP EVPN config for tenant VRF if we have VNIs
        if vni_map and local_asn:
            # Configure VRF in BGP
            result["router_bgp"]["vrf"] = {
                "Tenant_VRF": {
                    "rd": "auto",
                    "route_targets": {"import": ["evpn"], "export": ["evpn"]},
                    "redistribute": ["connected"],
                }
            }

        return result
