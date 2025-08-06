from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


class AristaSpine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data: Any) -> dict:
        data = get_data(data)

        # Initialize Arista EOS configuration structure for spine
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
                    "evpn": {"activate": True, "route_reflector": True},
                },
            },
            "router_ospf": {
                "process_id": 1,
                "router_id": None,
                "areas": {},
                "interfaces": {},
            },
            "ip_routing": True,
            "service_routing_protocols_model": "multi-agent",
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

                result["interfaces"][intf_name] = intf_config
                continue

            # Handle physical interfaces for connections to leaves
            if intf_name.startswith(("Ethernet", "eth")):
                # Standardize name to Arista format
                intf_name = intf_name.replace("eth", "Ethernet")

                intf_config = {
                    "description": interface.get("description", ""),
                    "type": "routed",  # Always routed for spine interfaces
                    "mtu": 9214,
                    "ip_address": None,
                }

                # Configure unnumbered interfaces for all spine-leaf connections
                intf_config["ip_address"] = "unnumbered Loopback0"
                # Add to OSPF interface list for proper unnumbered config
                result["router_ospf"]["interfaces"][intf_name] = {
                    "network_type": "point-to-point"
                }

                # Add direct IP addressing as fallback if unnumbered not possible
                if interface.get("ip_addresses"):
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
                                result["router_bgp"]["neighbors"][remote_ip] = {
                                    "remote_as": remote_asn,
                                    "description": f"Leaf {remote_ip}",
                                    "update_source": "Loopback0",
                                    "send_community": "extended",
                                    "maximum_routes": 12000,
                                    "route_reflector_client": True,
                                    "address_family": {"evpn": {"activate": True}},
                                }

                result["interfaces"][intf_name] = intf_config

        # If we have OSPF interfaces and a router ID but no areas, set default area 0
        if (
            result["router_ospf"]["interfaces"]
            and router_id
            and not result["router_ospf"]["areas"]
        ):
            result["router_ospf"]["areas"]["0"] = {"area_type": "normal"}

        return result
