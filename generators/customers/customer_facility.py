"""Shared ``generate()`` of the customer boarding generators that provision service contexts on a facility.
customer_dc.py and customer_colocation.py differ only in class attributes; the provisioning is FirewallContextMixin's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from generators.helpers.vrf import VrfMixin
from generators.mixins.firewall_context import FirewallContextMixin
from generators.mixins.firewall_context_spec import _ContextTarget
from generators.segments.segment_addressing import SegmentAddressingMixin
from utils.data_cleaning import clean_data

if TYPE_CHECKING:
    from generators.mixins.devices import DeviceMixin


class CustomerFacilityMixin(FirewallContextMixin, VrfMixin, SegmentAddressingMixin):
    """``generate()`` for a customer boarding generator on a facility with service pairs; boards the customer pools
    first. The host sets ``_customer_kind``, ``_parent_label``, ``_parent_generators`` and ``_context_specs``.
    """

    # False: a footprint with no hosting facility (a metro without a zone) just has no contexts.
    _parent_required: bool = True

    # Provided by DeviceMixin.
    if TYPE_CHECKING:
        set_controllers_from = DeviceMixin.set_controllers_from

    def _hosting_parents(self, customer: dict[str, Any]) -> list[dict[str, Any]]:
        """The facilities whose pairs host this footprint's contexts: its parent (a metro overrides it)."""
        parent = customer.get("parent")
        return [parent] if isinstance(parent, dict) and parent.get("id") else []

    async def _reconcile_border_vrfs(self, target: _ContextTarget, label: str) -> None:
        """Converge the VRF instances of the border devices facing the context's members, after the leg writes and
        outside the context's lock.
        """
        members = await self._resolve_transit_members(target.spec, target.devices, context_name=label)
        await self.reconcile_vrfs({member.bl_port.device.id for member in members})

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

        self.client.group_context.related_node_ids.append(customer_id)
        self.logger.info(f"Processing {kind} deployment {customer.get('name', customer_id)}")
        await self.ensure_customer_pools(
            kind=kind,
            org_id=customer["owner"]["org_id"],
            environment=customer["environment"],
            design=customer.get("design") or {},
            footprint_name=customer.get("name", customer_id),
        )

        # Wait for each in-flight facility generator: it writes the firewall/LB devices read below.
        for facility in self._hosting_parents(customer):
            refreshed = await self.wait_for_parent_generator_and_refetch(self._parent_generators, facility["id"])
            if refreshed is not None:
                entries = clean_data(refreshed).get(kind, [])
                if not entries:
                    self.logger.error(f"No {kind} data in GraphQL response")
                    return
                customer = entries[0]

        # Read synchronously by create_devices() (devices.py _resolve_role_controller).
        parent = customer.get("parent")
        self.set_controllers_from(parent if isinstance(parent, dict) else {})
        facilities = self._hosting_parents(customer)
        if not facilities:
            message = (
                f"Deployment {customer.get('name', customer_id)}: no {self._parent_label} to provision its "
                "service contexts"
            )
            if self._parent_required:
                self.logger.error(message)
                return
            self.logger.info(message)

        # One run per context kind and facility; locks are per context, so the runs are sequential.
        for spec in self._context_specs:
            served: list[str] = []
            complete = True
            for facility in facilities:
                target = await self._resolve_context_target(spec, customer, customer_id, facility)
                if target is None:
                    continue
                context_id = await self._ensure_transit_legs(customer=customer, customer_id=customer_id, target=target)
                if context_id is None:
                    complete = False
                else:
                    served.append(context_id)
                    await self._reconcile_border_vrfs(target, context_id)
            # A many link is converged per spec (each drops only links of its own role); only a complete run may drop.
            if spec.serving_many and complete:
                await self.unlink_stale_serving_contexts(
                    spec,
                    customer_id=customer_id,
                    current_ids=self.serving_context_ids(customer, spec),
                    served_ids=served,
                )
