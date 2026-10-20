"""Cable far-end lookup shared by the endpoint, app-instance segment and service-context generators."""

from __future__ import annotations

from typing import Any

from .protocols import DcimCable, DcimPhysicalInterface


async def far_end_interface(client: Any, interface: Any, include: list[str]) -> Any | None:
    """The interface at the other end of ``interface``'s cable, fetched with
    ``include``; None when the cable has no other endpoint."""
    cable = await client.get(kind=DcimCable, id=interface.cable.id, include=["endpoints"])
    far_ends = [peer for peer in cable.endpoints.peers if peer.id != interface.id]
    if not far_ends:
        return None
    return await client.get(kind=DcimPhysicalInterface, id=far_ends[0].id, include=include)


async def far_end_interfaces(client: Any, interfaces: list[Any], include: list[str]) -> dict[str, Any]:
    """interface id -> the physical interface at the other end of its cable, fetched with ``include``.

    far_end_interface() for several cabled interfaces in two queries, whatever
    their number: their cables, then the far ends. An interface whose cable
    has no other endpoint, or whose far end is not a physical interface, is
    absent from the result. No query at all for no interfaces.
    """
    if not interfaces:
        return {}
    cables = {
        cable.id: cable
        for cable in await client.filters(
            kind=DcimCable, ids=sorted({iface.cable.id for iface in interfaces}), include=["endpoints"]
        )
    }
    far_end_ids: dict[str, str] = {}
    for iface in interfaces:
        cable = cables.get(iface.cable.id)
        peers = [peer.id for peer in cable.endpoints.peers if peer.id != iface.id] if cable is not None else []
        if peers:
            far_end_ids[iface.id] = peers[0]
    if not far_end_ids:
        return {}
    far_ends = {
        node.id: node
        for node in await client.filters(
            kind=DcimPhysicalInterface, ids=sorted(set(far_end_ids.values())), include=include
        )
    }
    return {iface_id: far_ends[peer_id] for iface_id, peer_id in far_end_ids.items() if peer_id in far_ends}
