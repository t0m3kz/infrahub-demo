"""Customer boarding generator for TopologyCustomerCloud.

Triggered on TopologyCustomerCloud creation (see data/events/99_group_triggers.yml's
trigger-customer-deployment-cloud-on-added rule: the footprint joins customer_deployments_cloud).

It creates the customer's cloud pool, ``{org_id}-{environment}-cloud-ipv4``: a block of the design's
``address_prefix_length`` (the routed range of the footprint, S /28 to XL /25) carved from the environment's global
tenant pool (``Tenant-PROD-IPv4`` or ``Tenant-NONPROD-IPv4``) by the same function that boards a data center or a
colocation footprint (``SegmentAddressingMixin.ensure_customer_pools``). IPv4 only: a cloud footprint has no IPv6
pool, and its segments are IPv4 (checks/customer_quota.py). A customer's cloud footprints of one environment share the
pool, so the second finds it.

NOTHING DRAWS FROM THE POOL YET. A cloud subnet is provider-assigned (its ``cidr_block`` is data, not allocated), and
no generator builds anything in the cloud: the pool exists for the later routed-range allocation per virtual network.
A footprint carries no firewall context, load balancer or circuit of its own, so there is nothing else to provision.

Registered as add_customer_deployment_cloud in .infrahub.yml, targeting the customer_deployments_cloud group and
querying customer_cloud.gql.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..pools import PoolMixin
from .segment_addressing import SegmentAddressingMixin

CLOUD_KIND = "TopologyCustomerCloud"


class CustomerDeploymentCloudGenerator(PoolMixin, SegmentAddressingMixin, CommonGenerator):
    """add_customer_deployment_cloud: the customer's cloud pool, from the footprint's design."""

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
        await self.ensure_customer_pools(
            kind=CLOUD_KIND,
            org_id=footprint["owner"]["org_id"],
            environment=footprint["environment"],
            design=footprint.get("design") or {},
            footprint_name=footprint.get("name", footprint_id),
        )
