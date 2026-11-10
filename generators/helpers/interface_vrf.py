"""Put an interface in its VRF instance: the one helper for the interface edge of a ``ManagedVRF``.

An interface's VRF is the ``ManagedVRF`` among its ``capabilities`` (the VRF lists its interfaces through the interface
edge, ``ManagedGenericInterfaces``). The same edge holds the interface's other capabilities: the circuits it ends, the
exchanges a leg belongs to and, on a switch port, the VLAN rows of its switch it is a member of, each written by
another generator. So an existing interface's ``capabilities`` are never saved as a list (a list save replaces the edge
set; generators/helpers/edges.py): the VRF, and any other peer, is added edge by edge (``join_capabilities``, one
RelationshipAdd) and a stale instance removed edge by edge (``leave_vrfs``, RelationshipRemove). The instance an
interface joins is often created in the same pass (generators/helpers/vrf.py ``vrfs_held``), and a RelationshipAdd
naming a node created a moment before, in another request, can reach the other replica first: the add is retried on
NODE_NOT_FOUND (generators/helpers/common.py ``call_with_node_not_found_retry``). A NEW interface takes its VRF in its
create data (``vrf_capability``): there is no edge set to lose yet.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from ..protocols import ManagedVRF
from .common import call_with_node_not_found_retry
from .edges import add_edges, remove_edge

_RELATION = "capabilities"


def vrf_capability(vrf_id: str | None) -> dict[str, Any]:
    """The create data that puts a NEW interface in ``vrf_id`` as its only capability (nothing for None: no VRF, the
    default table). Also for an SVI, whose only capability is its VRF (checks/vrf_instances.py S4)."""
    return {_RELATION: [{"id": vrf_id}]} if vrf_id else {}


def vrf_ids(peers: Iterable[Any]) -> list[str]:
    """The instances among the peers of an interface's loaded ``capabilities``."""
    return [peer.id for peer in peers if peer.typename == ManagedVRF.__name__ and peer.id]


async def join_capabilities(client: Any, logger: logging.Logger, interface_id: str, peer_ids: Iterable[str]) -> None:
    """Add ``peer_ids`` to the interface's ``capabilities`` in one RelationshipAdd (the other peers and their source
    stay), retried on NODE_NOT_FOUND: a peer may be an instance or an exchange created a moment ago. Nothing for none."""
    wanted = list(dict.fromkeys(peer_ids))
    if not wanted:
        return
    await call_with_node_not_found_retry(
        lambda: add_edges(client, node_id=interface_id, relation=_RELATION, peer_ids=wanted),
        logger,
        f"adding capabilities to interface {interface_id}",
    )


async def leave_vrfs(client: Any, interface_id: str, stale_ids: Iterable[str]) -> None:
    """Remove the interface from the instances ``stale_ids``, one RelationshipRemove each (its other capabilities stay,
    and each instance keeps its other interfaces)."""
    for vrf_id in stale_ids:
        await remove_edge(client, node_id=interface_id, relation=_RELATION, peer_id=vrf_id)
