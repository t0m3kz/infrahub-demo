from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


class CiscoSpine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data: Any) -> dict:
        data = get_data(data)

        # Initialize Cisco NX-OS configuration structure for spine
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
            "feature_flags": [
                "feature ospf",
                "feature bgp",
                "feature pim",
                "feature interface-vlan",
                "feature vn-segment-vlan-based",
                "feature nv overlay",
                "nv overlay evpn",
            ],
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
                        result["ospf"]["router_id"] = router_id

                    if service.get("area") and service["area"].get("area") is not None:
                        area = str(service["area"]["area"])
                        result["ospf"]["areas"][area] = {"area_type": "normal"}

                case "ServiceBGPSession":
                    if service.get("local_as") and service["local_as"].get("asn"):
                        local_asn = service["local_as"]["asn"]
                        result["bgp"]["asn"] = local_asn
                        result["bgp"]["router_id"] = router_id

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

                result["interfaces"][intf_name] = intf_config
                continue

            # Handle physical interfaces for connections to leaves
            if intf_name.startswith(("Ethernet", "eth")):
                intf_config = {
                    "description": interface.get("description", ""),
                    "mode": "routed",  # Always routed for spine interfaces
                    "ip_unnumbered": None,
                    "ospf": {},
                    "mtu": "9216",
                    "speed": "auto",
                    "ip_addresses": [],
                }

                # Configure unnumbered interfaces for all spine-leaf connections
                unnumbered_loopback = "loopback0"  # Use loopback0 by default
                intf_config["ip_unnumbered"] = unnumbered_loopback

                # Add direct IP addressing as fallback if unnumbered not possible
                if interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            intf_config["ip_addresses"].append(addr)
                            # If we have direct addressing, don't use unnumbered
                            intf_config["ip_unnumbered"] = None

                # Add OSPF settings if found in services
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceOSPFArea":
                        if service.get("area") is not None:
                            area = service["area"]
                            intf_config["ospf"] = {
                                "area": str(area),
                                "network_type": "point-to-point",
                            }

                # Configure BGP peering with leaf if available
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceBGPSession":
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
                                    "description": f"Leaf {remote_ip}",
                                    "update_source": "loopback0",
                                    "address_families": ["l2vpn_evpn"],
                                }
                                result["bgp"]["neighbors"].append(neighbor)

                result["interfaces"][intf_name] = intf_config

        # Add route-reflector config for spine if we have local ASN
        if local_asn:
            result["bgp"]["address_families"]["l2vpn_evpn"][
                "route_reflector_client"
            ] = True

        return result
