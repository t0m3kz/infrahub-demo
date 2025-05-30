"""Bootstrap script."""

import logging

from data_design import DC_DEPLOYMENT
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from utils import create_objects


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

    network_types = [
        ("management", "Management Network", "management"),
        ("customer", "Customer Network", "supernet"),
        ("technical", "Technical Network", "technical"),
    ]

    prefix_data = []
    for prefix_key, description_suffix, role in network_types:
        if DC_DEPLOYMENT.get(prefix_key):
            prefix_data.append(
                {
                    "payload": {
                        "prefix": DC_DEPLOYMENT.get(prefix_key),
                        "description": f"{DC_DEPLOYMENT.get('name')} {description_suffix}",
                        "status": "active",
                        "role": role,
                    },
                    "store_key": DC_DEPLOYMENT.get(prefix_key),
                }
            )

    # Add public network if available
    if DC_DEPLOYMENT.get("public"):
        prefix_data.append(
            {
                "payload": {
                    "prefix": DC_DEPLOYMENT.get("public"),
                    "description": f"{DC_DEPLOYMENT.get('name')} Public Network",
                    "status": "active",
                    "role": "public",
                },
                "store_key": DC_DEPLOYMENT.get("public"),
            }
        )

    log.info("Creating prefixes")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="IpamPrefix",
        data_list=prefix_data,
    )

    # log.info("Create DC Topology Deployment")
    # let'ts update location
    DC_DEPLOYMENT.update(
        {
            "location": site.id,
            "provider": provider.id,
            "design": design.id,
            "management_subnet": client.store.get(
                kind="IpamPrefix", key=DC_DEPLOYMENT.get("management")
            ).id,
            "customer_subnet": client.store.get(
                kind="IpamPrefix", key=DC_DEPLOYMENT.get("customer")
            ).id,
            "technical_subnet": client.store.get(
                kind="IpamPrefix", key=DC_DEPLOYMENT.get("technical")
            ).id,
        }
    )
    if DC_DEPLOYMENT.get("public"):
        DC_DEPLOYMENT.update(
            {
                "public_subnet": client.store.get(
                    kind="IpamPrefix", key=DC_DEPLOYMENT.get("public")
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
