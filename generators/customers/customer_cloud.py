"""Customer boarding generator for TopologyCustomerCloud (add_customer_deployment_cloud). Creates the cloud pool through
``ensure_customer_pools``, then a /29 range per virtual network (``ensure_cloud_ranges``); later attachments go to
add_cloud_attachment_range. A ``transit`` footprint is also attached to the hub of its region and environment
(generators/cloud/cloud_hub.py), in the same plan; with no hub, or two, the run fails before its first write.
"""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.cloud.cloud_range import CLOUD_KIND, ensure_cloud_ranges
from utils.data_cleaning import clean_data
from utils.transit_hub import GATEWAY_KIND, TRANSIT, hub_candidates, missing_hub_message


class CustomerDeploymentCloudGenerator(BaseGenerator):
    """add_customer_deployment_cloud: the customer's cloud pool, the routed range of each virtual network and, for a
    ``transit`` footprint, its attachments and route table on the region's hub.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        entries = cleaned.get(CLOUD_KIND, [])
        if not entries:
            self.logger.info(f"No {CLOUD_KIND} data in GraphQL response: not this generator's kind")
            return
        footprint = entries[0]
        footprint_id = footprint.get("id", "")
        if not footprint_id:
            self.logger.error("Deployment missing id: cannot proceed")
            return
        self.logger.info(f"Processing {CLOUD_KIND} deployment {footprint.get('name', footprint_id)}")
        hub = None
        if footprint.get("connection", TRANSIT) == TRANSIT:
            candidates = hub_candidates(footprint, cleaned.get(GATEWAY_KIND))
            if message := missing_hub_message(footprint, candidates):
                self.fail(message)
            hub = candidates[0]
            self.logger.info(f"Transit footprint: attaching to hub {hub.name}")
        # The plan of ensure_cloud_ranges tracks the footprint first
        await ensure_cloud_ranges(self, footprint, hub=hub)
