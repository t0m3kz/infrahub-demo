"""The addresses of an allocated prefix: queried before created, saved even when found so the run tracks them. Functions
of the SDK ``client``, so the reconciler writes them.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from typing import Any

from generators.helpers.common import tracked_save_kwargs
from generators.protocols import IpamIPAddress


def _address_key(address: str) -> str:
    """Canonical text of an interface address, for matching what the server returns to what was asked."""
    return str(ipaddress.ip_interface(address))


async def upsert_p2p_addresses(
    client: Any,
    prefix: Any,
    *,
    address_length: int | None = None,
    description: str | None = None,
    track: bool = True,
) -> list[Any]:
    """Upsert both addresses of a /31 or /127 P2P prefix (offsets 0 and 1, returned in that order)."""
    by_offset = await upsert_prefix_addresses(
        client, prefix, offsets=(0, 1), address_length=address_length, description=description, track=track
    )
    return list(by_offset.values())


async def upsert_prefix_addresses(
    client: Any,
    prefix: Any,
    *,
    offsets: tuple[int, ...],
    address_length: int | None = None,
    description: str | None = None,
    track: bool = True,
) -> dict[int, Any]:
    """Upsert addresses at the given host offsets of an allocated prefix; returns offset -> address node. Queried
    before created (uniqueness is an async validator) and saved even when found so the run tracks it;
    ``track=False`` skips tracking.
    """
    network = ipaddress.ip_network(prefix.prefix.value, strict=False)
    length = network.prefixlen if address_length is None else address_length
    # network[offset], not .hosts(): .hosts() yields one address for /31 and /127
    addresses = {offset: f"{network[offset]}/{length}" for offset in offsets}
    found = await _find_addresses(client, list(addresses.values()), prefix.ip_namespace.id)
    return {
        offset: await _save_address(
            client,
            address=address,
            ip_namespace=prefix.ip_namespace,
            existing=found.get(_address_key(address)),
            description=description,
            track=track,
        )
        for offset, address in addresses.items()
    }


async def _find_addresses(client: Any, addresses: Sequence[str], namespace_id: str) -> dict[str, Any]:
    """The IpamIPAddress nodes that already exist among ``addresses`` in the namespace, by canonical address, in one
    filtered read (the find-before-create guard).
    """
    nodes = await client.filters(kind=IpamIPAddress, address__values=list(addresses), ip_namespace__ids=[namespace_id])
    return {_address_key(node.address.value): node for node in nodes}


async def _save_address(
    client: Any, *, address: str, ip_namespace: Any, existing: Any | None, description: str | None, track: bool
) -> Any:
    """Save one address, creating it when ``existing`` is None. Saved even when found, since the run tracks only what it
    saves; an existing address is re-created from its id to rewrite the description.
    """
    ip = existing
    if not ip or description is not None:
        ip = await client.create(
            kind=IpamIPAddress,
            data={
                **({"id": ip.id} if ip else {}),
                "address": address,
                "ip_namespace": ip_namespace,
                **({"description": description} if description is not None else {}),
            },
        )
    await ip.save(**tracked_save_kwargs(track))
    return ip
