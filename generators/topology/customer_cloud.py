"""Customer boarding generator for TopologyCustomerCloud (add_customer_deployment_cloud). Creates the cloud pool through
``ensure_customer_pools``, then a /29 range per virtual network (``ensure_cloud_ranges``); later attachments go to
add_cloud_attachment_range.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..pools import PoolMixin
from .cloud_range import CLOUD_KIND, CloudRangeMixin
from .segment_addressing import SegmentAddressingMixin


class CustomerDeploymentCloudGenerator(CloudRangeMixin, PoolMixin, SegmentAddressingMixin, CommonGenerator):
    """add_customer_deployment_cloud: the customer's cloud pool and the routed range of each virtual network."""

    async def generate(self, data: dict[str, Any]) -> None:
        entries = clean_data(data).get(CLOUD_KIND, [])
        if not entries:
            self.logger.info(f"No {CLOUD_KIND} data in GraphQL response: not this generator's kind")
            return
        footprint = entries[0]
        footprint_id = footprint.get("id", "")
        if not footprint_id:
            self.logger.error("Deployment missing id: cannot proceed")
            return
        self.client.group_context.related_node_ids.append(footprint_id)
        self.logger.info(f"Processing {CLOUD_KIND} deployment {footprint.get('name', footprint_id)}")
        await self.ensure_cloud_ranges(footprint)
