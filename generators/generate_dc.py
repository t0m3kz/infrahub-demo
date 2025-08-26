"""Infrastructure generator."""

from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreNumberPool
from netutils.interface import sort_interface_list

from .common import TopologyCreator, clean_data
from .schema_protocols import DcimPhysicalInterface, DcimVirtualInterface


class DCTopologyCreator(TopologyCreator):
    """Create data center topology."""

    async def create_fabric_peering(self) -> None:
        """
        Create fabric peering connections for a single site (unnumbered only).
        """
        batch = await self.client.create_batch()

        interfaces: dict = {
            device.name.value: [
                {
                    "name": interface["name"],
                    "role": interface["role"],
                }
                for interface in self.data["templates"][
                    device._data["object_template"][0]
                ]
                if interface["role"] in ["leaf", "uplink"]
            ]
            for device in self.devices
            if device.role.value in ["leaf", "spine", "border_leaf"]
        }

        spines_leaves = {
            name: sort_interface_list(
                [iface.get("name") for iface in ifaces if iface.get("role") == "leaf"]
            )
            for name, ifaces in interfaces.items()
            if "spine" in name
        }
        spine_borders = {
            name: sort_interface_list(
                [iface.get("name") for iface in ifaces if iface.get("role") == "uplink"]
            )
            for name, ifaces in interfaces.items()
            if "spine" in name
        }

        leafs = {
            name: sort_interface_list(
                [iface.get("name") for iface in ifaces if iface.get("role") == "uplink"]
            )
            for name, ifaces in interfaces.items()
            if "leaf" in name and "border" not in name
        }

        border_leafs = {
            name: sort_interface_list(
                [iface.get("name") for iface in ifaces if iface.get("role") == "uplink"]
            )
            for name, ifaces in interfaces.items()
            if "border_leaf" in name
        }

        connections: list = [
            {
                "source": spine,
                "target": leaf,
                "source_interface": spine_interfaces.pop(0),
                "destination_interface": leaf_interfaces.pop(0),
            }
            for spine, spine_interfaces in spines_leaves.items()
            for leaf, leaf_interfaces in leafs.items()
        ]

        connections.extend(
            {
                "source": spine,
                "target": leaf,
                "source_interface": spine_interfaces.pop(0),
                "destination_interface": leaf_interfaces.pop(0),
            }
            for spine, spine_interfaces in spine_borders.items()
            for leaf, leaf_interfaces in border_leafs.items()
        )

        # Always use unnumbered interface role for both OSPF and eBGP
        interface_role = "unnumbered"
        # Assign roles and connectors
        for connection in connections:
            source_endpoint = await self.client.get(
                kind=DcimPhysicalInterface,
                name__value=connection["source_interface"],
                device__name__value=connection["source"],
            )
            target_endpoint = await self.client.get(
                kind=DcimPhysicalInterface,
                name__value=connection["destination_interface"],
                device__name__value=connection["target"],
            )
            source_endpoint.status.value = "active"
            source_endpoint.description.value = (
                f"Peering connection to {' -> '.join(target_endpoint.hfid or [])}"
            )
            source_endpoint.role.value = interface_role
            source_endpoint.connector = target_endpoint.id  # type: ignore
            target_endpoint.status.value = "active"
            target_endpoint.description.value = (
                f"Peering connection to {' -> '.join(source_endpoint.hfid or [])}"
            )
            target_endpoint.role.value = interface_role

            batch.add(
                task=source_endpoint.save, allow_upsert=True, node=source_endpoint
            )
            batch.add(
                task=target_endpoint.save, allow_upsert=True, node=target_endpoint
            )

        async for node, _ in batch.execute():
            self.log.info(
                f"- Created/Updated [{node.get_kind()}] {node.description.value} from {' -> '.join(node.hfid)}"
            )

    async def create_ospf_underlay(self) -> None:
        """Create underlay service and associate it to the respective switches."""
        topology_name = self.data.get("name")
        self.log.info(f"Creating OSPF underlay for {topology_name}")
        await self._create(
            kind="RoutingOSPFArea",
            data={
                "payload": {
                    "name": f"{topology_name}-UNDERLAY",
                    "description": f"{topology_name} OSPF UNDERLAY service",
                    "area": 0,
                    "status": "active",
                    "owner": self.data.get("provider"),
                },
                "store_key": f"UNDERLAY-{topology_name}",
            },
        )
        # self.log.info(self.client.store._branches[self.branch].__dict__)
        self.log.info(f"Creating OSPF instances for {topology_name}")
        # self.log.info(self.client.store.get_by_hfid(f"ServiceOSPFArea__{topology_name}-UNDERLAY"))
        await self._create_in_batch(
            kind="ServiceOSPF",
            data_list=[
                {
                    "payload": {
                        "name": f"{device.name.value.upper()}-UNDERLAY",
                        "owner": self.data.get("provider"),
                        # "description": f"{device.name.value} OSPF UNDERLAY",
                        "area": self.client.store.get(
                            kind="RoutingOSPFArea",
                            key=f"UNDERLAY-{topology_name}",
                        ),
                        "version": "ospfv3",
                        "device": device.id,
                        "status": "active",
                        "router_id": self.client.store.get(
                            key=f"{device.name.value}-loopback0",
                            kind=DcimVirtualInterface,
                        )
                        .ip_addresses[0]
                        .id,
                        "interfaces": await self.client.filters(
                            kind="DcimInterface",
                            role__values=["unnumbered", "loopback"],
                            device__name__value=device.name.value,
                        ),
                    },
                    "store_key": f"UNDERLAY-{device.name.value}",
                }
                for device in self.devices
                if device.role.value in ["spine", "leaf", "border_leaf"]
            ],
        )

        # self.client.log.info(self.data)

        # ... any additional steps ...

    async def create_bgp_peer_groups(self, scenario: str) -> None:
        """Create all BGP peer groups (underlay and overlay) based on scenario."""
        topology_name = self.data.get("name")
        if not topology_name:
            raise ValueError("Topology name is required")

        self.log.info(
            f"Creating BGP peer groups for {topology_name} ({scenario} scenario)"
        )

        # Underlay peer groups (only for eBGP scenario)
        if scenario == "ebgp":
            # Create SPINE-TO-LEAF UNDERLAY peer group
            await self._create(
                kind="RoutingBGPPeerGroup",
                data={
                    "payload": {
                        "name": f"{topology_name}-SPINE-TO-LEAF-UNDERLAY",
                        "description": f"{topology_name} UNDERLAY from spine perspective",
                        "peer_group_type": "SPINE_TO_LEAF",
                        "bfd_enabled": True,
                        "ebgp_multihop": 0,
                        "send_community": True,
                        "send_community_extended": False,
                        "password": "UNDERLAY-secret",
                    },
                    "store_key": f"SPINE-TO-LEAF-UNDERLAY-PG-{topology_name}",
                },
            )

            # Create LEAF-TO-SPINE UNDERLAY peer group
            await self._create(
                kind="RoutingBGPPeerGroup",
                data={
                    "payload": {
                        "name": f"{topology_name}-LEAF-TO-SPINE-UNDERLAY",
                        "description": f"{topology_name} UNDERLAY from leaf perspective",
                        "peer_group_type": "LEAF_TO_SPINE",
                        "bfd_enabled": True,
                        "ebgp_multihop": 0,
                        "send_community": True,
                        "send_community_extended": False,
                        "password": "UNDERLAY-secret",
                    },
                    "store_key": f"LEAF-TO-SPINE-UNDERLAY-PG-{topology_name}",
                },
            )

        # Overlay peer groups (always created)
        await self._create(
            kind="RoutingBGPPeerGroup",
            data={
                "payload": {
                    "name": f"{topology_name}-RR-CLIENTS-OVERLAY",
                    "description": f"{topology_name} OVERLAY route reflector clients",
                    "peer_group_type": "EVPN_RR_CLIENT",
                    "bfd_enabled": True,
                    "ebgp_multihop": 3,
                    "send_community": True,
                    "send_community_extended": True,
                    "route_reflector_client": False,
                    "password": "OVERLAY-secret",
                },
                "store_key": f"RR-CLIENTS-OVERLAY-PG-{topology_name}",
            },
        )

        await self._create(
            kind="RoutingBGPPeerGroup",
            data={
                "payload": {
                    "name": f"{topology_name}-RR-SERVERS-OVERLAY",
                    "description": f"{topology_name} OVERLAY route reflector servers",
                    "peer_group_type": "EVPN_RR_SERVER",
                    "bfd_enabled": True,
                    "ebgp_multihop": 3,
                    "send_community": True,
                    "send_community_extended": True,
                    "route_reflector_client": True,
                    "password": "OVERLAY-secret",
                },
                "store_key": f"RR-SERVERS-OVERLAY-PG-{topology_name}",
            },
        )

    async def create_autonomous_systems(self, scenario: str) -> None:
        """Create autonomous systems for spines, leafs, and overlay based on scenario."""
        topology_name = self.data.get("name")
        self.log.info(
            f"Creating autonomous systems for {topology_name} (scenario: {scenario})"
        )

        # Get the PRIVATE-ASN4 pool
        asn_pool = await self.client.get(
            kind=CoreNumberPool,
            name__value="PRIVATE-ASN4",
            raise_when_missing=True,
            branch=self.branch,
        )

        if scenario == "ebgp":
            # Create spine ASN using pool
            await self._create(
                kind="RoutingAutonomousSystem",
                data={
                    "payload": {
                        "asn": asn_pool,
                        "status": "active",
                        "description": f"{topology_name} SPINES ASN for eBGP UNDERLAY",
                        "location": self.client.store.get(
                            kind="LocationBuilding", key=self.data["name"]
                        ),
                    },
                    "store_key": f"SPINE-ASN-{topology_name}",
                },
            )

            # Create leaf ASNs (one per leaf and border_leaf for maximum flexibility)
            leaf_devices = [
                device
                for device in self.devices
                if device.role.value in ["leaf", "border_leaf"]
            ]
            for device in leaf_devices:
                await self._create(
                    kind="RoutingAutonomousSystem",
                    data={
                        "payload": {
                            "asn": asn_pool,
                            "status": "active",
                            "description": f"{topology_name} {device.name.value} ASN for eBGP UNDERLAY",
                            "location": self.client.store.get(
                                kind="LocationBuilding", key=self.data["name"]
                            ),
                        },
                        "store_key": f"LEAF-ASN-{device.name.value}",
                    },
                )

            # Create overlay ASN for iBGP EVPN using pool
            await self._create(
                kind="RoutingAutonomousSystem",
                data={
                    "payload": {
                        "asn": asn_pool,
                        "status": "active",
                        "description": f"{topology_name} OVERLAY ASN for iBGP EVPN over eBGP UNDERLAY",
                        "location": self.client.store.get(
                            kind="LocationBuilding", key=self.data["name"]
                        ),
                    },
                    "store_key": f"OVERLAY-ASN-{topology_name}",
                },
            )
        else:
            # OSPF scenario: only create overlay ASN for iBGP EVPN
            await self._create(
                kind="RoutingAutonomousSystem",
                data={
                    "payload": {
                        "asn": asn_pool,
                        "status": "active",
                        "description": f"{topology_name} OVERLAY ASN for iBGP EVPN over OSPF UNDERLAY",
                        "location": self.client.store.get(
                            kind="LocationBuilding", key=self.data["name"]
                        ),
                    },
                    "store_key": f"OVERLAY-ASN-{topology_name}",
                },
            )

    async def create_ebgp_underlay(self, loopback_name: str) -> None:
        """Create eBGP underlay sessions using interface-based peering (unidirectional from spine perspective)."""
        topology_name = self.data.get("name")
        self.log.info(
            f"Creating eBGP UNDERLAY for {topology_name} (interface-based peering, unidirectional)"
        )

        # Get peer groups
        server_pg = self.client.store.get(
            kind="RoutingBGPPeerGroup", key=f"SPINE-TO-LEAF-UNDERLAY-PG-{topology_name}"
        )

        # Get all ASNs for spines and leaves
        spine_asn_obj = self.client.store.get(
            kind="RoutingAutonomousSystem", key=f"SPINE-ASN-{topology_name}"
        )
        spine_asn = spine_asn_obj.id if spine_asn_obj else None

        # Build device lists
        leaf_devices = [
            device
            for device in self.devices
            if device.role.value in ["leaf", "border_leaf"]
        ]
        spine_devices = [
            device for device in self.devices if device.role.value == "spine"
        ]

        # Create BGP sessions batch - ONLY spine-to-leaf sessions (unidirectional)
        batch = await self.client.create_batch()

        # Create spine-to-leaf sessions only (BGP will handle bidirectional communication)
        for spine_device in spine_devices:
            for leaf_device in leaf_devices:
                leaf_asn_obj = self.client.store.get(
                    kind="RoutingAutonomousSystem",
                    key=f"LEAF-ASN-{leaf_device.name.value}",
                )
                leaf_asn = leaf_asn_obj.id if leaf_asn_obj else None
                session_name = (
                    f"{spine_device.name.value}-{leaf_device.name.value}".upper()
                )

                spine_bgp_data = {
                    "name": session_name,
                    "owner": self.data.get("provider"),
                    "device": spine_device.id,
                    "local_as": spine_asn,
                    "remote_as": leaf_asn,
                    "router_id": self.client.store.get(
                        key=f"{spine_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "local_ip": self.client.store.get(
                        key=f"{spine_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "remote_ip": self.client.store.get(
                        key=f"{leaf_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    # Associate with unnumbered interfaces like OSPF does
                    "interfaces": await self.client.filters(
                        kind="DcimInterface",
                        role__values=["unnumbered"],
                        device__name__value=spine_device.name.value,
                    ),
                    "session_type": "EXTERNAL",
                    "status": "active",
                }
                if server_pg:
                    spine_bgp_data["peer_group"] = server_pg.id
                else:
                    spine_bgp_data["role"] = "peering"

                spine_bgp = await self.client.create(
                    kind="ServiceBGP", data=spine_bgp_data
                )
                batch.add(task=spine_bgp.save, allow_upsert=True, node=spine_bgp)

        # Create leaf BGP sessions (one session per leaf-spine pair on leaf)
        for leaf_device in leaf_devices:
            leaf_asn_obj = self.client.store.get(
                kind="RoutingAutonomousSystem", key=f"LEAF-ASN-{leaf_device.name.value}"
            )
            leaf_asn = leaf_asn_obj.id if leaf_asn_obj else None
            for spine_device in spine_devices:
                session_name = (
                    f"{leaf_device.name.value}-{spine_device.name.value}".upper()
                )

                leaf_bgp_data = {
                    "name": session_name,
                    "owner": self.data.get("provider"),
                    "device": leaf_device.id,
                    "local_as": leaf_asn,
                    "remote_as": spine_asn,
                    "router_id": self.client.store.get(
                        key=f"{leaf_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "local_ip": self.client.store.get(
                        key=f"{leaf_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "remote_ip": self.client.store.get(
                        key=f"{spine_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    # Associate with unnumbered interfaces like OSPF does
                    "interfaces": await self.client.filters(
                        kind="DcimInterface",
                        role__values=["unnumbered"],
                        device__name__value=leaf_device.name.value,
                    ),
                    "session_type": "EXTERNAL",
                    "status": "active",
                }

                # Get client peer group for leaves
                client_pg = self.client.store.get(
                    kind="RoutingBGPPeerGroup",
                    key=f"LEAF-TO-SPINE-UNDERLAY-PG-{topology_name}",
                )
                if client_pg:
                    leaf_bgp_data["peer_group"] = client_pg.id
                else:
                    leaf_bgp_data["role"] = "peering"

                leaf_bgp = await self.client.create(
                    kind="ServiceBGP", data=leaf_bgp_data
                )
                batch.add(task=leaf_bgp.save, allow_upsert=True, node=leaf_bgp)

        # Execute the batch
        async for node, _ in batch.execute():
            self.log.info(
                f"- Created [{node.get_kind()}] {node.name.value} (eBGP underlay)"
            )

    async def create_ibgp_overlay(
        self, loopback_name: str, session_type: str = "overlay"
    ) -> None:
        """Create iBGP overlay sessions using peer groups.

        Args:
            loopback_name: Name of the loopback interface to use
            session_type: Type of session - "overlay" for traditional iBGP or "evpn" for EVPN
        """
        topology_name = self.data.get("name")

        # Always use the overlay ASN for iBGP overlay sessions
        overlay_asn = self.client.store.get(
            kind="RoutingAutonomousSystem", key=f"OVERLAY-ASN-{topology_name}"
        )
        asn_id = overlay_asn.id if overlay_asn else None

        # Get peer groups
        client_pg = self.client.store.get(
            kind="RoutingBGPPeerGroup", key=f"RR-CLIENTS-OVERLAY-PG-{topology_name}"
        )
        server_pg = self.client.store.get(
            kind="RoutingBGPPeerGroup", key=f"RR-SERVERS-OVERLAY-PG-{topology_name}"
        )

        # Filter devices by role
        leaf_devices = [
            device
            for device in self.devices
            if device.role.value in ["leaf", "border_leaf"]
        ]
        spine_devices = [
            device for device in self.devices if device.role.value == "spine"
        ]

        # Create BGP sessions batch
        batch = await self.client.create_batch()

        # Create spine-to-leaf sessions (RR server to clients)
        for spine_device in spine_devices:
            for leaf_device in leaf_devices:
                session_name = (
                    f"{spine_device.name.value}-{leaf_device.name.value}-EVPN".upper()
                )

                spine_bgp_data = {
                    "name": session_name,
                    "owner": self.data.get("provider"),
                    "device": spine_device.id,
                    "local_as": asn_id,
                    "remote_as": asn_id,
                    "router_id": self.client.store.get(
                        key=f"{spine_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "local_ip": self.client.store.get(
                        key=f"{spine_device.name.value}-{loopback_name}",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "remote_ip": self.client.store.get(
                        key=f"{leaf_device.name.value}-{loopback_name}",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "session_type": "INTERNAL",
                    "status": "active",
                }

                if server_pg:
                    spine_bgp_data["peer_group"] = server_pg.id
                else:
                    spine_bgp_data["role"] = "peering"

                spine_bgp = await self.client.create(
                    kind="ServiceBGP", data=spine_bgp_data
                )
                batch.add(task=spine_bgp.save, allow_upsert=True, node=spine_bgp)

        # Create leaf-to-spine sessions (RR clients to servers)
        for leaf_device in leaf_devices:
            for spine_device in spine_devices:
                session_name = (
                    f"{leaf_device.name.value}-{spine_device.name.value}-EVPN".upper()
                )

                leaf_bgp_data = {
                    "name": session_name,
                    "owner": self.data.get("provider"),
                    "device": leaf_device.id,
                    "local_as": asn_id,
                    "remote_as": asn_id,
                    "router_id": self.client.store.get(
                        key=f"{leaf_device.name.value}-loopback0",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "local_ip": self.client.store.get(
                        key=f"{leaf_device.name.value}-{loopback_name}",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "remote_ip": self.client.store.get(
                        key=f"{spine_device.name.value}-{loopback_name}",
                        kind=DcimVirtualInterface,
                    )
                    .ip_addresses[0]
                    .id,
                    "session_type": "INTERNAL",
                    "status": "active",
                }

                if client_pg:
                    leaf_bgp_data["peer_group"] = client_pg.id
                else:
                    leaf_bgp_data["role"] = "peering"

                leaf_bgp = await self.client.create(
                    kind="ServiceBGP", data=leaf_bgp_data
                )
                batch.add(task=leaf_bgp.save, allow_upsert=True, node=leaf_bgp)

        # Execute the batch
        async for node, _ in batch.execute():
            self.log.info(
                f"- Created [{node.get_kind()}] {node.name.value} (iBGP EVPN overlay)"
            )

    async def create_dual_loopbacks(self) -> None:
        """Create both underlay (loopback0) and VTEP (loopback1) loopback interfaces"""
        self.log.info(
            "Creating dual loopback interfaces: loopback0 (underlay) and loopback1 (VTEP)"
        )

        # Create loopback0 for underlay routing using standard loopback pool and role
        await self.create_loopback(
            "loopback0", "loopback_ip_pool", "loopback", "Underlay"
        )

        # Create loopback1 for VTEP/overlay using VTEP pool and role
        await self.create_loopback(
            "loopback1", "loopback-vtep_ip_pool", "loopback-vtep", "VTEP"
        )


class DCTopologyGenerator(InfrahubGenerator):
    """Generate topology."""

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        cleaned_data = clean_data(data)
        if isinstance(cleaned_data, dict):
            data = cleaned_data["TopologyDataCenter"][0]
        else:
            raise ValueError("clean_data() did not return a dictionary")
        # Determine scenario from data or default to OSPF
        # Check both 'scenario' and 'strategy' fields, and also description for EBGP indicator
        scenario = data.get("scenario", data.get("strategy", "ospf")).lower()
        # Map strategy values to scenario values
        if scenario in ["ebgp-ibgp", "ebgp"]:
            scenario = "ebgp"
        elif scenario in ["ospf-ibgp", "ospf"]:
            scenario = "ospf"
        else:
            scenario = "ospf"  # Default fallback

        self.logger.info(
            f"Using {scenario} scenario for topology generation (unnumbered P2P only)"
        )

        network_creator = DCTopologyCreator(
            client=self.client, log=self.logger, branch=self.branch, data=data
        )
        await network_creator.load_data()
        await network_creator.create_site()

        # Load technical_subnet as an object if it exists
        if data.get("technical_subnet"):
            technical_subnet_obj = await self.client.get(
                kind="IpamPrefix", id=data["technical_subnet"]["id"], branch=self.branch
            )
        else:
            technical_subnet_obj = None

        # Build subnets list for address pools
        subnets = []
        if data.get("management_subnet"):
            subnets.append(
                {
                    "type": "Management",
                    "prefix_id": data["management_subnet"]["id"],
                }
            )

        # Handle technical subnet - split for VTEP functionality
        if technical_subnet_obj:
            # Create management pool first
            await network_creator.create_address_pools(subnets)
            # Then use the new split loopback pools method
            await network_creator.create_split_loopback_pools(technical_subnet_obj)
        else:
            # Fallback to regular address pool creation if no technical subnet
            await network_creator.create_address_pools(subnets)
        await network_creator.create_L2_pool()
        await network_creator.create_devices()
        # Create default network segments for EVPN fabric connectivity
        await network_creator.create_oob_connections("management")
        await network_creator.create_oob_connections("console")

        # Create fabric peering with unnumbered interfaces only
        await network_creator.create_fabric_peering()

        # Create dual loopbacks: loopback0 (underlay) and loopback1 (VTEP)
        await network_creator.create_dual_loopbacks()

        if scenario == "ospf":
            # Traditional OSPF + iBGP scenario
            await network_creator.create_ospf_underlay()
            await network_creator.create_autonomous_systems(
                "ospf"
            )  # Create overlay ASN for BGP peer groups
            await network_creator.create_bgp_peer_groups(
                "ospf"
            )  # Create peer groups first
            await network_creator.create_ibgp_overlay("loopback1", "overlay")
        else:
            await network_creator.create_autonomous_systems("ebgp")
            await network_creator.create_bgp_peer_groups(
                "ebgp"
            )  # Create peer groups first
            await network_creator.create_ebgp_underlay("loopback0")
            await network_creator.create_ibgp_overlay("loopback1", "evpn")
