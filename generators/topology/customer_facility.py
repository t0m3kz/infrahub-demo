"""Shared ``generate()`` of the customer boarding generators that provision
service contexts on their parent facility's firewall and load-balancer pairs.

customer_dc.py (TopologyCustomerDC on a DataCenter) and customer_colocation.py
(TopologyCustomerColocation on its serving ColocationZone) differ only in the
customer kind, the parent's label, whether a parent is required, the parent
generators to wait for and the context kinds they provision — class attributes
the host sets (see FirewallContextMixin). The provisioning itself is
generators/firewall_context.py's FirewallContextMixin, one run per ContextSpec.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from utils.data_cleaning import clean_data

from ..firewall_context import FirewallContextMixin


class CustomerFacilityMixin(FirewallContextMixin):
    """``generate()`` for a customer boarding generator on a facility with service pairs.

    The host class also mixes in PoolMixin, DeviceMixin and CablingMixin (see
    FirewallContextMixin) and CommonGenerator, and sets ``_customer_kind``,
    ``_parent_label``, ``_parent_generators`` and ``_context_specs``.
    """

    # A footprint whose parent is optional (a colocation footprint's serving_zone)
    # simply has no contexts when it is unset: an info log and a clean return. A
    # required parent (a DC footprint's DC) that is missing is an error.
    _parent_required: bool = True

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
        # written by the parent's own generator (the only owner of its HA
        # pairs). A customer can board concurrently with (or immediately
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
        # controller aliases in its query (a colocation zone declares none)
        # contributes nothing, leaving the list empty.
        parent = customer.get("parent")
        self.set_controllers_from(parent if isinstance(parent, dict) else {})
        if not (parent or {}).get("id"):
            message = (
                f"Deployment {customer.get('name', customer_id)}: no parent {self._parent_label} — "
                "cannot provision its service contexts"
            )
            if self._parent_required:
                self.logger.error(message)
            else:
                self.logger.info(message)
            return

        # One run per context kind, independent of each other: a parent with
        # load balancers and no firewalls (or the reverse) still gets the one.
        for spec in self._context_specs:
            target = await self._resolve_context_target(spec, customer, customer_id)
            if target is not None:
                await self._ensure_transit_legs(customer=customer, customer_id=customer_id, target=target)
