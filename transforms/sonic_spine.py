from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


class SonicSpine(InfrahubTransform):
    query = "spine_config"

    async def transform(self, data: Any) -> dict:
        data = get_data(data)

        # Initialize SONiC config structure with all required sections for a spine
        result = {
            "hostname": data["name"],
            "PORT": {},
            "INTERFACE": {},
            "LOOPBACK_INTERFACE": {},
            "BGP_NEIGHBOR": {},
            "BGP_ROUTER_AF": {},
            "OSPF_ROUTER": {},
            "NEIGH": {},
            "LOOPBACK": {},
        }

        # Process OSPF and BGP service configurations
        local_asn = None
        router_id = None

        for service in data.get("device_service") or []:
            if not service:
                continue

            match service.get("typename", ""):
                case "ServiceOSPF":
                    ospf_config = {}

                    if service.get("router_id") and service["router_id"].get("address"):
                        router_id = service["router_id"]["address"].split("/")[0]
                        ospf_config["router_identifier"] = router_id

                    if service.get("area") and service["area"].get("area") is not None:
                        area = str(service["area"]["area"])
                        ospf_config[f"area {area}"] = {"area_type": "normal"}

                    if ospf_config:
                        result["OSPF_ROUTER"]["ospf"] = ospf_config

                case "ServiceBGPSession":
                    if service.get("local_as") and service["local_as"].get("asn"):
                        local_asn = service["local_as"]["asn"]

                    # For spine nodes, configure BGP peering with leaves
                    if local_asn:
                        result["BGP_ROUTER_AF"][f"bgp {local_asn}"] = {
                            "address_family": {
                                "ipv4_unicast": {"redistribute": ["connected", "ospf"]},
                                "l2vpn_evpn": {
                                    "advertise": ["all"],
                                    "rd": "auto",
                                    "route-target": {
                                        "import": "auto",
                                        "export": "auto",
                                    },
                                },
                            }
                        }

        # Process loopback and physical interfaces
        for interface in data.get("interfaces") or []:
            if not interface:
                continue

            intf_name = interface.get("name")
            if not intf_name:
                continue

            # Handle loopback interfaces
            if intf_name.startswith("loopback"):
                if interface.get("ip_addresses"):
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            # For spine, all loopbacks are potential router-ids
                            result["LOOPBACK_INTERFACE"][intf_name] = {
                                "ip_address": addr
                            }

                            # Also configure the LOOPBACK section
                            result["LOOPBACK"][intf_name] = {
                                "admin_status": "up",
                            }
                continue

            # Handle physical interfaces for connections to leaves
            if intf_name.startswith(("ce", "xe", "eth")):
                # Add to PORT section
                result["PORT"][intf_name] = {"admin_status": "up"}

                # Create interface configuration
                intf_config = {}

                # Add description if present
                if interface.get("description"):
                    intf_config["description"] = interface["description"]

                # Find router-id loopback for unnumbered interfaces
                unnumbered_loopback = None
                for other_intf in data.get("interfaces") or []:
                    if other_intf.get("name", "").startswith("loopback"):
                        for ip in other_intf.get("ip_addresses") or []:
                            if ip.get("address"):
                                unnumbered_loopback = other_intf.get("name")
                                break
                        if unnumbered_loopback:
                            break

                # Configure unnumbered interfaces for spine-leaf connections
                if unnumbered_loopback:
                    intf_config["unnumbered_intf"] = unnumbered_loopback
                elif interface.get("ip_addresses"):
                    # Fallback to direct IP if unnumbered not possible
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            intf_config["ip_address"] = addr

                # Add OSPF settings if found in services
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceOSPFArea":
                        if service.get("area") is not None:
                            area = service["area"]
                            intf_config["ospf_network_type"] = "point-to-point"
                            intf_config["ospf_area"] = str(area)

                # Configure BGP peering with leaf if available
                # This is usually done through unnumbered BGP, but we add support for direct peering too
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

                            if remote_ip and remote_asn and router_id:
                                result["BGP_NEIGHBOR"][remote_ip] = {
                                    "asn": remote_asn,
                                    "holdtime": "180",
                                    "keepalive": "60",
                                    "local_addr": router_id,
                                    "name": f"leaf_{remote_ip}",
                                }

                # Add to INTERFACE section if we have config
                if intf_config:
                    result["INTERFACE"][intf_name] = intf_config

        return result
