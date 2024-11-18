"""Infrastructure generator."""

import re
import json
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
        except GraphQLError as exc:
            self.client.log.debug(f"- Creation failed due to {exc}")

    async def _create_devices(self, topology_name: str, data: list) -> None:
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
                    if interface.get("role") in ["spine", "leaf"]:
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

    async def _create_console_connections(self, topology_name: str, data: list) -> None:
        """Create objects of a specific kind and store in local store."""
        interfaces = {
            f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
                interface["name"]
                for interface in item["interface_patterns"]
                if interface["role"] == "console"
            ]
            for i, item in enumerate(data)
            for j in range(item["quantity"])
            if item["role"] in ["oob", "leaf", "spine", "console"]
        }

        consoles = {key: value for key, value in interfaces.items() if "console" in key}
        devices = {
            key: value for key, value in interfaces.items() if key not in consoles
        }

        connections = [
            {
                "source": console_device,
                "target": device,
                "source_interface": console_interfaces.pop(0),
                "destination_interface": device_interfaces[0],
            }
            for console_device, console_interfaces in consoles.items()
            for device, device_interfaces in devices.items()
            if int(device.split("-")[-1]) % 2 == int(console_device.split("-")[-1]) % 2
        ]

        for connection in connections:
            source_endpoint = self.store.get(
                key=f"{connection['source']}_{connection['source_interface']}",
            )
            target_endpoint = self.store.get(
                key=f"{connection['target']}_{connection['destination_interface']}",
            )
            source_endpoint.connector = [target_endpoint]
            await source_endpoint.save()

    async def _create_management_connections(
        self, topology_name: str, data: list
    ) -> None:
        """Create objects of a specific kind and store in local store."""
        interfaces = {
            f"{topology_name.lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
                interface["name"]
                for interface in item["interface_patterns"]
                if interface["role"] == "management"
            ]
            for i, item in enumerate(data)
            for j in range(item["quantity"])
            if item["role"] in ["oob", "leaf", "spine", "console"]
        }

        oob_switches = {key: value for key, value in interfaces.items() if "oob" in key}
        devices = {
            key: value for key, value in interfaces.items() if key not in oob_switches
        }

        connections = [
            {
                "source": oob_switch,
                "target": device,
                "source_interface": oob_interfaces.pop(0),
                "destination_interface": device_interfaces[0],
            }
            for oob_switch, oob_interfaces in oob_switches.items()
            for device, device_interfaces in devices.items()
            if int(device.split("-")[-1]) % 2 == int(oob_switch.split("-")[-1]) % 2
        ]

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
        print(json.dumps(connections, indent=4))

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
        await self._create_devices(new_data["name"], new_data["design"]["elements"])
        await self._create_interfaces(new_data["name"], new_data["design"]["elements"])
        await self._create_console_connections(
            new_data["name"], new_data["design"]["elements"]
        )
        await self._create_management_connections(
            new_data["name"], new_data["design"]["elements"]
        )
        await self._create_peering_connections(
            new_data["name"], new_data["design"]["elements"]
        )
