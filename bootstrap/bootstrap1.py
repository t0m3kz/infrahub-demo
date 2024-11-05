"""Bootstrap script."""

import logging
from ipaddress import IPv4Network
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError

from data import (
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
    FIREWALLS,
    L3_INTERFACES,
)


async def create_objects(
    client: InfrahubClient, log: logging.Logger, branch: str, kind: str, data_list: list
) -> None:
    """Create objects of a specific kind."""
    batch = await client.create_batch()
    for data in data_list:
        obj = await client.create(kind=kind, data=data.get("payload"), branch=branch)
        try:
            batch.add(task=obj.save, allow_upsert=True, node=obj)
        # TODO: Check if this is correct
        except ValidationError as exc:
            log.debug(f"- Creation failed due to {exc}")
        if data.get("store_key"):
            client.store.set(key=data.get("store_key"), node=obj)
    try:
        async for node, _ in batch.execute():
            object_reference = node.hfid[0] if node.hfid else None
            log.info(
                f"- Created [{node.get_kind()}] '{object_reference}'"
                if object_reference
                else f"- Created [{node.get_kind()}]"
            )
    except GraphQLError as exc:
        log.debug(f"- Creation failed due to {exc}")


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
                "payload": {"name": item[0], "ip_protocol": item[1], "port": item[2]},
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
                    "hostname": f"{item[0]}.comapny.com",
                    "device_type": client.store.get(
                        kind="InfraDeviceType", key=item[1]
                    ).id,
                    "platform": client.store.get(kind="InfraPlatform", key=item[2]).id,
                    "status": item[3],
                    "role": item[4],
                    "location": client.store.get(kind="LocationSite", key=item[5]).id,
                    "primary_address": client.store.get(
                        kind="IpamIPAddress", key=item[6]
                    ).id,
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

    log.info("Adding firewall devices to the group")
    group = await client.create(
        kind="CoreStandardGroup",
        name="firewall_devices",
    )
    await group.save(allow_upsert=True)
    await group.members.fetch()
    group.members.add(
        [client.store.get(kind="InfraFirewall", key=item[0]).id for item in FIREWALLS]
    )

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
                    "device": client.store.get(key=item[0]).id,
                    "ip_addresses": [
                        client.store.get(kind="IpamIPAddress", key=item[4])
                    ],
                    "description": item[5],
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

    await location(client=client, log=log, branch=branch)
    await core(client=client, log=log, branch=branch)
    await infra(client=client, log=log, branch=branch)
    await security(client=client, log=log, branch=branch)
    await devices(client=client, log=log, branch=branch)
