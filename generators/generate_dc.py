"""Infrastructure generator."""

from infrahub_sdk.generator import InfrahubGenerator

from .common import TopologyCreator, clean_data


class DCTopologyGenerator(InfrahubGenerator):
    """Generate topology."""

    async def generate(self, data: dict) -> None:
        """Generate topology."""
        cleaned_data = clean_data(data)
        if isinstance(cleaned_data, dict):
            data = cleaned_data["TopologyDataCenter"][0]
        else:
            raise ValueError("clean_data() did not return a dictionary")
        network_creator = TopologyCreator(
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
