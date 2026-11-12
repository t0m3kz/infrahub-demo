"""Cable far-end lookup shared by the endpoint, app-instance segment and service-context generators."""

from __future__ import annotations

import logging
from typing import Any

from .protocols import DcimCable, DcimPhysicalInterface

logger = logging.getLogger(__name__)


async def far_end_interfaces(client: Any, interfaces: list[Any], include: list[str]) -> dict[str, Any]:
    """interface id -> the physical interface at the other end of its cable, in two queries.
    An interface with no far end, or a far end that is not physical (a patch panel port, logged), is absent."""
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
    for iface_id, peer_id in far_end_ids.items():
        if peer_id not in far_ends:
            logger.warning(
                f"Far end {peer_id} of the cable on interface {iface_id} is not a physical interface: skipped"
            )
    return {iface_id: far_ends[peer_id] for iface_id, peer_id in far_end_ids.items() if peer_id in far_ends}


async def ports_free_for(client: Any, interfaces: list[Any], peer_device_ids: set[str]) -> list[Any]:
    """Interfaces that are uncabled or cabled to one of ``peer_device_ids``; those cabled elsewhere are left out.
    A second chain on the same ports must not pick ports the first has cabled."""
    cabled = [iface for iface in interfaces if getattr(getattr(iface, "cable", None), "id", None)]
    if not cabled:
        return interfaces
    far_ends = await far_end_interfaces(client, cabled, include=["device"])
    elsewhere = {
        iface.id for iface in cabled if (far := far_ends.get(iface.id)) is None or far.device.id not in peer_device_ids
    }
    return [iface for iface in interfaces if iface.id not in elsewhere]
