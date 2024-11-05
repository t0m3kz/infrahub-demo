"""Common functions for the bootstrap script."""

import logging
from typing import List
from infrahub_sdk import InfrahubClient
from infrahub_sdk.node import InfrahubNode
from infrahub_sdk.store import NodeStore
from infrahub_sdk.exceptions import GraphQLError, ValidationError


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


def populate_store(
    objects: List[InfrahubNode], key_type: str, store: NodeStore
) -> None:
    """Populate local store."""
    for obj in objects:
        key = getattr(obj, key_type)
        if key:
            store.set(key=key.value, node=obj)
