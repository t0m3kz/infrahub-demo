from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from .common import get_data


class SonicLeaf(InfrahubTransform):
    query = "leaf_config"

    async def transform(self, data: Any) -> dict:
        data = get_data(data)

        # Initialize SONiC config structure with all required sections
        result = {
            "hostname": data["name"],
            "PORT": {},
            "INTERFACE": {},
            "VLAN": {},
            "VLAN_INTERFACE": {},
            "LOOPBACK_INTERFACE": {},
            "BGP_NEIGHBOR": {},
            "BGP_ROUTER_AF": {},
            "OSPF_ROUTER": {},
            "VXLAN": {},
            "VXLAN_TUNNEL": {},
            "EVPN_NVO": {},
            "VRF": {},
            "NEIGH": {},
            "LOOPBACK": {},
        }

        # Track VNIs for VXLAN configuration
        vni_map = {}  # Maps VLAN ID to VNI
        vtep_loopback = None
        vtep_ip = None
        nvo_name = "nvo1"  # Default NVO name for EVPN

        # Process OSPF and BGP service configurations
        local_asn = None
        router_id = None

        for service in data.get("device_service") or []:
            if not service:
                continue

            match service["typename"]:
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
                            result["BGP_NEIGHBOR"][remote_ip] = {
                                "asn": remote_asn,
                                "holdtime": "180",
                                "keepalive": "60",
                                "local_addr": router_id,
                                "name": f"spine_{remote_ip}",
                            }

                    # Configure BGP router address family with EVPN
                    if local_asn:
                        result["BGP_ROUTER_AF"][f"bgp {local_asn}"] = {
                            "address_family": {
                                "ipv4_unicast": {"redistribute": ["connected", "ospf"]},
                                "l2vpn_evpn": {
                                    "advertise": ["all"],
                                    "advertise-all-vni": "true",
                                    "rd": "auto",
                                    "route-target": {
                                        "import": "auto",
                                        "export": "auto",
                                    },
                                },
                            }
                        }

        # Process interfaces and collect VLANs, VNIs, and VTEP info in one pass
        vlan_interfaces = {}
        tenant_vrf = "Tenant_VRF"  # Default VRF name for tenant traffic

        # Add tenant VRF configuration
        result["VRF"][tenant_vrf] = {"fallback": "true"}

        for interface in data.get("interfaces") or []:
            if not interface:
                continue

            intf_name = interface.get("name")
            if not intf_name:
                continue

            # Handle loopback interfaces
            if intf_name.startswith("loopback"):
                if interface.get("role") == "loopback-vtep" and interface.get(
                    "ip_addresses"
                ):
                    # This is a VTEP loopback
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            vtep_loopback = intf_name
                            vtep_ip = addr.split("/")[0]
                            result["LOOPBACK_INTERFACE"][intf_name] = {
                                "ip_address": addr
                            }
                elif interface.get("ip_addresses"):
                    # Regular loopback (for router-id, etc.)
                    for ip in interface["ip_addresses"]:
                        addr = ip.get("address")
                        if addr:
                            result["LOOPBACK_INTERFACE"][intf_name] = {
                                "ip_address": addr
                            }
                continue

            # Handle physical interfaces
            if intf_name.startswith(("ce", "xe", "eth")):
                # Add to PORT section
                result["PORT"][intf_name] = {"admin_status": "up"}

                # Create interface configuration
                intf_config = {}

                # Add description if present
                if interface.get("description"):
                    intf_config["description"] = interface["description"]

                # Handle interface role for unnumbered configuration
                underlay_loopback = None
                is_underlay = False
                is_overlay = False

                # Check interface role
                if interface.get("role") == "underlay":
                    is_underlay = True
                elif interface.get("role") == "overlay":
                    is_overlay = True

                # Find the appropriate loopback for unnumbered interfaces
                if is_underlay or is_overlay:
                    # Look for router-id loopback for unnumbered interfaces
                    for other_intf in data.get("interfaces") or []:
                        if (
                            other_intf.get("name", "").startswith("loopback")
                            and other_intf.get("role") == "router-id"
                        ):
                            for ip in other_intf.get("ip_addresses") or []:
                                if ip.get("address"):
                                    underlay_loopback = other_intf.get("name")
                                    break
                            if underlay_loopback:
                                break

                # Add IP address if present or configure as unnumbered
                if is_underlay and underlay_loopback:
                    # Configure as unnumbered interface using the router-id loopback
                    intf_config["unnumbered_intf"] = underlay_loopback
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
                            intf_config["ospf_network_type"] = "point-to-point"
                            intf_config["ospf_area"] = str(area)

                # Process VLANs and Network Segments
                vlan_members = []
                for service in interface.get("service") or []:
                    if service.get("typename") == "ServiceNetworkSegment":
                        if service.get("vni") is not None:
                            vni = service["vni"]
                            vlan_id = vni  # Using VNI as VLAN ID for simplicity

                            # Store VNI mapping
                            vni_map[str(vlan_id)] = vni

                            # Add to VLANs section
                            result["VLAN"][str(vlan_id)] = {
                                "vlanid": str(vlan_id),
                                "members": [intf_name],
                            }

                            # Create VLAN interface with VRF
                            vlan_intf = f"Vlan{vlan_id}"
                            vlan_interfaces[vlan_intf] = {"vrf_name": tenant_vrf}

                            # Add prefixes if available
                            if service.get("prefixes"):
                                for prefix in service["prefixes"]:
                                    if prefix.get("prefix"):
                                        vlan_interfaces[vlan_intf]["ip_address"] = (
                                            prefix["prefix"]
                                        )

                            vlan_members.append(vlan_id)

                if vlan_members:
                    intf_config["vlans"] = vlan_members

                # Add to INTERFACE section if we have config
                if intf_config:
                    result["INTERFACE"][intf_name] = intf_config

        # Add VLAN interfaces
        for vlan_intf, config in vlan_interfaces.items():
            result["VLAN_INTERFACE"][vlan_intf] = config

        # Configure VXLAN if we have VTEP info and VNIs
        if vtep_ip and vtep_loopback and vni_map:
            # Configure the VXLAN tunnel endpoint with enhanced attributes
            result["VXLAN_TUNNEL"]["vtep"] = {
                "src_ip": vtep_ip,
                "primary_ip": vtep_ip,
                "enable_vxlan_outer_ipv6_hash": "true",  # Enable optimized load balancing
                "enable_vtep_discovery": "true",  # Enable automatic VTEP discovery for better scaling
            }

            # Configure EVPN NVO
            result["EVPN_NVO"][nvo_name] = {
                "source_vtep": "vtep",
                "type": "evpn",
                "advertise_primary_ip": "true",  # Advertise primary IP of the VTEP
            }

            # Configure LOOPBACK section for VTEP loopback
            result["LOOPBACK"][vtep_loopback] = {
                "admin_status": "up",
            }

            # Configure VXLAN mappings for each VLAN with enhanced attributes
            for vlan_id, vni in vni_map.items():
                result["VXLAN"][f"evpn_nvo:{nvo_name}:vni-{vni}"] = {
                    "vni": str(vni),
                    "vlan": vlan_id,
                    "vrf": "Tenant_VRF",  # Link to tenant VRF
                    "advertise_prefix": "true",  # Advertise prefixes via EVPN
                }

        return result
