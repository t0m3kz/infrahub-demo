"""The routed range of a cloud footprint: one slice of its pool per virtual network, written on the network's attachments.

A cloud footprint (``TopologyCustomerCloud``: one account in one cloud region) has an IPv4 pool, ``{org}-{env}-cloud-ipv4``
(``SegmentAddressingMixin.ensure_customer_pools``), a block of its tier's ``address_prefix_length`` (S /28 to XL /25).
``ensure_cloud_ranges`` splits that pool into one ``CLOUD_RANGE_PREFIX_LENGTH`` (/29) slice per ``CloudVirtualNetwork`` of
the footprint's account in the footprint's region (``max_virtual_networks``: S 1, M 3, L 6, XL 12, all fit the block;
``checks/customer_quota.py`` holds it). The slice is the network's internally routed (translated) range towards the DC:
it is written as the ``allowed_prefixes`` of the ``CloudGatewayAttachment``s whose target is that network, and
``transforms/helpers/bgp_cloud.py`` reads it from there.

Rules:

- AUTHORED DATA WINS. An attachment that already has ``allowed_prefixes`` (authored, or the slice of an earlier run)
  is left alone. A network all of whose attachments have some gets no slice.
- A network with NO attachment gets no slice: nothing announces a range, and no address is spent. Its slice is
  allocated the day an attachment to it exists.
- The slice is keyed ``{account name}/{network name}`` in the pool (an account's network names are unique): the same
  identifier returns the same prefix on a re-run. A customer pool is shared by the customer's footprints of one
  environment, so it grows by one block when a draw does not fit (the one draw of ``_allocate_customer_slice``).
- The edge is added to each attachment alone (RelationshipAdd), never saved as a list; the slice is written untracked.

Two generators call it, so the order of the data does not matter: ``add_customer_deployment_cloud`` (the footprint is
boarded: every attachment already loaded) and ``add_cloud_attachment_range`` (an attachment joins its group or changes
its ``virtual_network``: the footprint of the network's account). A node rule cannot run the footprint's generator from an
attachment event, because an event runs a generator on the node that fired it.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from utils.exchange_transit import ZONE_BY_NS_TYPE, namespace_type_for_environment
from utils.limits import CLOUD_RANGE_PREFIX_LENGTH
from utils.segment_addressing import BLOCK_LENGTH_FIELD, IPV4, customer_pool_name, tenant_pool_name

from ..helpers.edges import add_edges

if TYPE_CHECKING:
    from .segment_addressing import SegmentAddressingMixin

CLOUD_KIND = "TopologyCustomerCloud"
NETWORK_KIND = "CloudVirtualNetwork"
ATTACHMENT_KIND = "CloudGatewayAttachment"


class CloudRangeMixin:
    """Mixin: the slice of a cloud footprint's pool for each virtual network that has an attachment.

    Expects the host class to provide the SegmentAddressingMixin functions below, ``client`` and ``logger``.
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        ensure_customer_pools = SegmentAddressingMixin.ensure_customer_pools
        _allocate_customer_slice = SegmentAddressingMixin._allocate_customer_slice

    async def ensure_cloud_ranges(self, footprint: dict[str, Any]) -> None:
        """Board the footprint's pool, then give each virtual network of its account and region an attached slice.

        ``footprint`` is the cleaned footprint: ``name``, ``environment``, ``owner.org_id``, ``design``,
        ``account`` (``id``, ``name``) and ``parent`` (the region, ``id``).
        """
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
