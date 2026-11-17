"""The routed range of a cloud footprint: one ``CLOUD_RANGE_PREFIX_LENGTH`` slice of its pool per virtual network,
written as the attachments' ``allowed_prefixes``. Authored ``allowed_prefixes`` win; the slice is keyed
``{account}/{network}`` (re-runs return it), the edge is added per attachment, and the slice is untracked. Called by
add_customer_deployment_cloud and add_cloud_attachment_range, so data order does not matter. One plan
(CustomerPoolsSpec, CustomerSliceSpec, AddEdgesSpec): ``read_cloud_ranges`` reads the networks and their bare
attachments, ``plan_cloud_ranges`` is pure, ``ensure_cloud_ranges`` applies it. For a ``transit`` footprint the same
plan holds its attachments to the hub (generators/cloud/cloud_hub.py), and each gets its network's slice from here:
this module stays the one writer of ``allowed_prefixes``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from generators.base import BaseGenerator
from generators.cloud.cloud_hub import TransitNetwork, plan_transit
from generators.plan import AddEdgesSpec, CustomerPoolsSpec, CustomerSliceSpec, NodeRef, TopologyPlan
from utils.exchange_transit import ZONE_BY_NS_TYPE, namespace_type_for_environment
from utils.limits import CLOUD_RANGE_PREFIX_LENGTH
from utils.segment_addressing import BLOCK_LENGTH_FIELD, IPV4, customer_pool_name, tenant_pool_name
from utils.transit_hub import TransitHub

CLOUD_KIND = "TopologyCustomerCloud"
NETWORK_KIND = "CloudVirtualNetwork"
ATTACHMENT_KIND = "CloudGatewayAttachment"


@dataclass(frozen=True)
class CloudNetwork:
    """A virtual network of the footprint's account and region, and its attachments that have no ``allowed_prefixes``
    (by id, with their names: a generated transit attachment is given its slice by its plan key instead).
    """

    name: str
    attachment_ids: tuple[str, ...] = ()
    id: str = ""
    attachment_names: tuple[str, ...] = ()


async def read_cloud_ranges(client: Any, footprint: dict[str, Any]) -> list[CloudNetwork]:
    """The networks of the footprint's account and region with their bare attachments, by name: two reads (the
    networks, then the attachments of all of them).
    """
    account, region_id = footprint.get("account") or {}, (footprint.get("parent") or {}).get("id")
    networks = await client.filters(kind=NETWORK_KIND, account__ids=[account["id"]], region__ids=[region_id])
    if not networks:
        return []
    attachments = await client.filters(
        kind=ATTACHMENT_KIND, virtual_network__ids=[network.id for network in networks], include=["allowed_prefixes"]
    )
    wanting: dict[str, list[Any]] = defaultdict(list)
    for attachment in attachments:
        if not attachment.allowed_prefixes.peer_ids:
            wanting[attachment.virtual_network.id].append(attachment)
    return sorted(
        (
            CloudNetwork(
                network.name.value,
                tuple(a.id for a in wanting[network.id]),
                network.id,
                tuple(str(getattr(getattr(a, "name", None), "value", "") or "") for a in wanting[network.id]),
            )
            for network in networks
        ),
        key=lambda network: network.name,
    )


def range_identifier(account_name: str, network_name: str) -> str:
    """The key of a network's slice, so a re-run returns the same prefix."""
    return f"{account_name}/{network_name}"


def plan_cloud_ranges(
    plan: TopologyPlan,
    footprint: dict[str, Any],
    networks: list[CloudNetwork],
    generated: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Add the footprint's customer pool (boarded first), a slice of it per network that has bare or generated
    attachments, and an edge from each of those attachments to its network's slice. ``generated``: network name ->
    (plan key, name) of the attachment the plan saves for it (a transit footprint). Pure.
    """
    design, environment = footprint.get("design") or {}, footprint["environment"]
    org_id, name = footprint["owner"]["org_id"], footprint.get("name", footprint["id"])
    plan.add(
        CustomerPoolsSpec(kind=CLOUD_KIND, org_id=org_id, environment=environment, design=design, footprint_name=name)
    )
    zone = ZONE_BY_NS_TYPE[namespace_type_for_environment(environment)]
    pool_name = customer_pool_name(org_id, environment, CLOUD_KIND, IPV4)
    planned = generated or {}
    for network in networks:
        own = planned.get(network.name)
        bare = [
            attachment_id
            for attachment_id, attachment_name in zip(
                network.attachment_ids, network.attachment_names or ("",) * len(network.attachment_ids), strict=True
            )
            if own is None or attachment_name != own[1]
        ]
        if not bare and own is None:
            continue
        identifier = range_identifier(footprint["account"]["name"], network.name)
        plan.add(
            CustomerSliceSpec(
                identifier=identifier,
                pool=pool_name,
                global_pool=tenant_pool_name(zone, IPV4),
                block_length=design[BLOCK_LENGTH_FIELD[IPV4]],
                length=CLOUD_RANGE_PREFIX_LENGTH,
            )
        )
        for attachment_id in bare:
            plan.add(AddEdgesSpec(node_id=attachment_id, relation="allowed_prefixes", peer_prefix=identifier))
        if own is not None:
            plan.add(AddEdgesSpec(node_id=NodeRef(own[0]), relation="allowed_prefixes", peer_prefix=identifier))


async def ensure_cloud_ranges(
    gen: BaseGenerator, footprint: dict[str, Any], *, root_id: str | None = None, hub: TransitHub | None = None
) -> None:
    """Board the footprint's pool, then give each virtual network of its account and region an attached slice, as one
    plan. ``root_id`` is the object the run tracks (default: the footprint): a run started for an attachment passes its
    own id and does not track the footprint. ``hub`` set (a transit footprint): the same plan attaches every network to
    it (generators/cloud/cloud_hub.py) and gives each generated attachment its network's slice.
    """
    plan = TopologyPlan(root_id=root_id or footprint["id"])
    account, region_id = footprint.get("account") or {}, (footprint.get("parent") or {}).get("id")
    networks: list[CloudNetwork] = []
    if account.get("id") and region_id:
        networks = await read_cloud_ranges(gen.client, footprint)
    else:
        gen.logger.warning(
            f"Footprint {footprint.get('name', footprint['id'])}: no account or region, no virtual network range to "
            "allocate"
        )
    generated: dict[str, tuple[str, str]] = {}
    if hub is not None:
        transit = [TransitNetwork(network.id, network.name) for network in networks]
        generated = plan_transit(plan, footprint, hub, transit)
    plan_cloud_ranges(plan, footprint, networks, generated)
    reconciled = await gen.apply_plan(plan)
    for spec in plan.of(CustomerSliceSpec):
        gen.logger.info(f"Virtual network range {spec.identifier}: {reconciled.prefixes[spec.identifier].prefix.value}")
