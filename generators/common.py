"""Common functions for the generators."""

import re
import ipaddress
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from netutils.interface import sort_interface_list


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


def set_speed(s):
    """Return speed in bps

    Args:
        s (str): intreface in the form of 1000(g)base-x

    Returns:
        int: Speed in bps
    """
    match = re.search(r"(\d+)(g|G)?(\w+)", s)
    speed = None
    if match:
        multiplier = 1000_000_000 if match.group(2) == "g" else 1000_000
        speed = int(match.group(1)) * multiplier
    return speed


class TopologyGenerator(InfrahubGenerator):
    """Generate topology."""

    async def _create_in_batch(
        self,
        kind: str,
        data_list: list,
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        batch = await self.client.create_batch()
        for data in data_list:
            try:
                obj = await self.client.create(kind=kind, data=data.get("payload"))
                batch.add(task=obj.save, allow_upsert=True, node=obj)
                if data.get("store_key"):
                    self.client.store.set(key=data.get("store_key"), node=obj)
            except GraphQLError as exc:
                self.client.log.debug(f"- Creation failed due to {exc}")
        try:
            async for node, _ in batch.execute():
                object_reference = (
                    " ".join(node.hfid) if node.hfid else node.display_label
                )
                self.client.log.info(
                    f"- Created [{node.get_kind()}] '{object_reference}'"
                    if object_reference
                    else f"- Created [{node.get_kind()}]"
                )
        except ValidationError as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def _create(self, kind: str, data: dict) -> None:
        """Create objects of a specific kind and store in local store."""
        try:
            obj = await self.client.create(kind=kind, data=data)
            await obj.save(allow_upsert=True)
            object_reference = " ".join(obj.hfid) if obj.hfid else obj.display_label
            self.client.log.info(
                f"- Created [{kind}] '{object_reference}'"
                if object_reference
                else f"- Created [{kind}]"
            )
            if data.get("store_key"):
                self.client.store.set(key=data.get("store_key"), node=obj)
        except (GraphQLError, ValidationError) as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def _create_core_groups(
        self,
        data: list,
    ):
        if data:
            await self._create_in_batch(kind="CoreStandardGroup", data_list=data)

    async def _create_devices(
        self,
        topology_name: str,
        data: list,
    ) -> None:
        """Create objects of a specific kind and store in local store."""

        data_list = []
        role_counters = {}

        # Populate the data_list with unique naming
        for device in data:
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

    async def _create_oob_connections(
        self,
        devices: list,
        templates: dict,
        connection_type: str,
    ) -> None:
        """Create objects of a specific kind and store in local store."""

        interfaces = {
            device.name.value: [
                interface["name"]
                for interface in templates[device._data["object_template"][0]]
                if interface["role"] == connection_type
            ]
            for device in devices
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

        for connection in connections:
            source_endpoint = await self.client.get(
                kind="DcimInterface",
                name__value=connection["source_interface"],
                device__name__value=connection["source"],
            )
            target_endpoint = await self.client.get(
                kind="DcimInterface",
                name__value=connection["destination_interface"],
                device__name__value=connection["target"],
            )
            self.logger.info(
                f"Creating {source_endpoint.hfid} -> {target_endpoint.hfid}"
            )

            source_endpoint.status.value = "active"
            source_endpoint.description.value = (
                f"Connection to {' -> '.join(target_endpoint.hfid)}"
            )
            source_endpoint.connector = target_endpoint.id
            target_endpoint.status.value = "active"
            target_endpoint.description.value = (
                f"Connection to {' -> '.join(source_endpoint.hfid)}"
            )

            await source_endpoint.save(allow_upsert=True)
            await target_endpoint.save(allow_upsert=True)

    async def _create_peering_connections(self, devices: list, templates: dict) -> None:
        """Create objects of a specific kind and store in local store."""
        # name = f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}"
        interfaces = {
            device.name.value: [
                interface["name"]
                for interface in templates[device._data["object_template"][0]]
                if interface["role"] in ["leaf", "uplink"]
            ]
            for device in devices
        }
        spines = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if "spine" in key and value
        }
        leafs = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if "leaf" in key and value
        }

        connections = [
            {
                "source": spine,
                "target": leaf,
                "source_interface": spine_interfaces.pop(0),
                "destination_interface": leaf_interfaces.pop(0),
            }
            for spine, spine_interfaces in spines.items()
            for leaf, leaf_interfaces in leafs.items()
        ]
        for connection in connections:
            source_endpoint = await self.client.get(
                kind="DcimInterface",
                name__value=connection["source_interface"],
                device__name__value=connection["source"],
            )
            target_endpoint = await self.client.get(
                kind="DcimInterface",
                name__value=connection["destination_interface"],
                device__name__value=connection["target"],
            )
            self.logger.info(
                f"Creating {source_endpoint.hfid} -> {target_endpoint.hfid}"
            )
            source_endpoint.status.value = "active"
            source_endpoint.description.value = (
                f"Peering connection to {' -> '.join(target_endpoint.hfid)}"
            )
            source_endpoint.role.value = "ospf-unnunbered"
            source_endpoint.connector = target_endpoint.id
            target_endpoint.status.value = "active"
            target_endpoint.description.value = (
                f"Peering connection to {' -> '.join(source_endpoint.hfid)}"
            )
            target_endpoint.role.value = "ospf-unnunbered"

            await source_endpoint.save(allow_upsert=True)
            await target_endpoint.save(allow_upsert=True)

    async def _create_ospf_underlay(self, topology_name: str, devices: list) -> None:
        """Create underlay service and associate it to the respective switches."""
        ospf_interfaces = await self.client.filters(
            kind="DcimInterface",
            role__values=["ospf-unnumbered", "loopback"],
            device__name__values=[device.name.value for device in devices],
        )
        self.client.log.info([interface.id for interface in ospf_interfaces])
        await self._create(
            kind="ServiceOSPFArea",
            data={
                "name": f"{topology_name}-UNDERLAY",
                "description": f"{topology_name} OSPF Underlay service",
                "area": 0,
                "status": "active",
                "namespace": {"id": "default"},
                "ospf_interfaces": [interface.id for interface in ospf_interfaces],
            },
            store_key=f"underlay-{topology_name}",
        )
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
                            resource_pool=self.client.store.get(key="loopback_ip_pool"),
                            identifier=f"{device.name.value}-loopback0",
                        ),
                    },
                    "store_key": f"underlay-{device.name.value}",
                }
                for device in devices
            ],
        )

    async def _create_overlay(self, topology_name: str, devices_ids: list) -> None:
        """Create underlay service and associate it to the respective switches."""
        await self._create(
            kind="ServiceBgpPeering",
            data={
                "name": f"{topology_name}-UNDERLAY",
                "description": f"{topology_name} iBGP overlay service",
                "asn": 65001,
                "status": "active",
                "devices": devices_ids,
            },
            store_key=f"overlay-{topology_name}",
        )

    async def _create_loopback(self, devices: list, loopback_name: str):
        await self._create_in_batch(
            kind="DcimVirtualInterface",
            data_list=[
                {
                    "payload": {
                        "name": loopback_name,
                        "device": self.client.store.get_by_hfid(
                            key=f"DcimPhysicalDevice__{device}"
                        ).id,
                        "ip_addresses": [
                            await self.client.allocate_next_ip_address(
                                resource_pool=self.client.store.get(
                                    key="loopback_ip_pool"
                                ),
                                identifier=f"{device}-{loopback_name}",
                                data={"description": f"{device} Loopback IP"},
                            ),
                        ],
                        "role": "loopback",
                        "status": "active",
                        "description": f"{device} {loopback_name} Interface",
                    },
                    "store_key": f"{device}-{loopback_name}",
                }
                for device in devices
            ],
        )

    async def _create_ip_pools(self, topology_name: str, pools: list) -> None:
        """Create objects of a specific kind and store in local store."""
        await self._create_in_batch(
            kind="CoreIPAddressPool",
            data_list=[
                {
                    "payload": {
                        "name": f"{topology_name}-{pool.get('type')}-pool",
                        "default_address_type": "IpamIPAddress",
                        "description": f"{pool.get('type')} IP Pool",
                        "ip_namespace": "default",
                        "resources": [pool.get("prefix_id")],
                    },
                    "store_key": f"{pool.get('type').lower()}_ip_pool",
                }
                for pool in pools
            ],
        )

    async def _create_segment_pool(self, topology_name: str, pools: list):
        """Create objects of a specific kind and store in local store."""
        await self._create_in_batch(
            kind="CoreIPAddressPool",
            data_list=[
                {
                    "payload": {
                        "name": f"{topology_name}-{pool.get('type')}-pool",
                        "default_address_type": "IpamIPAddress",
                        "description": f"{pool.get('type')} IP Pool",
                        "ip_namespace": "default",
                        "resources": [pool.get("prefix_id")],
                    },
                    "store_key": f"{pool.get('type').lower()}_ip_pool",
                }
                for pool in pools
            ],
        )

    async def _create_prefix_pool(self, data: list):
        pass
