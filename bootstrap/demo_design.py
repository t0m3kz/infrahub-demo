"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from utils import create_objects

from data_design import DESIGN_ELEMENTS, DESIGN, DC_DEPLOYMENT


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    # Get device types and save in store
    await client.filters(
        kind="DcimDeviceType",
        name__values=list(set(item[4] for item in DESIGN_ELEMENTS)),
        branch=branch,
        populate_store=True,
    )

    log.info("Creating Design Elements")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="DesignElement",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "description": item[1],
                    "quantity": item[2],
                    "role": item[3],
                    "device_type": client.store.get_by_hfid(
                        key=f"DcimDeviceType__{item[4]}"
                    ).id,
                    "interface_patterns": item[5],
                },
                "store_key": item[0],
            }
            for item in DESIGN_ELEMENTS
        ],
    )

    log.info("Creating Design Patterns")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="DesignTopology",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "description": item[1],
                    "type": item[2],
                    "elements": [
                        client.store.get(kind="DesignElement", key=element).id
                        for element in item[3]
                    ],
                },
                "store_key": item[0],
            }
            for item in DESIGN
        ],
    )

    site = await client.get(
        kind="LocationMetro", name__value=DC_DEPLOYMENT.get("location"), branch=branch
    )
    provider = await client.get(
        kind="OrganizationProvider",
        name__value=DC_DEPLOYMENT.get("provider"),
        branch=branch,
    )

    # log.info("Create DC Topology Deployment")
    # let'ts update location
    DC_DEPLOYMENT.update(
        {
            "location": site.id,
            "provider": provider.id,
            "design": client.store.get(
                kind="DesignTopology", key=DC_DEPLOYMENT.get("design")
            ).id,
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
