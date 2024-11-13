"""Bootstrap script."""

import logging
from ipaddress import IPv4Network
from infrahub_sdk import InfrahubClient
from utils import create_objects

from data_bootstrap import (
    CITIES,
    COUNTRIES,
    REGIONS,
    SITES,
    ACCOUNTS,
    SUBNETS_1918,
    GROUPS,
    TAGS,
    PROVIDERS,
    CUSTOMERS,
    MANUFACTURERS,
    ASNS,
    PLATFORMS,
    DEVICE_TYPES,
    ROUTE_TARGETS,
    VRFS
)


async def core(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the basics."""
    log.info("Creating Accounts")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Creating Subnets")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Creating Groups")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="CoreStandardGroup",
        data_list=[
            {"payload": {"name": item[0], "description": item[1]}, "key": item[0]}
            for item in GROUPS
        ],
    )

    log.info("Creating Tags")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="BuiltinTag",
        data_list=[{"payload": {"name": item}} for item in TAGS],
    )

    log.info("Create Providers")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="OrganizationProvider",
        data_list=[
            {"payload": {"name": item}, "store_key": item} for item in PROVIDERS
        ],
    )

    log.info("Create Customers")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="OrganizationCustomer",
        data_list=[{"payload": {"name": item}} for item in CUSTOMERS],
    )

    log.info("Create Manufacturers")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="OrganizationManufacturer",
        data_list=[
            {"payload": {"name": item}, "store_key": item} for item in MANUFACTURERS
        ],
    )


async def infra(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infra objects."""
    # Let's play with owner and source to test functionality
    log.info("Create ASNs")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="InfraAutonomousSystem",
        data_list=[
            {
                "payload": {
                    "name": {
                        "value": f"AS{item[0]}",
                        "source": client.store.get(
                            "CRM Synchronization", kind="CoreAccount"
                        ).id,
                        "owner": client.store.get("Tomek Zajac", kind="CoreAccount").id,
                    },
                    "asn": item[0],
                    "description": {
                        "value": f"AS{item[0]} for {item[1]}",
                        "source": client.store.get(
                            "CRM Synchronization", kind="CoreAccount"
                        ).id,
                        "owner": client.store.get("Tomek Zajac", kind="CoreAccount").id,
                    },
                    "organization": {
                        "id": client.store.get(
                            kind="OrganizationProvider", key=item[1]
                        ).id,
                    },
                },
                "store_key": item,
            }
            for item in ASNS
        ],
    )

    log.info("Create Platforms")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Create Device Types")
    await create_objects(
        client=client,
        log=log,
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


async def location(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the location objects."""
    log.info("Creating Regions")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="LocationRegion",
        data_list=[
            {"payload": {"name": item[0], "shortname": item[1]}, "store_key": item[0]}
            for item in REGIONS
        ],
    )

    log.info("Creating Countries")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Creating Cities")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Creating Sites")
    await create_objects(
        client=client,
        log=log,
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


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    await location(client=client, log=log, branch=branch)
    await core(client=client, log=log, branch=branch)
    await infra(client=client, log=log, branch=branch)
