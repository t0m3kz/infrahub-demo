"""Bootstrap script."""

import logging

from data_firewall import POP_DEPLOYMENT
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from utils import create_objects


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    site = await client.get(
        kind="LocationMetro", name__value=POP_DEPLOYMENT.get("location"), branch=branch
    )
    provider = await client.get(
        kind="OrganizationProvider",
        name__value=POP_DEPLOYMENT.get("provider"),
        branch=branch,
    )

    design = await client.get(
        kind="DesignTopology", name__value=POP_DEPLOYMENT.get("design"), branch=branch
    )

    network_types = [
        ("management", "Management Network", "management"),
        ("customer", "Customer Network", "supernet"),
    ]

    prefix_data = []
    for prefix_key, description_suffix, role in network_types:
        if POP_DEPLOYMENT.get(prefix_key):
            prefix_data.append(
                {
                    "payload": {
                        "prefix": POP_DEPLOYMENT.get(prefix_key),
                        "description": f"{POP_DEPLOYMENT.get('name')} {description_suffix}",
                        "status": "active",
                        "role": role,
                    },
                    "store_key": POP_DEPLOYMENT.get(prefix_key),
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

    log.info("Create ASNs")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="ServiceAutonomousSystem",
        data_list=[
            {
                "payload": {
                    "name": f"AS{POP_DEPLOYMENT.get('asn')}",
                    "description": f"AS{POP_DEPLOYMENT.get('asn')} for {POP_DEPLOYMENT.get('name')}",
                    "asn": POP_DEPLOYMENT.get("asn"),
                    "status": "active",
                    "provider": {"id": provider.id},
                },
                "store_key": "POP_ASN",
            }
        ],
    )

    POP_DEPLOYMENT.update(
        {
            "location": site.id,
            "provider": provider.id,
            "design": design.id,
            "management_subnet": client.store.get(
                kind="IpamPrefix", key=POP_DEPLOYMENT.get("management", "")
            ).id,
            "customer_subnet": client.store.get(
                kind="IpamPrefix", key=POP_DEPLOYMENT.get("customer", "")
            ).id,
            "asn": client.store.get_by_hfid(key="POP_ASN"),
        }
    )

    log.info(f"Creating DC Topology Deployment for {POP_DEPLOYMENT.get('name')}")
    try:
        deployment = await client.create(
            kind="TopologyColocationCenter", data=POP_DEPLOYMENT, branch=branch
        )
        await deployment.save(allow_upsert=True)
        # asssign the design to the deployment group
        topology_group = await client.create(
            kind="CoreStandardGroup",
            name="topologies_pop",
            branch=branch,
        )
        # create group for topologies if doesn't exist
        await topology_group.save(allow_upsert=True)
        await topology_group.members.fetch()
        topology_group.members.add(deployment)
        await topology_group.save(allow_upsert=True)
    except (ValidationError, GraphQLError) as e:
        log.error(e)

    log.info(f"- Created POP Topology Deployment for {POP_DEPLOYMENT.get('name')}")
