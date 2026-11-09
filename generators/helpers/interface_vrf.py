"""Put an interface in its VRF instance: the one helper for the interface edge of a ``ManagedVRF``.

An interface's VRF is the ``ManagedVRF`` among its ``capabilities`` (the VRF lists its interfaces through the interface
edge, ``ManagedGenericInterfaces``). The instance an interface joins is often created in the same pass
(generators/helpers/vrf.py ``vrfs_held``), and a RelationshipAdd on a node created a moment before, in another request,
can reach the other replica first and fail unretried. So the edge to the new instance is written by the interface's own
save: in its create data (``vrf_capability``), or added to its loaded ``capabilities`` and saved with the NODE_NOT_FOUND
retry (``add_vrf``). The instances it leaves are long-lived: it is removed from them edge by edge, from the VRF side
(``leave_vrfs``, RelationshipRemove), never by a saved list of the instance.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..protocols import ManagedVRF


def vrf_capability(vrf_id: str | None) -> dict[str, Any]:
    """The save data that puts an interface in ``vrf_id`` as its only capability (nothing for None: no VRF, the
    default table). For a new interface, or one whose only capability is its VRF (an SVI)."""
    return {"capabilities": [{"id": vrf_id}]} if vrf_id else {}


def vrf_ids(peers: Iterable[Any]) -> list[str]:
    """The instances among the peers of an interface's loaded ``capabilities``."""
    return [peer.id for peer in peers if peer.typename == ManagedVRF.__name__ and peer.id]


def add_vrf(capabilities: Any, vrf_id: str) -> bool:
    """Add ``vrf_id`` to an interface's loaded ``capabilities`` (RelationshipManager) unless it is there; the caller
    saves the interface. Returns whether it added."""
    if vrf_id in {peer.id for peer in capabilities.peers}:
        return False
    capabilities.add({"id": vrf_id})
    return True


async def leave_vrfs(client: Any, interface_id: str, stale_ids: Iterable[str]) -> None:
    """Remove the interface from the instances ``stale_ids`` (each keeps its other interfaces)."""
    for vrf_id in stale_ids:
        vrf = await client.get(kind=ManagedVRF, id=vrf_id)
        await vrf.remove_relationships(relation_to_update="capabilities", related_nodes=[interface_id])
