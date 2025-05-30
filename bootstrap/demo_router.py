"""Bootstrap script."""

import logging

from data_router import (
    INTERFACES,
    POP_DEPLOYMENT,
    ROUTE_TARGETS,
    ROUTERS,
    VRFS,
)
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from utils import create_objects


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

    await client.filters(
        kind="CoreStandardGroup",
        name__values=list(
            set(item[2].split(" ")[0].lower() + "_pop_router" for item in ROUTERS)
        ),
        branch=branch,
        populate_store=True,
    )

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

    member_of_groups = [
        await client.get(
            kind="CoreStandardGroup",
            name__value="topologies_pop",
            branch=branch,
        )
    ]

    if POP_DEPLOYMENT.get("emulation"):
        member_of_groups.append(
            await client.get(
                kind="CoreStandardGroup",
                name__value="topologies_clab",
                branch=branch,
            )
        )

    # log.info("Create DC Topology Deployment")
    # let'ts update location
    POP_DEPLOYMENT.update(
        {
            "location": site.id,
            "provider": provider.id,
            "design": design.id,
            "member_of_groups": member_of_groups,
        }
    )
    log.info(f"Creating POP Topology Deployment for {POP_DEPLOYMENT.get('name')}")
    try:
        deployment = await client.create(
            kind="TopologyColocationCenter", data=POP_DEPLOYMENT, branch=branch
        )
        await deployment.save(allow_upsert=True)
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
                    "member_of_groups": [
                        client.store.get_by_hfid(
                            key=f"CoreStandardGroup__{item[2].split(' ')[0].lower()}_pop_router"
                        ).id
                    ],
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
                    "ip_addresses": (
                        [client.store.get(kind="IpamIPAddress", key=item[4])]
                        if item[4]
                        else None
                    ),
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
