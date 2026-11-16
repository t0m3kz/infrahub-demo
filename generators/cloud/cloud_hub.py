"""The shared transit hub of a cloud footprint whose ``connection`` is ``transit``: its place on the region's hub.

The hub is data, never built here (found by utils/transit_hub.py): a ``CloudGateway`` of type ``hub`` in the footprint's region whose account has the
footprint's environment; its own link towards us (the hub's attachments to a peer gateway, the edge gateway a peering
lands on, with everything behind it) is authored. ``plan_transit`` (pure) adds what the footprint owns on it, tracked
by the run so a footprint that stops being ``transit`` loses them in the group cleanup:

- the footprint's route table ``rt-<footprint>`` on the hub;
- one attachment ``<hub>-<network>`` per virtual network of the footprint's account in its region, associated with
  that route table and propagating into it (the footprint's networks reach each other) and into the route table of
  every hub link (so the edge, which enters the hub through a link, accepts them: transforms/helpers/bgp_cloud.py);
- the route table on the propagation list of each hub link (an edge, so the footprint reaches us).

The routed range of each generated attachment is the network's slice (generators/cloud/cloud_range.py), the one writer
of ``allowed_prefixes``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from generators.plan import EdgesSpec, NodeRef, NodeSpec, TopologyPlan
from utils.transit_hub import TransitHub, ref, route_table_name

ROUTE_TABLE_KIND = "CloudGatewayRouteTable"
ATTACHMENT_KIND = "CloudGatewayAttachment"
ROUTE_TABLE_KEY = "transit-route-table"


@dataclass(frozen=True)
class TransitNetwork:
    """A virtual network of the footprint's account in its region."""

    id: str
    name: str


def attachment_name(hub: TransitHub, network: TransitNetwork) -> str:
    """The generated attachment of a network on the hub (a network name is unique, so is this one)."""
    return f"{hub.name}-{network.name}"


def attachment_key(network: TransitNetwork) -> str:
    """The plan key of the generated attachment of a network."""
    return f"transit-attachment-{network.name}"


def plan_transit(
    plan: TopologyPlan, footprint: Mapping[str, Any], hub: TransitHub, networks: Iterable[TransitNetwork]
) -> dict[str, tuple[str, str]]:
    """Add the footprint's route table on ``hub``, an attachment per network and the hub links' propagation into the
    table, all owned by the run. Returns network name -> (plan key, name) of its attachment. Pure.
    """
    account_id = ref(footprint.get("account"))
    label = str(footprint.get("name") or footprint["id"])
    plan.add(
        NodeSpec(
            key=ROUTE_TABLE_KEY,
            kind=ROUTE_TABLE_KIND,
            data={
                "name": route_table_name(footprint),
                "description": f"Routing domain of {label} on the shared hub {hub.name}",
                "status": "active",
                "is_default": False,
                "gateway": hub.id,
            },
            ownership="owned",
        )
    )
    toward_us = sorted({link.route_table_id for link in hub.links if link.route_table_id})
    keys: dict[str, tuple[str, str]] = {}
    for network in sorted(networks, key=lambda n: n.name):
        key, name = attachment_key(network), attachment_name(hub, network)
        plan.add(
            NodeSpec(
                key=key,
                kind=ATTACHMENT_KIND,
                data={
                    "name": name,
                    "description": f"{label}: virtual network {network.name} on the shared hub {hub.name}",
                    "status": "active",
                    "gateway": hub.id,
                    "virtual_network": network.id,
                    "owner_account": account_id,
                    "associated_route_table": NodeRef(ROUTE_TABLE_KEY),
                    "propagates_to_route_tables": [NodeRef(ROUTE_TABLE_KEY), *toward_us],
                },
                ownership="owned",
            )
        )
        keys[network.name] = (key, name)
    for link in hub.links:
        plan.add(
            EdgesSpec(
                node=link.id,
                relation="propagates_to_route_tables",
                peers=(NodeRef(ROUTE_TABLE_KEY),),
                kind=ATTACHMENT_KIND,
            )
        )
    return keys
