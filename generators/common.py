import logging
from typing import Any

from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from infrahub_sdk.protocols import CoreIPAddressPool
from netutils.interface import sort_interface_list

from .schema_protocols import DcimConsoleInterface, DcimPhysicalInterface


def clean_data(data: Any) -> Any:
    """
    Recursively transforms the input data by extracting 'value', 'node', or 'edges' from dictionaries.

    Args:
        data: The input data to clean.

    Returns:
        The cleaned data with extracted values.
    """
    if isinstance(data, dict):
        dict_result = {}
        for key, value in data.items():
            if isinstance(value, dict):
                if value.get("value"):
                    dict_result[key] = value["value"]
                elif value.get("node"):
                    dict_result[key] = clean_data(value["node"])
                elif value.get("edges"):
                    dict_result[key] = clean_data(value["edges"])
                elif not value.get("value"):
                    dict_result[key] = None
                else:
                    dict_result[key] = clean_data(value)
            elif "__" in key:
                dict_result[key.replace("__", "")] = value
            else:
                dict_result[key] = clean_data(value)
        return dict_result
    if isinstance(data, list):
        list_result = []
        for item in data:
            if isinstance(item, dict) and item.get("node", None) is not None:
                list_result.append(clean_data(item["node"]))
                continue
            list_result.append(clean_data(item))
        return list_result
    return data


