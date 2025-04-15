"""Bootstrap script."""

import logging
from ipaddress import IPv4Network
from infrahub_sdk import InfrahubClient
from infrahub_sdk.batch import InfrahubBatch
from infrahub_sdk.exceptions import GraphQLError

from data_bootstrap import (
    REGIONS,
    COUNTRIES,
    CITIES,
    SITES,
    ACCOUNTS,
    SUBNETS_1918,
    GROUPS,
    TAGS,
    PROVIDERS,
    CUSTOMERS,
    MANUFACTURERS,
    # ASNS,
    PLATFORMS,
    DEVICE_TYPES,
)


async def create_objects(
    client: InfrahubClient,
    branch: str,
    kind: str,
    data_list: list,
    batch: InfrahubBatch,
) -> None:
    """Create objects of a specific kind."""
    for data in data_list:
        obj = await client.create(kind=kind, data=data.get("payload"), branch=branch)
        batch.add(task=obj.save, allow_upsert=True, node=obj)
        if data.get("store_key"):
            client.store.set(key=data.get("store_key"), node=obj)


async def core(
    client: InfrahubClient, log: logging.Logger, branch: str, batch: InfrahubBatch
) -> None:
    """Create all the basics."""

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="CoreAccount",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "password": item[2],
                    "type": item[1],
                    "role": item[3],
                },
                "store_key": item[0],
            }
            for item in ACCOUNTS
        ],
    )
    log.info("Created Accounts")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="IpamIPPrefix",
        data_list=[
            {
                "payload": {
                    "prefix": item[0],
                    "description": {
                        "value": f"{IPv4Network(item[0]).network_address} Supernet"
                    },
                    "status": {"value": "active"},
                    "role": {"value": "supernet"},
                },
                "store_key": item[0],
            }
            for item in SUBNETS_1918
        ],
    )
    log.info("Created Subnets")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="CoreStandardGroup",
        data_list=[
            {"payload": {"name": item[0], "description": item[1]}, "key": item[0]}
            for item in GROUPS
        ],
    )
    log.info("Created Standard Groups")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="BuiltinTag",
        data_list=[{"payload": {"name": item}} for item in TAGS],
    )
    log.info("Created Tags")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="OrganizationProvider",
        data_list=[
            {"payload": {"name": item}, "store_key": item} for item in PROVIDERS
        ],
    )
    log.info("Created Providers")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="OrganizationCustomer",
        data_list=[{"payload": {"name": item}} for item in CUSTOMERS],
    )
    log.info("Created Customers")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="OrganizationManufacturer",
        data_list=[
            {"payload": {"name": item}, "store_key": item} for item in MANUFACTURERS
        ],
    )
    log.info("Created Manufacturers")


async def infra(
    client: InfrahubClient, log: logging.Logger, branch: str, batch: InfrahubBatch
) -> None:
    """Create all the infra objects."""
    # Let's play with owner and source to test functionality

    # await create_objects(
    #     client=client,
    #     batch=batch,
    #     branch=branch,
    #     kind="InfraAutonomousSystem",
    #     data_list=[
    #         {
    #             "payload": {
    #                 "name": {
    #                     "value": f"AS{item[0]}",
    #                     "source": client.store.get(
    #                         "CRM Synchronization", kind="CoreAccount"
    #                     ).id,
    #                     "owner": client.store.get("Tomek Zajac", kind="CoreAccount").id,
    #                 },
    #                 "asn": item[0],
    #                 "description": {
    #                     "value": f"AS{item[0]} for {item[1]}",
    #                     "source": client.store.get(
    #                         "CRM Synchronization", kind="CoreAccount"
    #                     ).id,
    #                     "owner": client.store.get("Tomek Zajac", kind="CoreAccount").id,
    #                 },
    #                 "organization": {
    #                     "id": client.store.get(
    #                         kind="OrganizationProvider", key=item[1]
    #                     ).id,
    #                 },
    #             },
    #             "store_key": item,
    #         }
    #         for item in ASNS
    #     ],
    # )
    # log.info("Created ASNs")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="InfraPlatform",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "nornir_platform": item[1],
                    "napalm_driver": item[2],
                    "netmiko_device_type": item[3],
                    "ansible_network_os": item[4],
                    "containerlab_os": item[5],
                },
                "store_key": item[0],
            }
            for item in PLATFORMS
        ],
    )
    log.info("Created Platforms")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="InfraDeviceType",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "part_number": item[1],
                    "height": item[2],
                    "full_depth": item[3],
                    "platform": {
                        "id": client.store.get(kind="InfraPlatform", key=item[4]).id
                    },
                },
                "store_key": item[0],
            }
            for item in DEVICE_TYPES
        ],
    )
    log.info("Created Device Types")


async def location(
    client: InfrahubClient, log: logging.Logger, branch: str, batch: InfrahubBatch
) -> None:
    """Create all the location objects."""

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="LocationRegion",
        data_list=[
            {"payload": {"name": item[0], "shortname": item[1]}, "store_key": item[0]}
            for item in REGIONS
        ],
    )
    log.info("Created Regions")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="LocationCountry",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "shortname": item[1],
                    "parent": client.store.get(kind="LocationRegion", key=item[2]).id,
                },
                "store_key": item[0],
            }
            for item in COUNTRIES
        ],
    )
    log.info("Created Countries")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="LocationCity",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "shortname": item[1],
                    "parent": client.store.get(kind="LocationCountry", key=item[2]).id,
                },
                "store_key": item[0],
            }
            for item in CITIES
        ],
    )
    log.info("Created Cities")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="LocationSite",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "shortname": item[1],
                    "status": item[2],
                    "site_type": item[3],
                    "parent": client.store.get(kind="LocationCity", key=item[4]).id,
                },
                "store_key": item[0],
            }
            for item in SITES
        ],
    )
    log.info("Created Cities")


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    # Increase concurrent queries
    # client.max_concurrent_execution = 20

    batch = await client.create_batch(return_exceptions=True)

    await location(client=client, log=log, branch=branch, batch=batch)
    await core(client=client, log=log, branch=branch, batch=batch)
    await infra(client=client, log=log, branch=branch, batch=batch)

    try:
        async for node, _ in batch.execute():
            object_reference = node.hfid[0] if node.hfid else None
            log.info(
                f"- Saved [{node.get_kind()}] '{object_reference}'"
                if object_reference
                else f"- Saved [{node.get_kind()}]"
            )
    except GraphQLError as exc:
        log.debug(f"- Creation failed due to {exc}")
