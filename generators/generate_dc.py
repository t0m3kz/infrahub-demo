"""Infrastructure generator."""

import logging
from .common import clean_data, TopologyGenerator


class DCTopologyGenerator(TopologyGenerator):
    """Generate topology."""

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