class TopologyCreator:
    """
    Handles the creation of topology elements in Infrahub.
    """

    def __init__(
        self, client: InfrahubClient, log: logging.Logger, branch: str, data: dict
    ):
        """
        Initialize the TopologyCreator.

        Args:
            client: InfrahubClient instance.
            log: Logger instance.
            branch: Branch name.
            data: Topology data dictionary.
        """
        self.client = client
        self.log = log
        self.branch = branch
        self.data = data
        self.devices: list = []

    async def _create_in_batch(
        self,
        kind: str,
        data_list: list,
    ) -> None:
        """
        Create objects of a specific kind and store in local store.

        Args:
            kind: The kind of object to create.
            data_list: List of data dictionaries for creation.
        """
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
        """
        Create an object of a specific kind and store in local store.

        Args:
            kind: The kind of object to create.
            data: The data dictionary for creation.
        """
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

        roles = list(
            set(f"{item['role']}s" for item in self.data["design"]["elements"])
        )
        manufacturers = list(
            set(
                f"{item['device_type']['manufacturer']['name'].lower().replace(' ', '_')}_{item['role']}"
                for item in self.data["design"]["elements"]
            )
        )
        await self.client.filters(
            kind="CoreStandardGroup",
            name__values=roles + manufacturers,
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
                "store_key": self.data["name"],
            },
        )

    async def create_address_pools(self, subnets: list[dict]) -> None:
        """Create objects of a specific kind and store in local store.

        Args:
            subnets: List of subnet dicts with 'type' and 'prefix_id' keys.
                    Format: [{"type": "Management", "prefix_id": "subnet_id"}, ...]
        """
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
                for pool in subnets
            ],
        )

    async def create_split_loopback_pools(self, technical_subnet_obj: Any) -> None:
        """Create separate IP address pools for underlay and VTEP loopbacks.

        Args:
            technical_subnet_obj: The technical subnet object to split
        """
        self.log.info("Creating split loopback pools for underlay and VTEP")

        # Split the technical subnet
        underlay_subnet_obj, vtep_subnet_obj = await self.split_technical_subnet(
            technical_subnet_obj
        )

        # Create address pools for both subnets
        subnets = [
            {
                "type": "Loopback",
                "prefix_id": underlay_subnet_obj.id,
            },
            {
                "type": "Loopback-VTEP",
                "prefix_id": vtep_subnet_obj.id,
            },
        ]

        await self.create_address_pools(subnets)

    async def split_technical_subnet(
        self, technical_subnet_obj: Any
    ) -> tuple[Any, Any]:
        """
        Split the technical subnet into two equal halves for underlay and VTEP loopbacks.

        Args:
            technical_subnet_obj: The technical subnet object to split

        Returns:
            tuple: (underlay_subnet_obj, vtep_subnet_obj)
        """
        import ipaddress

        # Get the prefix from the technical subnet
        original_prefix = ipaddress.ip_network(technical_subnet_obj.prefix.value)

        # Split into two equal subnets by adding 1 to the prefix length
        subnets = list(original_prefix.subnets(prefixlen_diff=1))

        if len(subnets) < 2:
            raise ValueError(f"Cannot split {original_prefix} - too small to split")

        underlay_subnet = subnets[0]  # First half for underlay
        vtep_subnet = subnets[1]  # Second half for VTEP

        self.log.info(f"Splitting {original_prefix} into:")
        self.log.info(f"  - Underlay: {underlay_subnet}")
        self.log.info(f"  - VTEP: {vtep_subnet}")

        # Create the underlay subnet object
        underlay_subnet_data = {
            "prefix": str(underlay_subnet),
            "status": "active",
            "role": "loopback",
            "description": f"{self.data.get('name')} Underlay Loopback Subnet",
        }

        underlay_subnet_obj = await self.client.create(
            kind="IpamPrefix", data=underlay_subnet_data, branch=self.branch
        )
        await underlay_subnet_obj.save(allow_upsert=True)

        # Create the VTEP subnet object
        vtep_subnet_data = {
            "prefix": str(vtep_subnet),
            "status": "active",
            "role": "loopback-vtep",
            "description": f"{self.data.get('name')} VTEP Loopback Subnet",
        }

        vtep_subnet_obj = await self.client.create(
            kind="IpamPrefix", data=vtep_subnet_data, branch=self.branch
        )
        await vtep_subnet_obj.save(allow_upsert=True)

        self.log.info(f"Created underlay subnet: {str(underlay_subnet)}")
        self.log.info(f"Created VTEP subnet: {str(vtep_subnet)}")

        return underlay_subnet_obj, vtep_subnet_obj

    async def create_L2_pool(self) -> None:
        """Create objects of a specific kind and store in local store."""
        await self._create(
            kind="CoreNumberPool",
            data={
                "payload": {
                    "name": f"{self.data.get('name')}-VLAN-POOL",
                    "description": f"{self.data.get('name')} VLAN Number Pool",
                    "node": "ServiceNetworkSegment",
                    "node_attribute": "vlan_id",
                    "start_range": 100,
                    "end_range": 4000,
                },
                # "store_key": f"{pool.get('type').lower()}_ip_pool",
            },
        )

    async def create_devices(self) -> None:
        self.log.info(f"Create devices for {self.data.get('name')}")
        # ... fetch device groups and templates logic ...
        physical_devices: list = []
        virtual_devices: list = []
        role_counters: dict = {}
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
                            key=f"CoreStandardGroup__{device['role']}s"
                        ),
                    ],
                    "primary_address": await self.client.allocate_next_ip_address(
                        resource_pool=self.client.store.get(
                            kind=CoreIPAddressPool, key="management_ip_pool"
                        ),
                        identifier=f"{name}-management",
                        data={"description": f"{name} Management IP"},
                    ),
                }
                # Append the constructed dictionary to respective lists

                device_entry = {"payload": payload, "store_key": name}
                (
                    virtual_devices
                    if "Virtual" in device["template"]["typename"]
                    else physical_devices
                ).append(device_entry)

        for kind, devices in [
            ("DcimPhysicalDevice", physical_devices),
            ("DcimVirtualDevice", virtual_devices),
        ]:
            if devices:
                await self._create_in_batch(kind=kind, data_list=devices)

        self.devices = [
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
                kind=(
                    DcimPhysicalInterface
                    if connection_type == "management"
                    else DcimConsoleInterface
                ),
                name__value=connection["source_interface"],
                device__name__value=connection["source"],
            )
            target_endpoint = await self.client.get(
                kind=(
                    DcimPhysicalInterface
                    if connection_type == "management"
                    else DcimConsoleInterface
                ),
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

    async def create_loopback(
        self,
        loopback_name: str,
        pool_key: str = "loopback_ip_pool",
        interface_role: str = "loopback",
        loopback_type: str = "Loopback",
    ) -> None:
        """Create loopback interfaces with specified IP pool, role, and type

        Args:
            loopback_name: Name of the loopback interface (e.g., 'loopback0', 'loopback1')
            pool_key: Key for the IP address pool to use (default: 'loopback_ip_pool')
            interface_role: Interface role for the schema (default: 'loopback')
            loopback_type: Type description for logging and descriptions (default: 'Loopback')
        """
        self.log.info(f"Creating {loopback_name} {loopback_type.lower()} interfaces")
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
                                    kind=CoreIPAddressPool, key=pool_key
                                ),
                                identifier=f"{device.name.value}-{loopback_name}",
                                data={
                                    "description": f"{device.name.value} {loopback_type} IP"
                                },
                            ),
                        ],
                        "role": interface_role,
                        "status": "active",
                        "description": f"{device.name.value} {loopback_name} {loopback_type} Interface",
                    },
                    "store_key": f"{device.name.value}-{loopback_name}",
                }
                for device in self.devices
                if device.role.value in ["spine", "leaf", "border_leaf", "edge"]
            ],
        )
