"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from utils import create_objects

from data_design import DC_DEPLOYMENT


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    site = await client.get(
        kind="LocationMetro", name__value=DC_DEPLOYMENT.get("location"), branch=branch
    )
    provider = await client.get(
        kind="OrganizationProvider",
        name__value=DC_DEPLOYMENT.get("provider"),
        branch=branch,
    )

    design = await client.get(
        kind="DesignTopology", name__value=DC_DEPLOYMENT.get("design"), branch=branch
    )

    # log.info("Create DC Topology Deployment")
    # let'ts update location
    DC_DEPLOYMENT.update(
        {
            "location": site.id,
            "provider": provider.id,
            "design": design.id,
        }
    )
    log.info(f"Creating DC Topology Deployment for {DC_DEPLOYMENT.get('name')}")
    try:
        deployment = await client.create(
            kind="TopologyDataCenter", data=DC_DEPLOYMENT, branch=branch
        )
        await deployment.save(allow_upsert=True)
        # asssign the design to the deployment group
        topology_group = await client.create(
            kind="CoreStandardGroup",
            name="topologies_dc",
            branch=branch,
        )
        # create group for topologies if doesn't exist
        await topology_group.save(allow_upsert=True)
        await topology_group.members.fetch()
        topology_group.members.add(deployment)
        await topology_group.save(allow_upsert=True)
    except (ValidationError, GraphQLError) as e:
        log.error(e)

    log.info(f"- Created DC Topology Deployment for {DC_DEPLOYMENT.get('name')}")
