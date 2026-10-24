"""Give a customer its routing tag: the number behind its community on every route it owns."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data
from utils.limits import CUSTOMER_ROUTING_TAG_POOL

from ..common import CommonGenerator
from ..helpers.routing_tag import ensure_routing_tag

CUSTOMER_KIND = "OrganizationCustomer"


class CustomerRoutingTagGenerator(CommonGenerator):
    """Draw ``OrganizationCustomer.routing_tag`` from Customer-Routing-Tag when the customer has none.

    Runs when a customer is created (data/events/99_actions.yml); a customer that has its tag causes no
    write, so a re-run changes nothing. The tag is the customer half of the routing identity
    (utils/routing_identity.customer_community): an operator's tag for reading the routing table, never a
    filter. The write is untracked: no run owns the customer, so no cleanup may delete it.
    """

    graphql_root_key = CUSTOMER_KIND

    async def generate(self, data: dict[str, Any]) -> None:
        customers = clean_data(data).get(self.graphql_root_key) or []
        if not customers:
            self.fail(f"No {self.graphql_root_key} data in GraphQL response")
        customer = customers[0]
        try:
            tag = await ensure_routing_tag(
                self.client, kind=CUSTOMER_KIND, node=customer, pool_name=CUSTOMER_ROUTING_TAG_POOL
            )
        except LookupError as exc:
            self.fail(str(exc))
        self.logger.info(f"Customer {customer.get('name')}: routing tag {tag}")
