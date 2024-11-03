"""Bootstrap script."""

import logging
from ipaddress import IPv4Network
from infrahub_sdk import InfrahubClient
from infrahub_sdk.batch import InfrahubBatch
from infrahub_sdk.exceptions import GraphQLError

from data import (
    CITIES,
    COUNTRIES,
    REGIONS,
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
    VRFS,
    IP_PROTOCOLS,
    SERVICES,
    SERVICE_GROUPS,
    ADDRESSES,
    PREFIXES,
    ADDRESS_GROUPS,
    ZONES,
    POLICIES,
    RULES,
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

    await create_objects(
        client=client,
        batch=batch,
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
    log.info("Created ASNs")

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

    await create_objects(
        client=client,
        batch=batch,
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
    log.info("Created Route Targets")

    await create_objects(
        client=client,
        batch=batch,
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
    log.info("Created VRFs")


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


async def security(
    client: InfrahubClient, log: logging.Logger, branch: str, batch: InfrahubBatch
) -> None:
    """Create all the security objects."""

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityIPProtocol",
        data_list=[
            {
                "payload": {
                    "name": item[1],
                    "protocol": item[0],
                    "description": item[2],
                },
                "store_key": item[1],
            }
            for item in IP_PROTOCOLS
        ],
    )
    log.info("Created IP Protocols")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityService",
        data_list=[
            {
                "payload": {"name": item[0], "ip_protocol": item[1], "port": item[2]},
                "store_key": item[0],
            }
            for item in SERVICES
        ],
    )
    log.info("Created Services")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityServiceGroup",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "services": [
                        client.store.get(kind="SecurityService", key=service)
                        for service in item[1]
                    ],
                },
                "store_key": item[0],
            }
            for item in SERVICE_GROUPS
        ],
    )
    log.info("Created Service Groups")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityPrefix",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "prefix": item[1],
                },
                "store_key": item[0],
            }
            for item in PREFIXES
        ],
    )
    log.info("Created Prefixes")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityIPAddress",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "address": item[1],
                },
                "store_key": item[0],
            }
            for item in ADDRESSES
        ],
    )
    log.info("Created Security Addresses")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityAddressGroup",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "addresses": [
                        client.store.get(kind="SecurityIPAddress", key=address)
                        for address in item[1]
                    ],
                },
                "store_key": item[0],
            }
            for item in ADDRESS_GROUPS
        ],
    )
    log.info("Created Address Groups")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityZone",
        data_list=[
            {
                "payload": {
                    "name": item,
                },
                "store_key": item,
            }
            for item in ZONES
        ],
    )
    log.info("Created Security Zones")
    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityPolicy",
        data_list=[
            {
                "payload": {"name": item},
                "store_key": item,
            }
            for item in POLICIES
        ],
    )
    log.info("Created Security Policies")

    await create_objects(
        client=client,
        batch=batch,
        branch=branch,
        kind="SecurityPolicyRule",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "policy": client.store.get(kind="SecurityPolicy", key=item[1]).id,
                    "index": item[2],
                    "action": item[3],
                    "source_zone": client.store.get(kind="SecurityZone", key=item[4]),
                    "destination_zone": client.store.get(
                        kind="SecurityZone", key=item[5]
                    ).id,
                    "source_address": [
                        client.store.get(kind="SecurityIPAddress", key=address).id
                        for address in item[6]
                    ],
                    "source_groups": [
                        client.store.get(kind="SecurityAddressGroup", key=group).id
                        for group in item[7]
                    ],
                    "source_services": [
                        client.store.get(kind="SecurityService", key=service).id
                        for service in item[8]
                    ],
                    "source_service_groups": [
                        client.store.get(kind="SecurityServiceGroup", key=srv_group).id
                        for srv_group in item[9]
                    ],
                    "destination_address": [
                        client.store.get(kind="SecurityIPAddress", key=address).id
                        for address in item[10]
                    ],
                    "destination_groups": [
                        client.store.get(kind="SecurityAddressGroup", key=group).id
                        for group in item[11]
                    ],
                    "destination_services": [
                        client.store.get(kind="SecurityService", key=service).id
                        for service in item[12]
                    ],
                    "destination_service_groups": [
                        client.store.get(kind="SecurityServiceGroup", key=srv_group).id
                        for srv_group in item[13]
                    ],
                },
                "store_key": item[0],
            }
            for item in RULES
        ],
    )
    log.info("Created Security Policy Rules")


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""

    # Increase concurrent queries
    client.max_concurrent_execution = 20

    batch = await client.create_batch(return_exceptions=True)

    await location(client=client, log=log, branch=branch, batch=batch)
    await core(client=client, log=log, branch=branch, batch=batch)
    await infra(client=client, log=log, branch=branch, batch=batch)
    await security(client=client, log=log, branch=branch, batch=batch)

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