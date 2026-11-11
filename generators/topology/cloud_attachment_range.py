"""Gives a virtual network its routed range when a gateway attachment to it appears or is retargeted.

Triggered when a ``CloudGatewayAttachment`` joins the ``cloud_attachment_ranges`` group (data/events/99_group_triggers.yml)
and when its ``virtual_network`` changes (data/events/99_actions.yml). The attachment is the node that fired the event, so
it is the generator's target; the work is the footprint's: the attachment's network belongs to an account, the account has
one ``TopologyCustomerCloud`` footprint, and ``CloudRangeMixin.ensure_cloud_ranges`` (the function
``add_customer_deployment_cloud`` calls too) gives every network of that footprint an attached /29 slice of the footprint's
pool where its attachments have no ``allowed_prefixes``. Authored prefixes win; see generators/topology/cloud_range.py.

An attachment to a gateway (no ``virtual_network``) or to a network of an account with no footprint (a hub account) is
logged and left alone. Registered as add_cloud_attachment_range in .infrahub.yml, querying cloud_attachment_range.gql.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..pools import PoolMixin
from .cloud_range import ATTACHMENT_KIND, CLOUD_KIND, CloudRangeMixin
from .segment_addressing import SegmentAddressingMixin


class CloudAttachmentRangeGenerator(CloudRangeMixin, PoolMixin, SegmentAddressingMixin, CommonGenerator):
    """add_cloud_attachment_range: the routed range of the network an attachment targets."""

    async def generate(self, data: dict[str, Any]) -> None:
        entries = clean_data(data).get(ATTACHMENT_KIND, [])
        if not entries:
            self.logger.info(f"No {ATTACHMENT_KIND} data in GraphQL response: not this generator's kind")
            return
        attachment = entries[0]
        if not attachment.get("id"):
            self.logger.error("Attachment missing id: cannot proceed")
            return
        self.client.group_context.related_node_ids.append(attachment["id"])
        network = attachment.get("virtual_network") or {}
        account = network.get("account") or {}
        if not network.get("id") or not account.get("id"):
            self.logger.info(f"Attachment {attachment.get('name')}: not to a virtual network, no range to allocate")
            return
        footprints = await self.client.filters(
            kind=CLOUD_KIND, account__ids=[account["id"]], include=["owner", "design", "parent"]
        )
        if not footprints:
            self.logger.info(f"Attachment {attachment.get('name')}: account {account.get('name')} has no footprint")
            return
        footprint = footprints[0]
        await self.ensure_cloud_ranges(
            {
                "id": footprint.id,
                "name": footprint.name.value,
                "environment": footprint.environment.value,
                "owner": {"org_id": footprint.owner.peer.org_id.value},
                "design": {
                    "name": footprint.design.peer.name.value,
                    "address_prefix_length": footprint.design.peer.address_prefix_length.value,
                },
                "account": account,
                "parent": {"id": footprint.parent.id},
            }
        )
