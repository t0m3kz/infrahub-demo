"""Give a customer its routing tag: the number behind its community on every route it owns."""

from __future__ import annotations

from typing import Any

from generators.base import BaseGenerator
from generators.helpers.routing_tag import routing_tag_spec
from generators.plan import TopologyPlan
from utils.data_cleaning import clean_data
from utils.limits import CUSTOMER_ROUTING_TAG_POOL

CUSTOMER_KIND = "OrganizationCustomer"


class CustomerRoutingTagGenerator(BaseGenerator):
    """Draw ``OrganizationCustomer.routing_tag`` from Customer-Routing-Tag when the customer has none; idempotent and
    untracked.
    """

    graphql_root_key = CUSTOMER_KIND

    async def generate(self, data: dict[str, Any]) -> None:
        customers = clean_data(data).get(self.graphql_root_key) or []
        if not customers:
            self.fail(f"No {self.graphql_root_key} data in GraphQL response")
        customer = customers[0]
        # The customer is the target, not an output: the plan has no root, the draw is untracked
        plan = TopologyPlan(root_id=None)
        try:
            spec = routing_tag_spec(kind=CUSTOMER_KIND, node=customer, pool_name=CUSTOMER_ROUTING_TAG_POOL)
        except ValueError as exc:
            self.fail(str(exc))
        if spec is not None:
            plan.add(spec)
            customer["routing_tag"] = (await self.apply_plan(plan)).routing_tags[spec.node_id]
        self.logger.info(f"Customer {customer.get('name')}: routing tag {customer['routing_tag']}")
