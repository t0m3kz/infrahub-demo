"""Bootstrap script."""

import logging
from ipaddress import IPv4Network
from infrahub_sdk import InfrahubClient
from utils import create_objects, get_interface_speed
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
    PLATFORMS,
    DEVICE_TYPES,
    DEVICE_TEMPLATES,
    DESIGN,
    DESIGN_ELEMENTS,
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
        kind="LocationMetro",
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
        kind="LocationBuilding",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "shortname": item[1],
                    "status": item[2],
                    "site_type": item[3],
                    "parent": client.store.get(kind="LocationMetro", key=item[4]).id,
                },
                "store_key": item[0],
            }
            for item in SITES
        ],
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
        kind="IpamPrefix",
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
    # log.info("Create ASNs")
    # await create_objects(
    #     client=client,
    #     log=log,
    #     branch=branch,
    #     kind="RoutingAutonomousSystem",
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

    log.info("Create Platforms")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="DcimPlatform",
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
        kind="DcimDeviceType",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "part_number": item[1],
                    "height": item[2],
                    "full_depth": item[3],
                    "manufacturer": {
                        "id": client.store.get(
                            kind="OrganizationManufacturer", key=item[4]
                        ).id
                    },
                    "platform": {
                        "id": client.store.get(kind="DcimPlatform", key=item[5]).id
                    },
                },
                "store_key": item[0],
            }
            for item in DEVICE_TYPES
        ],
    )

    # create device templates
    log.info("Create Device Templates")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="TemplateDcimPhysicalDevice",
        data_list=[
            {
                "payload": {
                    "template_name": f"{item[0]}_{item[1].upper()}",
                    "device_type": {
                        "id": client.store.get(kind="DcimDeviceType", key=item[0]).id
                    },
                    "platform": client.store.get(kind="DcimPlatform", key=item[2]).id,
                    "role": item[1],
                },
                "store_key": f"{item[0]}_{item[1].upper()}",
            }
            for item in DEVICE_TEMPLATES
        ],
    )

    log.info("Create Interface Templates")

    templates = {"TemplateDcimPhysicalInterface": [], "TemplateDcimInterfaceConsole": []}

    for template in DEVICE_TEMPLATES:
        for interface in template[3]:
            data = {
                "payload": {
                    "template_name": f"{template[0]}_{template[1].upper()}_{interface[0].upper()}",
                    "device": {
                        "id": client.store.get(
                            kind="TemplateDcimPhysicalDevice",
                            key=f"{template[0]}_{template[1].upper()}",
                        ).id
                    },

                "name": interface[0],
                "role": interface[2],
                "interface_type": interface[1],
                "status": "free",
                },
            }

            templates["TemplateDcimInterfaceConsole"].append(data) if interface[
                2
            ] == "console" else templates["TemplateDcimPhysicalInterface"].append(data)
    
    for kind, data_list in templates.items():
        await create_objects(
            client=client,
            log=log,
            branch=branch,
            kind=kind,
            data_list=data_list,
        )

    # await create_objects(
    #     client=client,
    #     log=log,
    #     branch=branch,
    #     kind="TemplateDcimPhysicalInterface"
    #     if item[1] != "console"
    #     else "TemplateDcimConsolePort",
    #     data_list=[
    #         {
    #             "payload": {
    #                 "template_name": f"{template[0]}_{template[1].upper()}_{interface[0].upper()}",
    #                 "device": {
    #                     "id": client.store.get(
    #                         kind="TemplateDcimPhysicalDevice",
    #                         key=f"{template[0]}_{template[1].upper()}",
    #                     ).id
    #                 },
    #                 "speed": get_interface_speed(interface[1]),
    #                 "name": interface[0],
    #                 "role": interface[2],
    #                 "interface_type": interface[1],
    #             },
    #         }
    #         for template in DEVICE_TEMPLATES
    #         for interface in template[3]
    #     ],
    # )


async def design(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the design objects."""
    # Let's play with owner and source to test functionality

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
                    "template": client.store.get_by_hfid(
                        key=f"TemplateDcimPhysicalDevice__{item[4]}_{item[3].upper()}"
                    ).id,
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


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    await location(client=client, log=log, branch=branch)
    await core(client=client, log=log, branch=branch)
    await infra(client=client, log=log, branch=branch)
    await design(client=client, log=log, branch=branch)
