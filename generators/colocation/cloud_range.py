"""The routed range of a cloud footprint: one ``CLOUD_RANGE_PREFIX_LENGTH`` slice of its pool per virtual network,
written as the attachments' ``allowed_prefixes``. Authored ``allowed_prefixes`` win; the slice is keyed
``{account}/{network}`` (re-runs return it), the edge is added per attachment, and the slice is untracked. Called by
add_customer_deployment_cloud and add_cloud_attachment_range, so data order does not matter.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from generators.helpers.edges import add_edges
from utils.exchange_transit import ZONE_BY_NS_TYPE, namespace_type_for_environment
from utils.limits import CLOUD_RANGE_PREFIX_LENGTH
from utils.segment_addressing import BLOCK_LENGTH_FIELD, IPV4, customer_pool_name, tenant_pool_name

if TYPE_CHECKING:
    from generators.segments.segment_addressing import SegmentAddressingMixin

CLOUD_KIND = "TopologyCustomerCloud"
NETWORK_KIND = "CloudVirtualNetwork"
ATTACHMENT_KIND = "CloudGatewayAttachment"


class CloudRangeMixin:
    """Mixin: the pool slice for each virtual network of a cloud footprint that has an attachment; needs
    SegmentAddressingMixin, ``client``, ``logger``.
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        ensure_customer_pools = SegmentAddressingMixin.ensure_customer_pools
        _allocate_customer_slice = SegmentAddressingMixin._allocate_customer_slice

    async def ensure_cloud_ranges(self, footprint: dict[str, Any]) -> None:
        """Board the footprint's pool, then give each virtual network of its account and region an attached slice."""
        design, environment = footprint.get("design") or {}, footprint["environment"]
        org_id, name = footprint["owner"]["org_id"], footprint.get("name", footprint["id"])
        await self.ensure_customer_pools(
            kind=CLOUD_KIND, org_id=org_id, environment=environment, design=design, footprint_name=name
        )
        account, region_id = footprint.get("account") or {}, (footprint.get("parent") or {}).get("id")
        if not account.get("id") or not region_id:
            self.logger.warning(f"Footprint {name}: no account or region, no virtual network range to allocate")
            return
        networks = await self.client.filters(kind=NETWORK_KIND, account__ids=[account["id"]], region__ids=[region_id])
        if not networks:
            return
        wanting = await self._attachments_without_range([network.id for network in networks])
        zone = ZONE_BY_NS_TYPE[namespace_type_for_environment(environment)]
        pool_name = customer_pool_name(org_id, environment, CLOUD_KIND, IPV4)
        for network in networks:
            attachment_ids = wanting.get(network.id)
            if not attachment_ids:
                continue
            slice_ = await self._allocate_customer_slice(
                f"{account['name']}/{network.name.value}",
                pool_name,
                tenant_pool_name(zone, IPV4),
                design[BLOCK_LENGTH_FIELD[IPV4]],
                CLOUD_RANGE_PREFIX_LENGTH,
            )
            for attachment_id in attachment_ids:
                await add_edges(self.client, node_id=attachment_id, relation="allowed_prefixes", peer_ids=[slice_.id])
            self.logger.info(
                f"Virtual network {network.name.value}: routed range {slice_.prefix.value} "
                f"on {len(attachment_ids)} attachment(s)"
            )

    async def _attachments_without_range(self, network_ids: list[str]) -> dict[str, list[str]]:
        """Network id -> ids of its attachments with no ``allowed_prefixes`` (one query for all the networks)."""
        attachments = await self.client.filters(
            kind=ATTACHMENT_KIND, virtual_network__ids=network_ids, include=["allowed_prefixes"]
        )
        wanting: dict[str, list[str]] = defaultdict(list)
        for attachment in attachments:
            if not attachment.allowed_prefixes.peer_ids:
                wanting[attachment.virtual_network.id].append(attachment.id)
        return wanting
