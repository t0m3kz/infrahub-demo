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
                    f"{item['device_type']['manufacturer']['name'].lower()}_dc_{item['role']}"
                    for item in data["design"]["elements"]
                )
            ),
            branch=self.branch,
            populate_store=True,
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

        await self._create_devices(data["name"], data["design"]["elements"], data["id"], "dc")
        await self._create_interfaces(data["name"], data["design"]["elements"])
        await self._create_oob_connections(
            data["name"], data["design"]["elements"], "console"
        )
        await self._create_oob_connections(
            data["name"], data["design"]["elements"], "management"
        )
        await self._create_peering_connections(data["name"], data["design"]["elements"])
