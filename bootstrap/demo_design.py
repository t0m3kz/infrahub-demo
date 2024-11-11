"""Bootstrap script."""

import logging
import json
from infrahub_sdk import InfrahubClient
from utils import create_objects

from data_design import (
    DESIGN_ELEMENTS,
    DESIGN,
)


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
        kind="TopologyDesignElement",
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
        kind="TopologyDesign",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "description": item[1],
                    "type": item[2],
                    "elements": [
                        client.store.get(kind="TopologyDesignElement", key=element).id
                        for element in item[3]
                    ],
                },
                #  "store_key": item[0],
            }
            for item in DESIGN
        ],
    )
