"""Gives a virtual network its routed range when a gateway attachment to it appears or is retargeted. Work is the
footprint's (``ensure_cloud_ranges``, cloud_range.py); an attachment to a gateway, or to a network with
no footprint, is logged and left alone.
"""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.cloud.cloud_range import ATTACHMENT_KIND, CLOUD_KIND, ensure_cloud_ranges
from generators.plan import TopologyPlan
from utils.data_cleaning import clean_data


class CloudAttachmentRangeGenerator(BaseGenerator):
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
        footprint = await self._footprint_of(attachment)
        if footprint is None:
            # The attachment is still this run's own: tracked, so a later run does not reclaim it
            await self.apply_plan(TopologyPlan(root_id=attachment["id"]))
            return
        await ensure_cloud_ranges(self, footprint, root_id=attachment["id"])

    async def _footprint_of(self, attachment: dict[str, Any]) -> dict[str, Any] | None:
        """The cleaned footprint of the account of the attachment's network, None (logged) when it has no network or the
        account has no footprint.
        """
        network = attachment.get("virtual_network") or {}
        account = network.get("account") or {}
        if not network.get("id") or not account.get("id"):
            self.logger.info(f"Attachment {attachment.get('name')}: not to a virtual network, no range to allocate")
            return None
        footprints = await self.client.filters(
            kind=CLOUD_KIND, account__ids=[account["id"]], include=["owner", "design", "parent"]
        )
        if not footprints:
            self.logger.info(f"Attachment {attachment.get('name')}: account {account.get('name')} has no footprint")
            return None
        footprint: Any = footprints[0]
        return {
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
