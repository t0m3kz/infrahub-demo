"""Infrastructure generator."""

from infrahub_sdk.exceptions import ValidationError
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.protocols import CoreIPAddressPool
from netutils.interface import sort_interface_list

from .common import TopologyCreator, clean_data
from .schema_protocols import (
    DcimPhysicalInterface,
    DcimVirtualInterface,
    ServiceBGPSession,
)


class DCTopologyCreator(TopologyCreator):
    """Create data center topology."""

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
        # asn = await self.client.get(
        #     kind="ServiceAutonomousSystem",
        #     name__value=f"{topology_name}-OVERLAY",
        #     populate_store=True,
        #     raise_when_missing=False,
        # )
        # if not asn:
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
        asn_id = self.client.store.get(f"underlay-{topology_name}").id
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


class DCTopologyGenerator(InfrahubGenerator):
    """Generate topology."""

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        cleaned_data = clean_data(data)
        if isinstance(cleaned_data, dict):
            data = cleaned_data["TopologyDataCenter"][0]
        else:
            raise ValueError("clean_data() did not return a dictionary")
        network_creator = DCTopologyCreator(
            client=self.client, log=self.logger, branch=self.branch, data=data
        )
        await network_creator.load_data()
        await network_creator.create_site()
        await network_creator.create_address_pools()
        await network_creator.create_L2_pool()
        await network_creator.create_devices()
        # self.log.info(self.client.store._branches[self.branch].__dict__)
        await network_creator.create_oob_connections("management")
        await network_creator.create_oob_connections("console")
        await network_creator.create_fabric_peering()
        await network_creator.create_loopback("loopback0")
        await network_creator.create_ospf_underlay()
        await network_creator.create_ibgp_overlay("loopback0")
