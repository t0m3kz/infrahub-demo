"""Put an interface in its VRF instance, edge by edge, never by saving the ``capabilities`` list.
See generators/helpers/edges.py; the add is retried on NODE_NOT_FOUND (the instance may be new)."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..protocols import ManagedVRF
from .common import call_with_node_not_found_retry
from .edges import add_edges, remove_edge

_RELATION = "capabilities"


def vrf_capability(vrf_id: str | None) -> dict[str, Any]:
    """Create data that puts a NEW interface in ``vrf_id`` as its only capability (nothing for None)."""
    return {_RELATION: [{"id": vrf_id}]} if vrf_id else {}


def vrf_ids(peers: Iterable[Any]) -> list[str]:
    """The instances among the peers of an interface's loaded ``capabilities``."""
    return [peer.id for peer in peers if peer.typename == ManagedVRF.__name__ and peer.id]


async def join_capabilities(client: Any, logger: logging.Logger, interface_id: str, peer_ids: Iterable[str]) -> None:
    """Add ``peer_ids`` to the interface's ``capabilities`` in one RelationshipAdd, retried on NODE_NOT_FOUND."""
    wanted = list(dict.fromkeys(peer_ids))
    if not wanted:
        return
    await call_with_node_not_found_retry(
        lambda: add_edges(client, node_id=interface_id, relation=_RELATION, peer_ids=wanted),
        logger,
        f"adding capabilities to interface {interface_id}",
    )


async def leave_vrfs(client: Any, interface_id: str, stale_ids: Iterable[str]) -> None:
    """Remove the interface from the instances ``stale_ids``, one RelationshipRemove each."""
    for vrf_id in stale_ids:
        await remove_edge(client, node_id=interface_id, relation=_RELATION, peer_id=vrf_id)
