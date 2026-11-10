"""Reads of ``ManagedInterfaceEncapsulation`` for generators: what an SDK interface rides on, and who rides on a port.

The writers build the object themselves (an upsert by the name ``utils/interface_encapsulation.py``
``encapsulation_name`` with ``parent``, ``vlan_id`` and the interface in ``capabilities``); these are the two reads the
generators share, one query for any number of interfaces.
"""

from __future__ import annotations

from typing import Any

from ..protocols import DcimVirtualInterface, ManagedInterfaceEncapsulation


async def parent_ids_of(client: Any, interface_ids: list[str]) -> dict[str, str]:
    """interface id -> the id of the interface it rides on, for the interfaces that have an encapsulation."""
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
    """``(sub-interface, encapsulation)`` of the virtual interfaces of ``device_ids`` that ride on one of ``parent_ids``
    (with the tag ``vlan_id`` when given): the encapsulations of the parents first, then their interfaces, two reads."""
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
    """The ids of every interface that rides on one of ``parent_ids`` (a sub-interface, a peering), one read."""
    encapsulations = await client.filters(
        kind=ManagedInterfaceEncapsulation, parent__ids=parent_ids, include=["capabilities"]
    )
    return sorted({peer.id for encapsulation in encapsulations for peer in encapsulation.capabilities.peers})
