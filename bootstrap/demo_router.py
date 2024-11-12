"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from utils import create_objects

from data_router import ROUTERS, INTERFACES


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
                },
                "store_key": item[0],
            }
            for item in ROUTERS
        ],
    )

    log.info(client.store.__dict__)

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
                    # "ip_addresses": [
                    #     client.store.get(kind="IpamIPAddress", key=item[4])
                    # ],
                    "description": item[5],
                    "role": item[6],
                    "status": "active",
                }
            }
            for item in INTERFACES
        ],
    )

    # log.info("Create Route Targets")
    # await create_objects(
    #     client=client,
    #     log=log,
    #     branch=branch,
    #     kind="InfraRouteTarget",
    #     data_list=[
    #         {
    #             "payload": {
    #                 "name": item[0],
    #                 "description": item[1],
    #             },
    #             "store_key": item[0],
    #         }
    #         for item in ROUTE_TARGETS
    #     ],
    # )

    # log.info("Create VRFs")
    # await create_objects(
    #     client=client,
    #     log=log,
    #     branch=branch,
    #     kind="InfraVRF",
    #     data_list=[
    #         {
    #             "payload": {
    #                 "name": item[0],
    #                 "description": item[1],
    #                 "vrf_rd": item[2],
    #                 "export_rt": (
    #                     {
    #                         "id": client.store.get(
    #                             kind="InfraRouteTarget", key=item[3]
    #                         ).id
    #                     }
    #                     if item[3]
    #                     else None
    #                 ),
    #                 "import_rt": (
    #                     {
    #                         "id": client.store.get(
    #                             kind="InfraRouteTarget", key=item[4]
    #                         ).id
    #                     }
    #                     if item[4]
    #                     else None
    #                 ),
    #             },
    #             "store_key": item[0],
    #         }
    #         for item in VRFS
    #     ],
    # )
