"""Generator reads of what an interface rides on (its own ``parent`` and ``vlan_id``) and who rides on a port."""

from __future__ import annotations

from typing import Any

from generators.protocols import DcimSubInterface, DcimVirtualInterface


async def parent_ids_of(client: Any, interface_ids: list[str]) -> dict[str, str]:
    """interface id -> id of the interface it rides on, for interfaces that have a parent."""
    if not interface_ids:
        return {}
    interfaces = await client.filters(kind=DcimSubInterface, ids=interface_ids)
    return {interface.id: interface.parent.id for interface in interfaces if interface.parent.id}


async def sub_interfaces_on(
    client: Any, *, device_ids: list[str], parent_ids: list[str], vlan_id: int | None = None
) -> list[Any]:
    """The virtual interfaces of ``device_ids`` riding on ``parent_ids`` (optionally one ``vlan_id``), each with its
    ``parent`` and ``vlan_id``.
    """
    tag = {} if vlan_id is None else {"vlan_id__value": vlan_id}
    return await client.filters(kind=DcimVirtualInterface, device__ids=device_ids, parent__ids=parent_ids, **tag)


async def riding_interface_ids(client: Any, parent_ids: list[str]) -> list[str]:
    """Ids of every interface that rides on one of ``parent_ids``."""
    interfaces = await client.filters(kind=DcimSubInterface, parent__ids=parent_ids)
    return sorted({interface.id for interface in interfaces})
