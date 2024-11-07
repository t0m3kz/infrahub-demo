"""Bootstrap script."""

import logging
from infrahub_sdk import InfrahubClient
from utils import create_objects

from data_router import ROUTE_TARGETS, VRFS, L3_INTERFACES, ROUTERS


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
                    # Here we're using hfid to get platform and location from store
                    "platform": client.store.get_by_hfid(
                        key=f"InfraPlatform__{item[2]}"
                    ).id,
                    "status": item[3],
                    "role": item[4],
                    "location": client.store.get_by_hfid(
                        key=f"LocationSite__{item[5]}"
                    ).id,
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
            for item in ROUTERS
        ],
    )

    log.info("Adding firewall devices to the groups")

    juniper_group = await client.create(
        kind="CoreStandardGroup",
        name="cisco_devices",
    )
    await juniper_group.save(allow_upsert=True)
    await juniper_group.members.fetch()

    firewall_group = await client.create(
        kind="CoreStandardGroup",
        name="core_devices",
    )
    await firewall_group.save(allow_upsert=True)
    await firewall_group.members.fetch()

    # Add devices to groups is not accepting list ?
    for member in [
        client.store.get(kind="InfraFirewall", key=item[0]).id for item in ROUTERS
    ]:
        juniper_group.members.add(member)
        firewall_group.members.add(member)
    await juniper_group.save()
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
                    "device": client.store.get(key=item[0]).id,
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
