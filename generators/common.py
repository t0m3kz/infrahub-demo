import logging

from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from infrahub_sdk.protocols import CoreIPAddressPool
from netutils.interface import sort_interface_list

from .schema_protocols import (
    DcimConsoleInterface,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ServiceBGPSession,
)


def clean_data(data):
    """
    Recursively transforms the input data
    by extracting 'value', 'node', or 'edges' from dictionaries.
    """
    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                if value.get("value"):
                    result[key] = value["value"]
                elif value.get("node"):
                    result[key] = clean_data(value["node"])
                elif value.get("edges"):
                    result[key] = clean_data(value["edges"])
                elif not value.get("value"):
                    result[key] = None
                else:
                    result[key] = clean_data(value)
            else:
                result[key] = clean_data(value)
        return result
    if isinstance(data, list):
        result = []
        for item in data:
            if isinstance(item, dict) and item.get("node", None) is not None:
                result.append(clean_data(item["node"]))
                continue
            result.append(clean_data(item))
        return result
    return data


class TopologyCreator:
    def __init__(
        self, client: InfrahubClient, log: logging.Logger, branch: str, data: dict
    ):
        self.client = client
        self.log = log
        self.branch = branch
        self.data = data
        self.devices = []

    async def _create_in_batch(
        self,
        kind: str,
        data_list: list,
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        batch = await self.client.create_batch()
        for data in data_list:
            try:
                obj = await self.client.create(
                    kind=kind, data=data.get("payload"), branch=self.branch
                )
                batch.add(task=obj.save, allow_upsert=True, node=obj)
                if data.get("store_key"):
                    self.client.store.set(
                        key=data.get("store_key"), node=obj, branch=self.branch
                    )
            except GraphQLError as exc:
                self.log.debug(f"- Creation failed due to {exc}")
        try:
            async for node, _ in batch.execute():
                object_reference = (
                    " ".join(node.hfid) if node.hfid else node.display_label
                )
                self.log.info(
                    f"- Created [{node.get_kind()}] {object_reference}"
                    if object_reference
                    else f"- Created [{node.get_kind()}]"
                )
        except ValidationError as exc:
            self.log.debug(f"- Creation failed due to {exc}")

    async def _create(self, kind: str, data: dict) -> None:
        """Create objects of a specific kind and store in local store."""
        try:
            obj = await self.client.create(
                kind=kind, data=data.get("payload"), branch=self.branch
            )
            await obj.save(allow_upsert=True)
            object_reference = " ".join(obj.hfid) if obj.hfid else obj.display_label
            self.log.info(
                f"- Created [{kind}] {object_reference}"
                if object_reference
                else f"- Created [{kind}]"
            )
            if data.get("store_key"):
                self.client.store.set(
                    key=data.get("store_key"), node=obj, branch=self.branch
                )
        except (GraphQLError, ValidationError) as exc:
            self.log.error(f"- Creation failed due to {exc}")

    async def load_data(self) -> None:
        """Load data and store in cache."""
        self.data.update(
            {
                "templates": {
                    item["template"]["template_name"]: item["template"]["interfaces"]
                    for item in self.data["design"]["elements"]
                }
            }
        )
        await self.client.filters(
            kind="CoreStandardGroup",
            name__values=list(
                set(
                    f"{item['device_type']['manufacturer']['name'].lower()}_{item['role']}"
                    for item in self.data["design"]["elements"]
                )
            ),
            branch=self.branch,
            populate_store=True,
        )
        # get the device templates
        await self.client.filters(
            kind="CoreObjectTemplate",
            template_name__values=list(
                set(
                    item["template"]["template_name"]
                    for item in self.data["design"]["elements"]
                )
            ),
            branch=self.branch,
            populate_store=True,
        )

    async def create_site(self) -> None:
        """Create site."""
        self.log.info(f"Create site {self.data.get('name')}")
        await self._create(
            kind="LocationBuilding",
            data={
                "payload": {
                    "name": self.data["name"],
                    "shortname": self.data["name"],
                    "parent": self.data["location"]["id"],
                },
                # "store_key": self.data["name"],
            },
        )

    async def create_address_pools(self) -> None:
        """Create objects of a specific kind and store in local store."""
        self.log.info("Creating address pools")
        await self._create_in_batch(
            kind="CoreIPAddressPool",
            data_list=[
                {
                    "payload": {
                        "name": f"{self.data.get('name')}-{pool.get('type')}-pool",
                        "default_address_type": "IpamIPAddress",
                        "description": f"{pool.get('type')} IP Pool",
                        "ip_namespace": "default",
                        "resources": [pool.get("prefix_id")],
                    },
                    "store_key": f"{pool.get('type', '').lower()}_ip_pool",
                }
                for pool in [
                    {
                        "type": "Management",
                        "prefix_id": self.data["management_subnet"]["id"],
                    },
                    {
                        "type": "Loopback",
                        "prefix_id": self.data["technical_subnet"]["id"],
                    },
                ]
            ],
        )

    async def create_L2_pool(self) -> None:
        """Create objects of a specific kind and store in local store."""
        await self._create(
            kind="CoreNumberPool",
            data={
                "payload": {
                    "name": f"{self.data.get('name')}-vlan-pool",
                    "description": f"{self.data.get('name')} L2 Segment Number Pool",
                    "node": "ServiceLayer2Network",
                    "node_attribute": "vlan",
                    "start_range": 100,
                    "end_range": 3500,
                },
                # "store_key": f"{pool.get('type').lower()}_ip_pool",
            },
        )

    async def create_devices(self) -> None:
        self.log.info(f"Create devices for {self.data.get('name')}")
        # ... fetch device groups and templates logic ...
        data_list = []
        role_counters = {}
        topology_name = self.data.get("name", "")

        # Populate the data_list with unique naming
        for device in self.data["design"]["elements"]:
            role = device["role"]

            # Initialize counter for this role if it doesn't exist
            role_counters.setdefault(role, 0)

            for i in range(1, device["quantity"] + 1):
                # Increment the counter for this role
                role_counters[role] += 1

                # Format the name string once per device
                name = f"{topology_name.lower()}-{role}-{str(role_counters[role]).zfill(2)}"

                # Construct the payload once per device
                payload = {
                    "name": name,
                    "object_template": [device["template"]["template_name"]],
                    "device_type": device["device_type"]["id"],
                    "platform": device["device_type"]["platform"]["id"],
                    "status": "active",
                    "role": role,
                    "location": self.client.store.get_by_hfid(
                        key=f"LocationBuilding__{topology_name}"
                    ).id,
                    "topology": self.data.get("id"),
                    "member_of_groups": [
                        self.client.store.get_by_hfid(
                            key=f"CoreStandardGroup__{device['device_type']['manufacturer']['name'].lower()}_{device['role']}"
                        )
                    ],
                }
                # Append the constructed dictionary to data_list
                data_list.append({"payload": payload, "store_key": name})

        await self._create_in_batch(
            kind="DcimPhysicalDevice",
            data_list=data_list,
        )

        self.devices: list = [
            self.client.store.get_by_hfid(f"DcimGenericDevice__{device[0]}")
            for device in self.client.store._branches[self.branch]
            ._hfids["DcimGenericDevice"]
            .keys()
        ]

    async def create_oob_connections(
        self,
        connection_type: str,
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        batch = await self.client.create_batch()
        interfaces: dict = {
            device.name.value: [
                interface["name"]
                for interface in self.data["templates"][
                    device._data["object_template"][0]
                ]
                if interface["role"] == connection_type
            ]
            for device in self.devices
        }

        device_key = "oob" if connection_type == "management" else "console"
        sources = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if device_key in key and value
        }

        destinations = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if key not in sources and value
        }

        connections = [
            {
                "source": source_device,
                "target": destination_device,
                "source_interface": source_interfaces.pop(0),
                "destination_interface": destination_interfaces.pop(0),
            }
            for source_device, source_interfaces in sources.items()
            for destination_device, destination_interfaces in destinations.items()
            if int(destination_device.split("-")[-1]) % 2
            == int(source_device.split("-")[-1]) % 2
        ]

        if connections:
            self.log.info(
                f"Create {connection_type} connections for {self.data.get('name')}"
            )

        for connection in connections:
            source_endpoint = await self.client.get(
                kind=DcimPhysicalInterface
                if connection_type == "management"
                else DcimConsoleInterface,
                name__value=connection["source_interface"],
                device__name__value=connection["source"],
            )
            target_endpoint = await self.client.get(
                kind=DcimPhysicalInterface
                if connection_type == "management"
                else DcimConsoleInterface,
                name__value=connection["destination_interface"],
                device__name__value=connection["target"],
            )

            source_endpoint.status.value = "active"
            source_endpoint.description.value = (
                f"Connection to {' -> '.join(target_endpoint.hfid or [])}"
            )
            source_endpoint.connector = target_endpoint.id  # type: ignore
            target_endpoint.status.value = "active"
            target_endpoint.description.value = (
                f"Connection to {' -> '.join(source_endpoint.hfid or [])}"
            )
            batch.add(
                task=source_endpoint.save, allow_upsert=True, node=source_endpoint
            )
            batch.add(
                task=target_endpoint.save, allow_upsert=True, node=target_endpoint
            )
        try:
            async for node, _ in batch.execute():
                self.log.info(
                    f"- Created [{node.get_kind()}] {node.description.value} from {' -> '.join(node.hfid)}"
                )

        except ValidationError as exc:
            self.log.debug(f"- Creation failed due to {exc}")

    async def create_fabric_peering(self) -> None:
        """Create objects of a specific kind and store in local store."""
        batch = await self.client.create_batch()
        interfaces: dict = {
            device.name.value: [
                interface["name"]
                for interface in self.data["templates"][
                    device._data["object_template"][0]
                ]
                if interface["role"] in ["leaf", "uplink"]
            ]
            for device in self.devices
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
                f"Create fabric peering connections for {self.data.get('name')}"
            )

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
            source_endpoint.role.value = "ospf-unnumbered"
            source_endpoint.connector = target_endpoint.id  # type: ignore
            target_endpoint.status.value = "active"
            target_endpoint.description.value = (
                f"Peering connection to {' -> '.join(source_endpoint.hfid or [])}"
            )
            target_endpoint.role.value = "ospf-unnumbered"

            batch.add(
                task=source_endpoint.save, allow_upsert=True, node=source_endpoint
            )
            batch.add(
                task=target_endpoint.save, allow_upsert=True, node=target_endpoint
            )

        try:
            async for node, _ in batch.execute():
                self.log.info(
                    f"- Created [{node.get_kind()}] {node.description.value} from {' -> '.join(node.hfid)}"
                )

        except ValidationError as exc:
            self.log.debug(f"- Creation failed due to {exc}")

    async def create_loopback(self, loopback_name: str) -> None:
        """Create loopback interfaces"""
        self.log.info(f"Creating {loopback_name} interfaces")
        await self._create_in_batch(
            kind="DcimVirtualInterface",
            data_list=[
                {
                    "payload": {
                        "name": loopback_name,
                        "device": device.id,
                        "ip_addresses": [
                            await self.client.allocate_next_ip_address(
                                resource_pool=self.client.store.get(
                                    kind=CoreIPAddressPool, key="loopback_ip_pool"
                                ),
                                identifier=f"{device.name.value}-{loopback_name}",
                                data={
                                    "description": f"{device.name.value} Loopback IP"
                                },
                            ),
                        ],
                        "role": "loopback",
                        "status": "active",
                        "description": f"{device.name.value} {loopback_name} Interface",
                    },
                    "store_key": f"{device.name.value}-{loopback_name}",
                }
                for device in self.devices
                if device.role.value in ["spine", "leaf"]
            ],
        )

    async def create_ospf_underlay(self) -> None:
        """Create underlay service and associate it to the respective switches."""
        topology_name = self.data.get("name")
        ospf_interfaces = await self.client.filters(
            kind="DcimInterface",
            role__values=["ospf-unnumbered", "loopback"],
            device__name__values=[
                device.name.value
                for device in self.devices
                if device.role.value in ["spine", "leaf"]
            ],
        )
        self.log.info(f"Creating OSPF underlay for {topology_name}")
        await self._create(
            kind="ServiceOSPFArea",
            data={
                "payload": {
                    "name": f"{topology_name}-UNDERLAY",
                    "description": f"{topology_name} OSPF Underlay service",
                    "area": 0,
                    "status": "active",
                    "namespace": {"id": "default"},
                    "ospf_interfaces": [interface.id for interface in ospf_interfaces],
                },
                "store_key": f"underlay-{topology_name}",
            },
        )
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
                            kind="ServiceOSPFArea", key=f"underlay-{topology_name}"
                        ),
                        "device": device.id,
                        "status": "active",
                        "router_id": await self.client.allocate_next_ip_address(
                            resource_pool=self.client.store.get(
                                key="loopback_ip_pool", kind=CoreIPAddressPool
                            ),
                            identifier=f"{device.name.value}-loopback0",
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

    async def create_ibgp_overlay(self, loopback_name: str) -> None:
        """Create iBGP sessions."""
        topology_name = self.data.get("name")
        self.log.info(f"Creating iBGP overlay for {topology_name}")
        # Get or create ASN
        asn = await self.client.get(
            kind="ServiceAutonomousSystem",
            name__value=f"{topology_name}-OVERLAY",
            populate_store=True,
            raise_when_missing=False,
        )
        if not asn:
            asn_pool = await self.client.get(
                kind="CoreNumberPool",
                name__value="PRIVATE-ASN4",
                raise_when_missing=False,
                branch=self.branch,
            )
            await self._create(
                kind="ServiceAutonomousSystem",
                data={
                    "payload": {
                        "name": f"{topology_name}-OVERLAY",
                        "description": f"{topology_name} iBGP Overlay service",
                        "asn": asn_pool,
                        "status": "active",
                    },
                    "store_key": f"underlay-{topology_name}",
                },
            )
        asn_id = (
            asn.id if asn else self.client.store.get(f"underlay-{topology_name}").id
        )
        # Filter devices by role
        leaf_devices: list[DcimPhysicalInterface] = [
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
                    obj = await self.client.create(
                        kind=ServiceBGPSession,
                        data={
                            "name": f"{source_device.name.value}-{target_device.name.value}",
                            "local_as": asn_id,
                            "remote_as": asn_id,
                            "device": source_device.id,
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
                            "description": f"{source_device.name.value} -> {target_device.name.value} iBGP Session",
                            "role": "peering",
                        },
                    )
                    batch.add(task=obj.save, allow_upsert=True, node=obj)
        # Execute the batch
        try:
            async for node, _ in batch.execute():
                self.log.info(f"- Created [{node.get_kind()}] {node.description.value}")
        except ValidationError as exc:
            self.log.debug(f"- Creation failed due to {exc}")
