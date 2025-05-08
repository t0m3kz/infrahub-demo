"""Common functions for the bootstrap script."""

import logging
from typing import List

from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import GraphQLError, ValidationError
from infrahub_sdk.node import InfrahubNode
from infrahub_sdk.store import NodeStore


def get_interface_speed(interface_type: str) -> int:
    """
    Assign interface speed (in Mbps) based on the interface_type.

    Args:
        interface_type (str): The type of the interface.

    Returns:
        int: The speed of the interface in Mbps. Returns 0 if the type is unknown.
    """
    speed_mapping = {
        "100base-fx": 100,
        "100base-lfx": 100,
        "100base-tx": 100,
        "100base-t1": 100,
        "1000base-t": 1000,
        "1000base-x-gbic": 1000,
        "1000base-x-sfp": 1000,
        "2.5gbase-t": 2500,
        "5gbase-t": 5000,
        "10gbase-t": 10000,
        "10gbase-cx4": 10000,
        "10gbase-x-sfpp": 10000,
        "10gbase-x-xfp": 10000,
        "10gbase-x-xenpak": 10000,
        "10gbase-x-x2": 10000,
        "25gbase-x-sfp28": 25000,
        "40gbase-x-qsfpp": 40000,
        "50gbase-x-sfp28": 50000,
        "100gbase-x-cfp": 100000,
        "100gbase-x-cfp2": 100000,
        "100gbase-x-cfp4": 100000,
        "100gbase-x-cpak": 100000,
        "100gbase-x-qsfp28": 100000,
        "200gbase-x-cfp2": 200000,
        "200gbase-x-qsfp56": 200000,
        "400gbase-x-qsfpdd": 400000,
        "400gbase-x-osfp": 400000,
        "ieee802.11a": 54,
        "ieee802.11g": 54,
        "ieee802.11n": 600,
        "ieee802.11ac": 1300,
        "ieee802.11ad": 7000,
        "ieee802.11ax": 9600,
        "ieee802.15.1": 3,  # Bluetooth
        "gsm": 0.1,  # Approximate speed in Mbps
        "cdma": 0.1,  # Approximate speed in Mbps
        "lte": 100,  # Approximate speed in Mbps
        "sonet-oc3": 155,
        "sonet-oc12": 622,
        "sonet-oc48": 2488,
        "sonet-oc192": 9953,
        "sonet-oc768": 39813,
        "sonet-oc1920": 99532,
        "sonet-oc3840": 199065,
        "1gfc-sfp": 1000,
        "2gfc-sfp": 2000,
        "4gfc-sfp": 4000,
        "8gfc-sfpp": 8000,
        "16gfc-sfpp": 16000,
        "32gfc-sfp28": 32000,
        "64gfc-qsfpp": 64000,
        "128gfc-qsfp28": 128000,
        "infiniband-sdr": 2500,
        "infiniband-ddr": 5000,
        "infiniband-qdr": 10000,
        "infiniband-fdr10": 10000,
        "infiniband-fdr": 14000,
        "infiniband-edr": 25000,
        "infiniband-hdr": 50000,
        "infiniband-ndr": 100000,
        "infiniband-xdr": 200000,
        "t1": 1.544,  # Mbps
        "e1": 2.048,  # Mbps
        "t3": 44.736,  # Mbps
        "e3": 34.368,  # Mbps
        "xdsl": 100,  # Approximate speed in Mbps
        "docsis": 1000,  # Approximate speed in Mbps
        "cisco-stackwise": 16000,
        "cisco-stackwise-plus": 32000,
        "cisco-flexstack": 16000,
        "cisco-flexstack-plus": 32000,
        "cisco-stackwise-80": 80000,
        "cisco-stackwise-160": 160000,
        "cisco-stackwise-320": 320000,
        "cisco-stackwise-480": 480000,
        "juniper-vcp": 10000,
        "extreme-summitstack": 10000,
        "extreme-summitstack-128": 128000,
        "extreme-summitstack-256": 256000,
        "extreme-summitstack-512": 512000,
        "gpon": 2488,  # Mbps
        "xg-pon": 10000,  # Mbps
        "xgs-pon": 10000,  # Mbps
        "ng-pon2": 40000,  # Mbps
        "epon": 1000,  # Mbps
        "10g-epon": 10000,  # Mbps
        "other": 0,  # Unknown speed
    }

    return speed_mapping.get(interface_type, 0)


async def create_objects(
    client: InfrahubClient, log: logging.Logger, branch: str, kind: str, data_list: list
) -> None:
    """Create objects of a specific kind."""
    batch = await client.create_batch()

    # Add all objects to batch in one pass
    for data in data_list:
        try:
            obj = await client.create(
                kind=kind, data=data.get("payload"), branch=branch
            )
            batch.add(task=obj.save, allow_upsert=True, node=obj)

            # Store reference if needed
            if store_key := data.get("store_key"):
                client.store.set(key=store_key, node=obj)
        except GraphQLError as exc:
            log.debug(f"- Creation failed due to {exc}")

    # Execute batch
    try:
        async for node, _ in batch.execute():
            # Use object reference if available, otherwise fallback to display_label
            object_reference = node.hfid[0] if node.hfid else node.display_label

            log_message = f"- Created [{node.get_kind()}]"
            if object_reference:
                log_message += f" '{object_reference}'"

            log.info(log_message)
    except ValidationError as exc:
        log.debug(f"- Creation failed due to {exc}")


def populate_store(
    objects: List[InfrahubNode], key_type: str, store: NodeStore
) -> None:
    """Populate local store."""
    for obj in objects:
        key = getattr(obj, key_type)
        if key:
            store.set(key=key.value, node=obj)
