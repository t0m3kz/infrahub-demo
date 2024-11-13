"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from utils import create_objects, populate_store

from data_firewall import (
    IP_PROTOCOLS,
    SERVICES,
    SERVICE_GROUPS,
    ADDRESSES,
    PREFIXES,
    ADDRESS_GROUPS,
    ZONES,
    POLICIES,
    RULES,
    FIREWALLS,
    L3_INTERFACES,
)


async def security(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the security objects."""

    log.info("Create IP Protocols")
    await create_objects(
        client=client,
        log=log,
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
    log.info("Create Services")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="SecurityService",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "ip_protocol": client.store.get(
                        kind="SecurityIPProtocol", key=item[1]
                    ),
                    "port": item[2],
                },
                "store_key": item[0],
            }
            for item in SERVICES
        ],
    )

    log.info("Create Service Groups")
    await create_objects(
        client=client,
        log=log,
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
    log.info("Create Prefixes")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Create Security Addresses")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Create Address Groups")
    await create_objects(
        client=client,
        log=log,
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
    log.info("Create Security Zones")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Create Security Policies")
    await create_objects(
        client=client,
        log=log,
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

    log.info("Create Security Policy Rules")
    await create_objects(
        client=client,
        log=log,
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


async def devices(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the device objects."""
    # Create IPAM addresses
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
            for item in L3_INTERFACES
        ],
    )

    juniper_group = await client.create(
        kind="CoreStandardGroup",
        name="juniper_firewall_devices",
        branch=branch,
    )

    log.info("Creating Devices")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="InfraFirewall",
        data_list=[
            {
                "payload": {
                    "name": item[0],
                    "hostname": f"{item[0]}.company.io",
                    "device_type": client.store.get(
                        kind="InfraDeviceType", key=item[1]
                    ).id,
                    # Here we're using hfid to get platform and location from store
                    "platform": client.store.get_by_hfid(
                        key=f"InfraPlatform__{item[2]}"
                    ).id,
                    "status": item[3],
                    "role": item[4],
                    "location": client.store.get_by_hfid(
                        key=f"LocationSite__{item[5]}"
                    ),
                    "primary_address": client.store.get(
                        kind="IpamIPAddress", key=item[6]
                    ),
                    "policy": (
                        client.store.get(kind="SecurityPolicy", key=item[7]).id
                        if item[7]
                        else None
                    ),
                },
                "store_key": item[0],
            }
            for item in FIREWALLS
        ],
    )

    log.info("Adding firewall devices to the groups")

    firewalls = [
        client.store.get(kind="InfraFirewall", key=item[0]).id for item in FIREWALLS
    ]

    juniper_group = await client.create(
        kind="CoreStandardGroup",
        name="juniper_firewall_devices",
        branch=branch,
    )

    await juniper_group.save(allow_upsert=True)
    await juniper_group.members.fetch()
    juniper_group.members.extend(firewalls)
    await juniper_group.save()

    firewall_group = await client.create(
        kind="CoreStandardGroup",
        name="firewalls",
        branch=branch,
    )
    await firewall_group.save(allow_upsert=True)
    await firewall_group.members.fetch()
    firewall_group.members.extend(firewalls)
    await firewall_group.save()
    # FIXME: Addresses should be fetched and added later
    # in case interface already exist and has other IPs attached
    log.info("Creating Device Interfaces")
    await create_objects(
        client=client,
        log=log,
        branch=branch,
        kind="InfraInterfaceL3",
        data_list=[
            {
                "payload": {
                    "name": item[1],
                    "speed": item[2],
                    # Let's play with key only
                    "device": client.store.get(key=item[0]),
                    "ip_addresses": [
                        client.store.get(kind="IpamIPAddress", key=item[4])
                    ],
                    "description": item[5],
                    "role": item[6],
                    "status": "active",
                    "security_zone": (
                        client.store.get(kind="SecurityZone", key=item[3])
                        if item[3]
                        else None
                    ),
                }
            }
            for item in L3_INTERFACES
        ],
    )


async def run(client: InfrahubClient, log: logging.Logger, branch: str) -> None:
    """Create all the infrastructure objects."""
    # client.max_concurrent_execution = 1
    # Load necessary daty into store
    # # Use filters to save memmory
    # Here we're showing method to manually populate store
    # and show how to change the key
    device_types = await client.filters(
        kind="InfraDeviceType",
        name__values=list(set(item[1] for item in FIREWALLS)),
        branch=branch,
        populate_store=True,
    )
    populate_store(objects=device_types, key_type="name", store=client.store)

    # For those we will use HFIDs in the future
    # HFID is set in schemas
    await client.filters(
        kind="InfraPlatform",
        name__values=list(set(item[2] for item in FIREWALLS)),
        branch=branch,
        populate_store=True,
    )

    await client.filters(
        kind="LocationSite",
        name__values=list(set(item[5] for item in FIREWALLS)),
        branch=branch,
        populate_store=True,
    )
    await security(client=client, log=log, branch=branch)
    await devices(client=client, log=log, branch=branch)
