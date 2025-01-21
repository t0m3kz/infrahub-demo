"""Infrastructure generator."""

import re
import logging
from infrahub_sdk.generator import InfrahubGenerator
from infrahub_sdk.exceptions import GraphQLError, ValidationError


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


class DCTopologyGenerator(InfrahubGenerator):
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
                object_reference = node.hfid[0] if node.hfid else node.display_label
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
            if store_key:
                self.client.store.set(key=store_key, node=obj)
        except (GraphQLError, ValidationError) as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

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
                    "location": self.store.get(key=topology_name).id,
                }
                await self._create(
                    kind="IpamPrefix", data=data, store_key=pools.get(item)
                )

    async def _create_devices(
        self, topology_name: str, data: list, topology: str
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        devices = {"DcimDevice": [], "DcimFirewall": []}

        for device in data:
            for item in range(1, device["quantity"] + 1):
                site = topology_name.lower()
                role = device["role"]
                manufacturer = device["device_type"]["manufacturer"]["name"].lower()
                device_name = f"{site}-{role}-{str(item).zfill(2)}"
                _device = {
                    "payload": {
                        "name": device_name,
                        "device_type": device["device_type"]["id"],
                        # Here we're using hfid to get platform and location from store
                        "platform": device["device_type"]["platform"]["id"],
                        "status": "active",
                        "role": role if role != "firewall" else "edge_firewall",
                        "location": self.store.get(key=topology_name).id,
                        "member_of_groups": [
                            self.store.get_by_hfid(
                                key=f"CoreStandardGroup__{manufacturer}_{role}"
                            ).id
                        ],
                        "topology": topology,
                    },
                    "store_key": device_name,
                }
                # create interface list for all devices
                if role == "firewall":
                    devices["DcimFirewall"].append(_device)
                else:
                    devices["DcimDevice"].append(_device)
        for kind, device_list in devices.items():
            if device_list:
                await self._create_in_batch(kind=kind, data_list=device_list)
        # self._create_in_batch
        # import json

    async def _create_interfaces(self, topology_name: str, data: list) -> None:
        """Create objects of a specific kind and store in local store."""
        interfaces = {
            "DcimInterfaceL2": [],
            "DcimInterfaceL3": [],
            "DcimInterfaceConsole": [],
        }
        for device in data:
            for item in range(1, device["quantity"] + 1):
                device_name = (
                    f"{topology_name.lower()}-{device.get('role')}-{str(item).zfill(2)}"
                )
                # get exsting interfaces for device
                # and update if they does't exist ?
                for interface in device["interface_patterns"]:
                    _interface = {
                        "payload": {
                            "name": interface.get("name"),
                            "speed": set_speed(interface.get("type")),
                            "mtu": 9000,
                            "device": self.client.store.get(key=device_name).id,
                            "description": f"{device_name} {interface.get('role')} interface",
                            "role": interface.get("role"),
                            "status": "active",
                        },
                        "store_key": f"{device_name}_{interface.get('name')}",
                    }
                    if interface.get("role") in ["uplink", "leaf", "management"]:
                        interfaces["DcimInterfaceL3"].append(_interface)
                    elif interface.get("role") == "console":
                        interfaces["DcimInterfaceConsole"].append(_interface)
                    else:
                        _interface["payload"]["l2_mode"] = "Access"
                        interfaces["DcimInterfaceL2"].append(_interface)

        for kind, interface_list in interfaces.items():
            if interface_list:
                await self._create_in_batch(kind=kind, data_list=interface_list)
        # import json
        # print(json.dumps(l2interface_list, indent=4))

    async def _create_oob_connections(
        self,
        topology_name: str,
        data: list,
        connection_type: str,
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        if connection_type == "management":
            # interface_roles = ["management"]
            device_key = "oob"
        elif connection_type == "console":
            # interface_roles = ["console"]
            device_key = "console"

        interfaces = {
            f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
                interface["name"]
                for interface in item["interface_patterns"]
                if interface["role"] == connection_type
            ]
            for i, item in enumerate(data)
            for j in range(item["quantity"])
            if item["role"] in ["oob", "leaf", "spine", "console"]
        }

        sources = {key: value for key, value in interfaces.items() if device_key in key}
        destinations = {
            key: value for key, value in interfaces.items() if key not in sources
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
            source_endpoint = self.store.get(
                key=f"{connection['source']}_{connection['source_interface']}",
            )
            target_endpoint = self.store.get(
                key=f"{connection['target']}_{connection['destination_interface']}",
            )
            source_endpoint.connector = target_endpoint
            await source_endpoint.save(allow_upsert=True)

    async def _create_peering_connections(self, topology_name: str, data: list) -> None:
        """Create objects of a specific kind and store in local store."""
        # name = f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}"
        interfaces = {
            f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
                interface["name"]
                for interface in item["interface_patterns"]
                if interface["role"] in ["leaf", "uplink"]
            ]
            for i, item in enumerate(data)
            for j in range(item["quantity"])
            if item["role"] in ["spine", "leaf"]
        }
        spines = {key: value for key, value in interfaces.items() if "spine" in key}
        leafs = {key: value for key, value in interfaces.items() if "leaf" in key}
        # print(spines)
        connections = [
            {
                "source": spine_switch,
                "target": leaf,
                "source_interface": spine_interfaces.pop(0),
                "destination_interface": leaf_interfaces.pop(0),
            }
            for spine_switch, spine_interfaces in spines.items()
            for leaf, leaf_interfaces in leafs.items()
        ]
        for connection in connections:
            source_endpoint = self.store.get(
                key=f"{connection['source']}_{connection['source_interface']}",
            )
            target_endpoint = self.store.get(
                key=f"{connection['target']}_{connection['destination_interface']}",
            )
            # print(source_endpoint, target_endpoint)
            source_endpoint.connector = target_endpoint
            await source_endpoint.save(allow_upsert=True)

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        self.client.log.setLevel(logging.INFO)
        self.client.log.addHandler(logging.StreamHandler())
        topology = data["TopologyDataCenter"]["edges"][0]["node"]
        self.client.log.info(f"Generating topology: {topology['name']['value']}")
        new_data = clean_data(data)["TopologyDataCenter"][0]
        # (topology)
        await self._create(
            kind="LocationBuilding",
            data={
                "name": topology["name"]["value"],
                "shortname": topology["name"]["value"],
                "parent": topology["location"]["node"]["id"],
            },
            store_key=topology["name"]["value"],
        )
        # create pools

        # get the device groups
        await self.client.filters(
            kind="CoreStandardGroup",
            name__values=list(
                set(
                    f"{item['device_type']['manufacturer']['name'].lower()}_{item['role']}"
                    for item in new_data["design"]["elements"]
                )
            ),
            branch=self.branch,
            populate_store=True,
        )
        # create respective IP address pools
        await self._create_ip_pools(
            new_data["name"],
            pools={
                "management": new_data["management"],
                "technical": new_data["technical"],
                "customer": new_data["customer"],
                "public": new_data["public"],
            },
        )

        await self._create_devices(
            new_data["name"], new_data["design"]["elements"], new_data["id"]
        )
        await self._create_interfaces(new_data["name"], new_data["design"]["elements"])
        await self._create_oob_connections(
            new_data["name"], new_data["design"]["elements"], "console"
        )
        await self._create_oob_connections(
            new_data["name"], new_data["design"]["elements"], "management"
        )
        await self._create_peering_connections(
            new_data["name"], new_data["design"]["elements"]
        )
