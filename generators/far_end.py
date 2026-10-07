"""Cable far-end lookup shared by the endpoint and app-instance segment generators."""

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
