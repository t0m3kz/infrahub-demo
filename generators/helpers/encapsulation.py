"""Generator reads of ``ManagedInterfaceEncapsulation``: what an interface rides on, and who rides on a port."""

from __future__ import annotations

from typing import Any

from ..protocols import DcimVirtualInterface, ManagedInterfaceEncapsulation


async def parent_ids_of(client: Any, interface_ids: list[str]) -> dict[str, str]:
    """interface id -> id of the interface it rides on, for interfaces that have an encapsulation."""
    if not interface_ids:
        return {}
    encapsulations = await client.filters(
        kind=ManagedInterfaceEncapsulation, capabilities__ids=interface_ids, include=["capabilities"]
    )
    wanted = set(interface_ids)
    return {
        peer.id: encapsulation.parent.id
        for encapsulation in encapsulations
        for peer in encapsulation.capabilities.peers
        if peer.id in wanted and encapsulation.parent.id
    }


async def sub_interfaces_on(
    client: Any, *, device_ids: list[str], parent_ids: list[str], vlan_id: int | None = None
) -> list[tuple[Any, Any]]:
    """``(sub-interface, encapsulation)`` of the virtual interfaces of ``device_ids`` riding on ``parent_ids``
    (optionally one ``vlan_id``).
    """
    tag = {} if vlan_id is None else {"vlan_id__value": vlan_id}
    encapsulations = await client.filters(
        kind=ManagedInterfaceEncapsulation, parent__ids=parent_ids, include=["capabilities"], **tag
    )
    by_interface = {
        peer.id: encapsulation for encapsulation in encapsulations for peer in encapsulation.capabilities.peers
    }
    if not by_interface:
        return []
    interfaces = await client.filters(kind=DcimVirtualInterface, ids=sorted(by_interface), device__ids=device_ids)
    return [(interface, by_interface[interface.id]) for interface in interfaces]


async def riding_interface_ids(client: Any, parent_ids: list[str]) -> list[str]:
    """Ids of every interface that rides on one of ``parent_ids``."""
    encapsulations = await client.filters(
        kind=ManagedInterfaceEncapsulation, parent__ids=parent_ids, include=["capabilities"]
    )
    return sorted({peer.id for encapsulation in encapsulations for peer in encapsulation.capabilities.peers})
