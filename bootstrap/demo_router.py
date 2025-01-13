"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from utils import create_objects

from data_router import (
    ROUTERS,
    INTERFACES,
    DESIGN_ELEMENTS,
    DESIGN,
    POP_DEPLOYMENT,
    ROUTE_TARGETS,
    VRFS,
)


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    await client.filters(
        kind="DcimDeviceType",
        name__values=list(set(item[1] for item in ROUTERS)),
        branch=branch,
        populate_store=True,
    )

    await client.filters(
        kind="DcimPlatform",
        name__values=list(set(item[2] for item in ROUTERS)),
        branch=branch,
        populate_store=True,
    )

    await client.filters(
        kind="LocationBuilding",
        name__values=list(set(item[5] for item in ROUTERS)),
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
        kind="LocationMetro", name__value=POP_DEPLOYMENT.get("location"), branch=branch
    )
    provider = await client.get(
        kind="OrganizationProvider",
        name__value=POP_DEPLOYMENT.get("provider"),
        branch=branch,
    )

    # log.info("Create DC Topology Deployment")
    # let'ts update location
    POP_DEPLOYMENT.update(
        {
            "location": site.id,
            "provider": provider.id,
            "design": client.store.get(
                kind="DesignTopology", key=POP_DEPLOYMENT.get("design")
            ).id,
        }
    )
    log.info(f"Creating POP Topology Deployment for {POP_DEPLOYMENT.get('name')}")
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

    log.info(
        f"- Created Colocation Center Topology Deployment for {POP_DEPLOYMENT.get('name')}"
    )

    log.info("Creating IPAM addresses")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="IpamIPAddress",
        data_list=[
            {
                "payload": {
                    "address": item[4],
                    "description": item[5],
                },
                "store_key": item[4],
            }
            for item in INTERFACES
            if item[4]
        ],
    )

    log.info("Creating Devices")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="DcimDevice",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "device_type": client.store.get_by_hfid(
                        key=f"DcimDeviceType__{item[1]}"
                    ).id,
                    # Here we're using hfid to get platform and location from store
                    "platform": client.store.get_by_hfid(
                        key=f"DcimPlatform__{item[2]}"
                    ).id,
                    "status": item[3],
                    "role": item[4],
                    "location": client.store.get_by_hfid(
                        key=f"LocationBuilding__{item[5]}"
                    ).id,
                    "primary_address": client.store.get(
                        kind="IpamIPAddress", key=item[6]
                    ).id,
                    "topology": deployment.id,
                },
                "store_key": item[0],
            }
            for item in ROUTERS
        ],
    )

    # log.info(client.store.__dict__)

    log.info("Creating Device Interfaces")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="DcimInterfaceL3",
        data_list=[
            {
                "payload": {
                    "name": item[1],
                    "speed": item[2],
                    "device": client.store.get_by_hfid(key=f"DcimDevice__{item[0]}").id,
                    "ip_addresses": [
                        client.store.get(kind="IpamIPAddress", key=item[4])
                    ] if item[4] else None,
                    "description": item[5],
                    "role": item[6],
                    "status": "active",
                }
            }
            for item in INTERFACES
        ],
    )

    log.info("Create Route Targets")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="IpamRouteTarget",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "description": item[1],
                },
                "store_key": item[0],
            }
            for item in ROUTE_TARGETS
        ],
    )

    namespace = await client.get(
        kind="IpamNamespace", name__value="default", branch=branch
    )

    log.info("Create VRFs")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="IpamVRF",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "description": item[1],
                    "vrf_rd": item[2],
                    "export_rt": (
                        {
                            "id": client.store.get_by_hfid(
                                key=f"IpamRouteTarget__{item[3]}"
                            ).id
                        }
                        if item[3]
                        else None
                    ),
                    "import_rt": (
                        {
                            "id": client.store.get_by_hfid(
                                key=f"IpamRouteTarget__{item[4]}"
                            ).id
                        }
                        if item[4]
                        else None
                    ),
                    "namespace": namespace.id,
                },
            }
            for item in VRFS
        ],
    )
