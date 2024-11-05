"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from utils import create_objects

from data_router import (
    ROUTE_TARGETS,
    VRFS,
)


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    log.info("Create Route Targets")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="InfraRouteTarget",
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

    log.info("Create VRFs")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="InfraVRF",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "description": item[1],
                    "vrf_rd": item[2],
                    "export_rt": (
                        {
                            "id": client.store.get(
                                kind="InfraRouteTarget", key=item[3]
                            ).id
                        }
                        if item[3]
                        else None
                    ),
                    "import_rt": (
                        {
                            "id": client.store.get(
                                kind="InfraRouteTarget", key=item[4]
                            ).id
                        }
                        if item[4]
                        else None
                    ),
                },
                "store_key": item[0],
            }
            for item in VRFS
        ],
    )
