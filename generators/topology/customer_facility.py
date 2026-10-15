"""Shared ``generate()`` of the customer boarding generators that provision a
FirewallContext on their parent facility's firewall pair.

customer_dc.py (TopologyCustomerDC on a DataCenter) and customer_colocation.py
(TopologyCustomerColocation on a ColocationMetro) differ only in the customer
kind, the parent's label and the parent generators to wait for — class
attributes the host sets (see FirewallContextMixin). The provisioning itself is
generators/firewall_context.py's FirewallContextMixin.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from utils.data_cleaning import clean_data

from ..firewall_context import FirewallContextMixin


class CustomerFacilityMixin(FirewallContextMixin):
    """``generate()`` for a customer boarding generator on a firewall-owning facility.

    The host class also mixes in PoolMixin, DeviceMixin and CablingMixin (see
    FirewallContextMixin) and CommonGenerator, and sets ``_customer_kind``,
    ``_parent_label`` and ``_parent_generators``.
    """

    # Provided by DeviceMixin.
    set_controllers_from: Callable[[Mapping[str, Any]], None]

    async def generate(self, data: dict[str, Any]) -> None:
        kind = self._customer_kind
        cleaned = clean_data(data)

        entries = cleaned.get(kind, [])
        if not entries:
            self.logger.info(f"No {kind} data in GraphQL response — not this generator's kind")
            return
        customer = entries[0]

        customer_id: str = customer.get("id", "")
        if not customer_id:
            self.logger.error("Deployment missing id — cannot proceed")
            return

        self.logger.info(f"Processing {kind} deployment {customer.get('name', customer_id)}")

        # This reads parent-level data (firewall_devices, loadbalancer_devices)
        # written by the parent's own generator (the only owner of its firewall
        # HA pair). A customer can board concurrently with (or immediately
        # after) its parent's creation — e.g. a bulk load — so wait for an
        # in-flight parent generator and re-parse rather than risk provisioning
        # against a parent that has no firewall/LB devices yet (see pod.py's
        # identical wait for the same reason).
        parent_id = (customer.get("parent") or {}).get("id")
        if parent_id:
            refreshed = await self.wait_for_parent_generator_and_refetch(self._parent_generators, parent_id)
            if refreshed is not None:
                entries = clean_data(refreshed).get(kind, [])
                if not entries:
                    self.logger.error(f"No {kind} data in GraphQL response")
                    return
                customer = entries[0]

        # Read synchronously by create_devices() — see
        # generators/devices.py's _resolve_role_controller. A parent without
        # controller aliases in its query (a colocation metro declares none)
        # contributes nothing, leaving the list empty.
        parent = customer.get("parent")
        self.set_controllers_from(parent if isinstance(parent, dict) else {})

        target = await self._resolve_context_target(customer, customer_id)
        if target is not None:
            await self._ensure_transit_legs(customer=customer, customer_id=customer_id, target=target)
        await self._ensure_dedicated_loadbalancer(customer, customer_id)
