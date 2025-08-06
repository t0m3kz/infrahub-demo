"""Infrastructure generator."""

from infrahub_sdk.generator import InfrahubGenerator

from .common import TopologyCreator, clean_data


class PopTopologyGenerator(InfrahubGenerator):
    """Generate topology."""

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        cleaned_data = clean_data(data)
        if isinstance(cleaned_data, dict):
            data = cleaned_data["TopologyColocationCenter"][0]
        else:
            raise ValueError("clean_data() did not return a dictionary")

        self.logger.info(f"Data: {data}")
        network_creator = TopologyCreator(
            client=self.client, log=self.logger, branch=self.branch, data=data
        )
        await network_creator.load_data()
        await network_creator.create_site()
        await network_creator.create_address_pools()
        await network_creator.create_devices()
        await network_creator.create_loopback("loopback0")
        # self.log.info(self.client.store._branches[self.branch].__dict__)
        await network_creator.create_oob_connections("management")
        await network_creator.create_oob_connections("console")
