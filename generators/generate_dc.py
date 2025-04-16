"""Infrastructure generator."""

import logging
from .common import clean_data, TopologyGenerator


class DCTopologyGenerator(TopologyGenerator):
    """Generate topology."""

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        self.client.log.setLevel(logging.INFO)
        self.client.log.addHandler(logging.StreamHandler())
        data = clean_data(data)["TopologyDataCenter"][0]
        # self.client.log.info(f"Generating DC topology: {data['design']['elements']}")

        self.client.log.info(f"Generating DC topology: {data['name']}")
        await self._create(
            kind="LocationBuilding",
            data={
                "name": data["name"],
                "shortname": data["name"],
                "parent": data["location"]["id"],
            },
            store_key=data["name"],
        )
        # create pools

        # get the device groups
        await self.client.filters(
            kind="CoreStandardGroup",
            name__values=list(
                set(
                    f"{item['device_type']['manufacturer']['name'].lower()}_{item['role']}"
                    for item in data["design"]["elements"]
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
                    for item in data["design"]["elements"]
                )
            ),
            branch=self.branch,
            populate_store=True,
        )
        # create device groups

        await self._create_core_groups(
            data=[
                {
                    "payload": {
                        "name": f"{item['device_type']['manufacturer']['name'].lower()}_{item['role']}",
                        "description": f"{item['device_type']['manufacturer']['name']} {item['role']} Group",
                    }
                }
                for item in data["design"]["elements"]
            ]
        )

        # add to the topology group

        if data["emulation"]:
            self.client.log.info(f"Assign CLAB group for topology: {data['name']}")
            clab_group = await self.client.get(
                kind="CoreStandardGroup", name__value="topologies_clab"
            )
            await clab_group.add_relationships(
                related_nodes=[data["id"]], relation_to_update="members"
            )

        # create respective IP address pools
        await self._create_ip_pools(
            data["name"],
            pools={
                "management": data["management"],
                "technical": data["technical"],
                "customer": data["customer"],
                "public": data["public"],
            },
        )

        await self._create_devices(data["name"], data["design"]["elements"], data["id"])

        await self._create_oob_connections(
            data["name"], data["design"]["elements"], "console"
        )

        await self._create_oob_connections(
            data["name"], data["design"]["elements"], "management"
        )

        await self._create_peering_connections(data["name"], data["design"]["elements"])

        # interfaces = {
        #     f"{data['name'].lower()}-{item['role']}-{str(j + 1).zfill(2)}": [
        #         interface["name"]
        #         for interface in item["template"]["interfaces"]
        #         if interface["role"] == "console"
        #     ]
        #     for item in data["design"]["elements"]
        #     for j in range(item["quantity"])
        #     if item["role"] in ["oob", "leaf", "spine", "console"]
        # }

        # # sort source inerfaces
        # sources = {
        #     key: sort_interface_list(value)
        #     for key, value in interfaces.items()
        #     if "console" in key
        # }
        # destinations = {
        #     key: sort_interface_list(value)
        #     for key, value in interfaces.items()
        #     if key not in sources
        # }

        # await self.client.filters(kind="InterfacePhysical", device__name__values=list(interfaces.keys()), populate_store=True, parallel=True)
        # connections = [
        #     {
        #         "source": source_device,
        #         "target": destination_device,
        #         "source_interface": source_interfaces.pop(0),
        #         "destination_interface": destination_interfaces.pop(0),
        #     }
        #     for source_device, source_interfaces in sources.items()
        #     for destination_device, destination_interfaces in destinations.items()
        #     if int(destination_device.split("-")[-1]) % 2
        #     == int(source_device.split("-")[-1]) % 2
        # ]
        # # self.client.log.info(self.store._branches[self.branch].__dict__)
        # for connection in connections:
        #     source_endpoint = await self.client.get(
        #         kind="DcimInterface",
        #         name__value=connection["source_interface"],
        #         device__name__value=connection["source"],
        #     )  # self.store.get_by_hfid((connection["source"], connection["source_interface"]))
        #     target_endpoint = await self.client.get(
        #         kind="DcimInterface",
        #         name__value=connection["destination_interface"],
        #         device__name__value=connection["target"],
        #     )  # self.store.get_by_hfid((connection["target"], connection["destination_interface"]))
        #     source_endpoint.connector = target_endpoint.id
        #     await source_endpoint.save(allow_upsert=True)

        # self.client.log.info(connections)

        # self.client.log.info(interfaces)

        # create loopback interfaces
        # await self._create_interfaces(data["name"], data["design"]["elements"])
        # create Console connections
        # for item in self.store.get("dc-2-dc_firewall-01_fxp0").interfaces:
        #     self.client.log.info(item)
        # self.client.log.info(self.store._branches[self.branch].__dict__)
        # self.client.log.info(self.store.get("dc-2-dc_firewall-01").interfaces.peers)
        # self.client.log.info(self.store.get_by_hfid("dc-2-leaf-04").interfaces)

        # self.client.log.info(self.store._branches[self.branch].__dict__)
        # switch = self.store.get_by_hfid("dc-2-leaf-04").interfaces.peers
        # for item in switch.interfaces.peers:
        # for device in devices:
        #     for interface in device.interfaces:
        #         self.client.log.info(interface.__dict__)
        # self.client.log.info(device.interfaces.peers)
        # self.client.log.info(switch.interfaces.peers.__dict__)
        # self.client.log.info(switch.interfaces)
        # self.client.log.info(self.store.get_by_hfid("dc-2-leaf-04").interfaces.__dict__)
        # self.client.log.info(self.store.get_by_hfid("dc-2-leaf-04").interfaces.__dict__)
        # test = self.store.get_by_hfid("dc-2-leaf-04")

        # create Management connections
        # await self._create_oob_connections(
        #     data["name"], data["design"]["elements"], "management"
        # )
        # await self._create_peering_connections(data["name"], data["design"]["elements"])
