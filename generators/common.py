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

    async def _create(self, kind: str, data: dict, store_key: str = None) -> None:
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
            if store_key:
                self.client.store.set(key=store_key, node=obj)
        except (GraphQLError, ValidationError) as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def _create_core_groups(
        self,
        data: list,
    ):
        if data:
            await self._create_in_batch(kind="CoreStandardGroup", data_list=data)

    async def _create_ip_pools(self, topology_name: str, pools: dict) -> None:
        """Create objects of a specific kind and store in local store."""
        # namespace = await self.client.get(kind="IpamNamespace", name__value="default")
        for item in pools:
            if pools.get(item):
                data = {
                    "prefix": pools.get(item),
                    "description": f"{item} network pool for {topology_name}",
                    "is_pool": item in ["management", "customer"],
                    "status": "active",
                    "role": "supernet" if item == "customer" else item,
                    "location": self.client.store.get(key=topology_name).id,
                }
                await self._create(kind="IpamPrefix", data=data, store_key=item)
                self.client.log.info(self.client.store.get(item).id)
                if item in ["management", "technical"]:
                    data = {
                        "name": f"{topology_name}-{item}",
                        "description": f"Management network for {topology_name}",
                        "default_address_type": "IpamIPAddress",
                        "default_prefix_length": ipaddress.IPv4Network(
                            pools.get(item)
                        ).prefixlen,
                        "resources": [self.client.store.get(item).id],
                        "ip_namespace": {"id": "default"},
                    }
                    await self._create(
                        kind="CoreIPAddressPool",
                        data=data,
                        store_key=f"{topology_name}-{item}",
                    )
                else:
                    data = {
                        "name": f"{topology_name}-{item}",
                        "description": f"Management network for {topology_name}",
                        "default_address_type": "IpamIPAddress",
                        "default_prefix_length": 24,
                        "resources": [self.client.store.get(item).id],
                        "ip_namespace": {"id": "default"},
                    }
                    await self._create(
                        kind="CoreIPPrefixPool",
                        data=data,
                        store_key=f"{topology_name}-{item}",
                    )

    async def _create_devices(
        self,
        topology_name: str,
        data: list,
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        await self._create_in_batch(
            kind="DcimPhysicalDevice",
            data_list=[
                {
                    "payload": {
                        "name": f"{topology_name.lower()}-{device['role']}-{str(item).zfill(2)}",
                        "object_template": [device["template"]["template_name"]],
                        "device_type": device["device_type"]["id"],
                        "platform": device["device_type"]["platform"]["id"],
                        "status": "active",
                        "role": device["role"],
                        "location": self.client.store.get_by_hfid(
                            key=f"LocationBuilding__{topology_name}"
                        ).id,
                        "member_of_groups": [
                            self.client.store.get_by_hfid(
                                key=f"CoreStandardGroup__{device['device_type']['manufacturer']['name'].lower()}_{device['role']}"
                            )
                        ],
                    },
                    "store_key": f"{topology_name.lower()}-{device['role']}-{str(item).zfill(2)}",
                }
                for device in data
                for item in range(1, device["quantity"] + 1)
            ],
        )

    async def _create_oob_connections(
        self,
        topology_name: str,
        data: list,
        connection_type: str,
    ) -> None:
        """Create objects of a specific kind and store in local store."""

        device_key = "oob" if connection_type == "management" else "console"
        interfaces = {
            f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
                interface["name"]
                for interface in item["template"]["interfaces"]
                if interface["role"] == connection_type
            ]
            for item in data
            for j in range(item["quantity"])
            if item["role"] in ["oob", "leaf", "spine", "console"]
        }

        sources = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if device_key in key
        }
        destinations = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if key not in sources
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

    async def _create_peering_connections(self, topology_name: str, data: list) -> None:
        """Create objects of a specific kind and store in local store."""
        # name = f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}"
        interfaces = {
            f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
                interface["name"]
                for interface in item["template"]["interfaces"]
                if interface["role"] in ["leaf", "uplink"]
            ]
            for i, item in enumerate(data)
            for j in range(item["quantity"])
            if item["role"] in ["spine", "leaf"]
        }
        spines = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if "spine" in key
        }
        leafs = {
            key: sort_interface_list(value)
            for key, value in interfaces.items()
            if "leaf" in key
        }
        # print(spines)
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

    async def _create_underlay(self, topology_name: str, devices_ids: list) -> None:
        """Create underlay service and associate it to the respective switches."""
        await self._create(
            kind="ServiceOspfPeering",
            data={
                "name": f"{topology_name}-UNDERLAY",
                "description": f"{topology_name} OSPF underlay service",
                "area": 0,
                "status": "active",
                "devices": devices_ids,
            },
            store_key=f"underlay-{topology_name}",
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

    async def _create_loopback(self, data: list):
        pass

    async def _create_asn_pool(self, data: list):
        pass

    async def _create_vlan_pool(self, data: list):
        pass

    async def _create_prefix_pool(self, data: list):
        pass

    async def _create_ip_pool(self, data: list):
        pass

    async def _create_bgp_pool(self, data: list):
        pass
