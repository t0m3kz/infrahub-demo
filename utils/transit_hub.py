"""The hub of a ``transit`` cloud footprint: the one ``CloudGateway`` of type ``hub`` in its region whose account has
its environment (data, never built). Read by add_customer_deployment_cloud (generators/cloud/cloud_hub.py) and by
checks/connectivity/cloud_connection.py R12; pure, over clean_data payloads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

HUB = "hub"
TRANSIT = "transit"
GATEWAY_KIND = "CloudGateway"


def ref(value: Any) -> str | None:
    """The id of a cleaned reference (a bare id, or a dict carrying ``id``)."""
    if isinstance(value, dict):
        value = value.get("id")
    return str(value) if value else None


@dataclass(frozen=True)
class HubLink:
    """An attachment of the hub to a peer gateway: the hub's way towards us, and the route table it is associated with."""

    id: str
    name: str
    route_table_id: str | None


@dataclass(frozen=True)
class TransitHub:
    """The hub gateway a transit footprint attaches to."""

    id: str
    name: str
    links: tuple[HubLink, ...]


def hub_candidates(footprint: Mapping[str, Any], gateways: Iterable[Mapping[str, Any]] | None) -> list[TransitHub]:
    """The hub gateways of the footprint's region whose account is of the footprint's environment, by name. Pure."""
    region = ref(footprint.get("parent"))
    environment = footprint.get("environment")
    found: list[TransitHub] = []
    for gateway in gateways or []:
        if not isinstance(gateway, Mapping) or gateway.get("gateway_type", HUB) != HUB or not gateway.get("id"):
            continue
        account = gateway.get("account")
        account = account if isinstance(account, Mapping) else {}
        if ref(gateway.get("region")) != region or account.get("environment") != environment:
            continue
        links = tuple(
            HubLink(
                str(attachment["id"]),
                str(attachment.get("name") or attachment["id"]),
                ref(attachment.get("associated_route_table")),
            )
            for attachment in gateway.get("attachments") or []
            if isinstance(attachment, Mapping) and attachment.get("id") and ref(attachment.get("peer_gateway"))
        )
        found.append(TransitHub(str(gateway["id"]), str(gateway.get("name") or gateway["id"]), links))
    return sorted(found, key=lambda hub: hub.name)


def missing_hub_message(footprint: Mapping[str, Any], candidates: list[TransitHub]) -> str | None:
    """Why the footprint has no one hub (None when it has exactly one): names the footprint, region, environment and fix."""
    if len(candidates) == 1:
        return None
    region = footprint.get("parent")
    region = region if isinstance(region, Mapping) else {}
    place = f"region '{region.get('name') or ref(region)}' and environment '{footprint.get('environment')}'"
    label = f"TopologyCustomerCloud '{footprint.get('name', footprint.get('id'))}'"
    if not candidates:
        return (
            f"{label}: connection 'transit' but no hub gateway in {place}, so its networks cannot be attached. "
            "Author a CloudGateway with gateway_type hub, its region set to the footprint's region, in an account of "
            "that environment (with its authored link towards us), or set the footprint's connection to dedicated or vpn."
        )
    names = ", ".join(f"'{hub.name}'" for hub in candidates)
    return (
        f"{label}: connection 'transit' but {len(candidates)} hub gateways in {place} ({names}): exactly one hub "
        "serves a region and environment. Remove or retype the extra CloudGateway (gateway_type edge)."
    )


def route_table_name(footprint: Mapping[str, Any]) -> str:
    """The footprint's route table on the hub: one per footprint, so footprints never share a routing domain."""
    return f"rt-{str(footprint.get('name') or footprint['id']).lower()}"
