"""Infrastructure generator."""


from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool, CoreIPPrefixPool, CoreNumberPool
from netutils.interface import sort_interface_list

from .common import TopologyCreator, clean_data
from .schema_protocols import DcimPhysicalInterface, DcimVirtualInterface


class DCTopologyCreator(TopologyCreator):
    """Create data center topology."""

    async def create_fabric_peering_and_p2p(
        self,
    ) -> None:
        """
        Create fabric peering connections for a single site (unnumbered only).
        """
        batch = await self.client.create_batch()
        all_devices = self.devices

        interfaces: dict = {
            device.name.value: [
                interface["name"]
                for interface in self.data["templates"][
                    device._data["object_template"][0]
                ]
                if interface["role"] in ["leaf", "uplink"]
            ]
            for device in all_devices
        }
        spines: dict = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if "spine" in key and value
        }
        leafs: dict = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if "leaf" in key and value
        }

        connections: list = [
            {
                "source": spine,
                "target": leaf,
                "source_interface": spine_interfaces.pop(0),
                "destination_interface": leaf_interfaces.pop(0),
            }
            for spine, spine_interfaces in spines.items()
            for leaf, leaf_interfaces in leafs.items()
        ]

        if connections:
            self.log.info(
                f"Create fabric peering connections for {self.data.get('name')} (unnumbered P2P only)"
            )

        # Set interface role for unnumbered only
        interface_role = "ip_unnumbered"

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
                    "description": f"{topology_name} OSPF Underlay service",
                    "area": 0,
                    "status": "active",
                    # "namespace": {"id": "default"},
                    # "ospf_interfaces": [interface.id for interface in ospf_interfaces],
                },
                "store_key": f"underlay-{topology_name}",
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
                        "name": f"{device.name.value.upper()}-OSPF",
                        "description": f"{device.name.value} OSPF UNDERLAY",
                        "area": self.client.store.get(
                            kind="RoutingOSPFArea",
                            key=[f"{topology_name}-UNDERLAY", "0"],
                        ),
                        "version": "ospfv3",
                        "device": device.id,
                        "status": "active",
                        "router_id": await self.client.allocate_next_ip_address(
                            resource_pool=self.client.store.get(
                                key="loopback_ip_pool", kind=CoreIPAddressPool
                            ),
                            identifier=f"{device.name.value}-loopback0",
                        ),
                        "ospf_interface": await self.client.filters(
                            kind="DcimInterface",
                            role__values=["ip_unnumbered", "loopback"],
                            device__name__value=device.name.value,
                        ),
                    },
                    "store_key": f"underlay-{device.name.value}",
                }
                for device in self.devices
                if device.role.value in ["spine", "leaf"]
            ],
        )

        # self.client.log.info(self.data)

        # ... any additional steps ...

    async def create_ebgp_autonomous_systems(self) -> None:
        """Create eBGP autonomous systems for spines, leafs, and overlay."""
        topology_name = self.data.get("name")
        self.log.info(f"Creating eBGP autonomous systems for {topology_name}")

        # Get the PRIVATE-ASN4 pool
        asn_pool = await self.client.get(
            kind=CoreNumberPool,
            name__value="PRIVATE-ASN4",
            raise_when_missing=True,
            branch=self.branch,
        )

        # Create spine ASN using pool
        await self._create(
            kind="RoutingAutonomousSystem",
            data={
                "payload": {
                    "name": f"{topology_name}-SPINE-AS",
                    "asn": asn_pool,
                    "status": "active",
                    "description": f"{topology_name} Spine ASN for eBGP underlay",
                },
                "store_key": f"spine-asn-{topology_name}",
            },
        )

        # Create leaf ASNs (one per leaf for maximum flexibility)
        leaf_devices = [
            device for device in self.devices if device.role.value == "leaf"
        ]
        for device in leaf_devices:
            await self._create(
                kind="RoutingAutonomousSystem",
                data={
                    "payload": {
                        "name": f"{topology_name}-{device.name.value.upper()}-AS",
                        "asn": asn_pool,
                        "status": "active",
                        "description": f"{topology_name} {device.name.value} ASN for eBGP underlay",
                    },
                    "store_key": f"leaf-asn-{device.name.value}",
                },
            )

        # Create overlay ASN for iBGP EVPN using pool
        await self._create(
            kind="RoutingAutonomousSystem",
            data={
                "payload": {
                    "name": f"{topology_name}-OVERLAY-AS",
                    "asn": asn_pool,
                    "status": "active",
                    "description": f"{topology_name} Overlay ASN for iBGP EVPN",
                },
                "store_key": f"overlay-asn-{topology_name}",
            },
        )

    async def create_p2p_interfaces(self) -> None:
        """Create P2P IP addresses for eBGP underlay interfaces."""
        topology_name = self.data.get("name")
        self.log.info(f"Creating P2P IP addresses for {topology_name}")

        # Get P2P IP prefix pool - must exist for numbered P2P
        p2p_pool = self.client.store.get(
            kind="CoreIPPrefixPool", key=f"p2p-pool-{topology_name}"
        )

        # Get all connected interface pairs
        core_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            role__value="core",
            connector__isnull=False,
        )

        # Create P2P subnets for connected interfaces
        batch = await self.client.create_batch()
        processed_pairs = set()

        for interface in core_interfaces:
            if interface.connector and str(interface.id) not in processed_pairs:
                # Get the connected interface
                connected_interface = await self.client.get(
                    kind=DcimPhysicalInterface,
                    id=interface.connector.id,
                )

                # Allocate a /31 subnet for this P2P link
                # Handle different pool types - cast to CoreIPPrefixPool if needed
                if hasattr(p2p_pool, "id") and not isinstance(
                    p2p_pool, CoreIPPrefixPool
                ):
                    # It's an InfrahubNode, need to get it as CoreIPPrefixPool
                    pool_for_allocation = await self.client.get(
                        kind=CoreIPPrefixPool,
                        id=p2p_pool.id,
                        branch=self.branch,
                    )
                else:
                    # It's already a CoreIPPrefixPool
                    pool_for_allocation = p2p_pool

                # Create a unique identifier for the P2P link including interface names
                interface_names = sorted(
                    [
                        f"{interface.device.name}-{interface.name}",
                        f"{connected_interface.device.name}-{connected_interface.name}",
                    ]
                )
                p2p_link_id = f"p2p-{interface_names[0]}-{interface_names[1]}"

                p2p_subnet = await self.client.allocate_next_ip_prefix(
                    resource_pool=pool_for_allocation,  # type: ignore
                    identifier=p2p_link_id,
                    prefix_length=31,
                    data={
                        "description": f"P2P {interface.device.name} {interface.name} and {connected_interface.device.name} {connected_interface.name}",
                        "status": "active",
                    },
                )

                # Calculate the two IP addresses from the /31 subnet and create them manually
                import ipaddress

                network = ipaddress.IPv4Network(p2p_subnet.prefix.value, strict=False)
                ip_addresses = list(network.hosts())

                # Get the default namespace
                default_namespace = await self.client.get(
                    kind="IpamNamespace",
                    name__value="default",
                    branch=self.branch,
                )

                # Create first IP address for the first interface with /31 subnet mask
                ip1 = await self.client.create(
                    kind="IpamIPAddress",
                    data={
                        "address": f"{ip_addresses[0]}/31",
                        "description": f"{interface.device.name} {interface.name} P2P IP",
                        "ip_namespace": default_namespace.id,
                    },
                    identifier=f"{p2p_link_id}-{interface.device.name}-{interface.name}",
                )
                await ip1.save(allow_upsert=True)

                # Create second IP address for the connected interface with /31 subnet mask
                ip2 = await self.client.create(
                    kind="IpamIPAddress",
                    data={
                        "address": f"{ip_addresses[1]}/31",
                        "description": f"{connected_interface.device.name} {connected_interface.name} P2P IP",
                        "ip_namespace": default_namespace.id,
                    },
                    identifier=f"{p2p_link_id}-{connected_interface.device.name}-{connected_interface.name}",
                )
                await ip2.save(allow_upsert=True)

                # Associate IPs with interfaces
                interface.ip_addresses.add(ip1.id)
                connected_interface.ip_addresses.add(ip2.id)

                batch.add(task=interface.save, allow_upsert=True, node=interface)
                batch.add(
                    task=connected_interface.save,
                    allow_upsert=True,
                    node=connected_interface,
                )

                # Mark both interfaces as processed
                processed_pairs.add(str(interface.id))
                processed_pairs.add(str(connected_interface.id))

        async for node, _ in batch.execute():
            self.log.info(f"- Assigned P2P IP to [{node.get_kind()}] {node.name.value}")

    async def create_ebgp_underlay(self) -> None:
        """Create eBGP underlay sessions between spines and leafs."""
        topology_name = self.data.get("name")
        self.log.info(
            f"Creating eBGP underlay for {topology_name} (unnumbered interfaces)"
        )

        # Get ASNs
        spine_asn = self.client.store.get(
            kind="RoutingAutonomousSystem", key=f"spine-asn-{topology_name}"
        )

        # Get all connected interface pairs for unnumbered interfaces
        core_interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            role__value="ip_unnumbered",
            connector__isnull=False,
        )

        batch = await self.client.create_batch()
        processed_pairs = set()

        for interface in core_interfaces:
            if interface.connector and str(interface.id) not in processed_pairs:
                # Get the connected interface
                connected_interface = await self.client.get(
                    kind=DcimPhysicalInterface,
                    id=interface.connector.id,
                )

                # Determine which is spine and which is leaf
                if "spine" in str(interface.device.name).lower():
                    spine_interface = interface
                    leaf_interface = connected_interface
                elif "spine" in str(connected_interface.device.name).lower():
                    spine_interface = connected_interface
                    leaf_interface = interface
                else:
                    continue  # Skip if neither is spine

                # Get leaf ASN
                leaf_asn = self.client.store.get(
                    kind="RoutingAutonomousSystem",
                    key=f"leaf-asn-{leaf_interface.device.name}",
                )

                # Create BGP session from spine to leaf
                spine_bgp_data = {
                    "name": f"{spine_interface.device.name}-{leaf_interface.device.name}-UNDERLAY",
                    "description": f"eBGP underlay session {spine_interface.device.name} -> {leaf_interface.device.name}",
                    "session_type": "EXTERNAL",
                    "device": spine_interface.device.id,
                    "local_as": spine_asn.id,
                    "remote_as": leaf_asn.id,
                    "status": "active",
                }

                # No IP addresses needed for unnumbered P2P

                spine_bgp = await self.client.create(
                    kind="ServiceBGP",
                    data=spine_bgp_data,
                )

                # Create BGP session from leaf to spine
                leaf_bgp_data = {
                    "name": f"{leaf_interface.device.name}-{spine_interface.device.name}-UNDERLAY",
                    "description": f"eBGP underlay session {leaf_interface.device.name} -> {spine_interface.device.name}",
                    "session_type": "EXTERNAL",
                    "device": leaf_interface.device.id,
                    "local_as": leaf_asn.id,
                    "remote_as": spine_asn.id,
                    "status": "active",
                }

                # No IP addresses needed for unnumbered P2P

                leaf_bgp = await self.client.create(
                    kind="ServiceBGP",
                    data=leaf_bgp_data,
                )

                batch.add(task=spine_bgp.save, allow_upsert=True, node=spine_bgp)
                batch.add(task=leaf_bgp.save, allow_upsert=True, node=leaf_bgp)

                # Mark both interfaces as processed
                processed_pairs.add(str(interface.id))
                processed_pairs.add(str(connected_interface.id))

        async for node, _ in batch.execute():
            self.log.info(f"- Created [{node.get_kind()}] {node.description.value}")

    async def create_ibgp_overlay(
        self, loopback_name: str, session_type: str = "overlay"
    ) -> None:
        """Create iBGP overlay sessions.

        Args:
            loopback_name: Name of the loopback interface to use
            session_type: Type of session - "overlay" for traditional iBGP or "evpn" for EVPN
        """
        topology_name = self.data.get("name")

        if session_type == "evpn":
            self.log.info(f"Creating iBGP EVPN overlay for {topology_name}")
            # Get existing overlay ASN for EVPN (created by eBGP scenario)
            overlay_asn = self.client.store.get(
                kind="RoutingAutonomousSystem", key=f"overlay-asn-{topology_name}"
            )
            asn_id = overlay_asn.id
            session_suffix = "EVPN"
        else:
            self.log.info(f"Creating iBGP overlay for {topology_name}")
            # Create ASN for traditional overlay scenario
            asn_pool = await self.client.get(
                kind="CoreNumberPool",
                name__value="PRIVATE-ASN4",
                raise_when_missing=False,
                branch=self.branch,
            )
            await self._create(
                kind="RoutingAutonomousSystem",
                data={
                    "payload": {
                        "name": f"{topology_name}-OVERLAY",
                        "asn": asn_pool,
                        "status": "active",
                    },
                    "store_key": f"underlay-{topology_name}",
                },
            )
            asn_id = self.client.store.get(f"underlay-{topology_name}").id
            session_suffix = ""

        # Filter devices by role
        leaf_devices = [
            device for device in self.devices if device.role.value == "leaf"
        ]
        spine_devices = [
            device for device in self.devices if device.role.value == "spine"
        ]

        # Create BGP sessions batch
        batch = await self.client.create_batch()

        # Create bidirectional BGP sessions
        for source_devices, target_devices in [
            (leaf_devices, spine_devices),
            (spine_devices, leaf_devices),
        ]:
            for source_device in source_devices:
                for target_device in target_devices:
                    # Build session name with optional suffix
                    session_name = (
                        f"{source_device.name.value}-{target_device.name.value}"
                    )
                    if session_suffix:
                        session_name += f"-{session_suffix}"

                    # Build description based on session type
                    if session_type == "evpn":
                        if "spine" in source_device.name.value.lower():
                            description = f"iBGP EVPN route reflector session {source_device.name.value} -> {target_device.name.value}"
                        else:
                            description = f"iBGP EVPN client session {source_device.name.value} -> {target_device.name.value}"
                    else:
                        description = f"{source_device.name.value} -> {target_device.name.value} iBGP Session"

                    obj = await self.client.create(
                        kind="ServiceBGP",
                        data={
                            "name": session_name,
                            "local_as": asn_id,
                            "remote_as": asn_id,
                            "device": source_device.id,
                            "router_id": self.client.store.get(
                                key=f"{source_device.name.value}-{loopback_name}",
                                kind=DcimVirtualInterface,
                                raise_when_missing=True,
                            )
                            .ip_addresses[0]
                            .id,
                            "local_ip": self.client.store.get(
                                key=f"{source_device.name.value}-{loopback_name}",
                                kind=DcimVirtualInterface,
                                raise_when_missing=True,
                            )
                            .ip_addresses[0]
                            .id,
                            "remote_ip": self.client.store.get(
                                key=f"{target_device.name.value}-{loopback_name}",
                                kind=DcimVirtualInterface,
                                raise_when_missing=True,
                            )
                            .ip_addresses[0]
                            .id,
                            "session_type": "INTERNAL",
                            "status": "active",
                            "description": description,
                            "role": "peering" if session_type != "evpn" else None,
                        },
                    )
                    batch.add(task=obj.save, allow_upsert=True, node=obj)

        # Execute the batch
        async for node, _ in batch.execute():
            self.log.info(f"- Created [{node.get_kind()}] {node.description.value}")


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

        # Check the enable_ip_unnumbered flag from schema (inverted logic - True means use unnumbered)
        enable_ip_unnumbered = data.get(
            "enable_ip_unnumbered", True
        )  # Default to True (unnumbered)
        use_numbered_p2p = not enable_ip_unnumbered  # Invert the logic

        # Also check description for backward compatibility
        description = data.get("description", "").upper()
        if "EBGP" in description:
            scenario = "ebgp"
        if "NUMBERED" in description:
            use_numbered_p2p = True

        # Map strategy values to scenario values
        if scenario in ["ebgp-ibgp", "ebgp"]:
            scenario = "ebgp"
        elif scenario in ["ospf-ibgp", "ospf"]:
            scenario = "ospf"
        else:
            scenario = "ospf"  # Default fallback

        self.logger.info(
            f"Using {scenario} scenario for topology generation ({'numbered' if use_numbered_p2p else 'unnumbered'} P2P)"
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

        if technical_subnet_obj:
            # For eBGP scenario with numbered P2P, split the technical subnet
            if scenario == "ebgp" and use_numbered_p2p:
                try:
                    (
                        loopback_subnet_id,
                        _,
                    ) = await network_creator.split_technical_subnet(
                        technical_subnet_obj, str(data.get("name", "unknown"))
                    )
                    subnets.append(
                        {
                            "type": "Loopback",
                            "prefix_id": loopback_subnet_id,
                        }
                    )
                except Exception as e:
                    self.logger.error(
                        f"Failed to split technical subnet: {e} - using original subnet"
                    )
                    # Use the loaded object after transformation
                    subnets.append(
                        {
                            "type": "Loopback",
                            "prefix_id": technical_subnet_obj.id,
                        }
                    )
            else:
                # For non-eBGP scenarios or unnumbered P2P, use the original technical subnet
                subnets.append(
                    {
                        "type": "Loopback",
                        "prefix_id": technical_subnet_obj.id,
                    }
                )

        await network_creator.create_address_pools(subnets)
        await network_creator.create_L2_pool()
        await network_creator.create_devices()
        await network_creator.create_oob_connections("management")
        await network_creator.create_oob_connections("console")

        # Create fabric peering with scenario-specific interface roles
        await network_creator.create_fabric_peering_and_p2p()
        await network_creator.create_loopback("loopback0")

        if scenario == "ospf":
            # Traditional OSPF + iBGP scenario
            await network_creator.create_ospf_underlay()
            await network_creator.create_ibgp_overlay("loopback0", "overlay")

        elif scenario == "ebgp":
            # eBGP multi-AS + iBGP EVPN scenario
            await network_creator.create_ebgp_autonomous_systems()
            await network_creator.create_ebgp_underlay()
            await network_creator.create_ibgp_overlay("loopback0", "evpn")

        else:
            self.logger.warning(f"Unknown scenario '{scenario}', falling back to OSPF")
            await network_creator.create_ospf_underlay()
            await network_creator.create_ibgp_overlay("loopback0", "overlay")
