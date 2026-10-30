"""Shared ``generate()`` of the customer boarding generators that provision
service contexts on their parent facility's firewall and load-balancer pairs.

customer_dc.py (TopologyCustomerDC on a DataCenter) and customer_colocation.py
(TopologyCustomerColocation on every ColocationZone of its metro) differ only in
the customer kind, the hosting facilities (``_hosting_parents``: the DC, or the
zones of the metro), whether a facility is required, the parent generators to
wait for and the context kinds they provision — class attributes the host sets
(see FirewallContextMixin). The provisioning itself is
generators/firewall_context.py's FirewallContextMixin, one run per ContextSpec
and per hosting facility.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from utils.data_cleaning import clean_data

from ..firewall_context import FirewallContextMixin
from ..helpers.vrf import VrfMixin
from ..protocols import DcimInterface

if TYPE_CHECKING:
    from ..devices import DeviceMixin

# The two kinds that inherit DcimSwitchedInterface. A filter on the marker generic cannot carry the context's
# `capabilities` edge (it belongs to DcimInterface), so the ports of a context are read on DcimInterface and cut here.
_SWITCHED_PORT_KINDS = frozenset({"DcimPhysicalInterface", "DcimLAGInterface"})


class CustomerFacilityMixin(FirewallContextMixin, VrfMixin):
    """``generate()`` for a customer boarding generator on a facility with service pairs.

    The host class also mixes in PoolMixin, DeviceMixin and CablingMixin (see
    FirewallContextMixin) and CommonGenerator, and sets ``_customer_kind``,
    ``_parent_label``, ``_parent_generators`` and ``_context_specs``.
    """

    # A footprint with no hosting facility (a colocation metro without a zone) simply has no
    # contexts: an info log. A required one (a DC footprint's DC) that is missing is an error.
    _parent_required: bool = True

    # Provided by DeviceMixin.
    if TYPE_CHECKING:
        set_controllers_from = DeviceMixin.set_controllers_from

    def _hosting_parents(self, customer: dict[str, Any]) -> list[dict[str, Any]]:
        """The facilities whose firewall and load-balancer pairs host this footprint's contexts: its parent.

        A host whose parent owns no devices (a colocation metro: its zones do) overrides it.
        """
        parent = customer.get("parent")
        return [parent] if isinstance(parent, dict) and parent.get("id") else []

    async def _reconcile_border_vrfs(self, context_id: str) -> None:
        """Converge the VRF instances of the border devices whose service ports are tagged with the context.

        A leg puts an address on the firewall or load balancer, never on the border leaf or edge: the border's demand
        is the leg's namespace, read through the context tagged on its service port. The leg writes run under the
        context's lock (``fw-context-*``/``lb-context-*``), which ``_ensure_transit_legs`` has released by now, and a
        VRF lock is never taken under it; so the instance follows the leg, it does not precede it.
        """
        # One read on the generic: a service port is a physical port or a LAG; the context may also be tagged on the
        # hosted firewall's sub-interfaces, whose devices carry no instance (_SWITCHED_PORT_KINDS leaves them out).
        ports = await self.client.filters(kind=DcimInterface, capabilities__ids=[context_id], include=["device"])
        await self.reconcile_vrfs({port.device.id for port in ports if port.get_kind() in _SWITCHED_PORT_KINDS})

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

        # This reads facility-level data (firewall_devices, loadbalancer_devices)
        # written by the facility's own generator (the only owner of its HA
        # pairs). A customer can board concurrently with (or immediately
        # after) its facility's creation — e.g. a bulk load — so wait for each
        # in-flight facility generator and re-parse rather than risk provisioning
        # against a facility that has no firewall/LB devices yet (see pod.py's
        # identical wait for the same reason). Every hosting facility is waited
        # for, one with no firewall yet included: it may be about to get them.
        for facility in self._hosting_parents(customer):
            refreshed = await self.wait_for_parent_generator_and_refetch(self._parent_generators, facility["id"])
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

        # One run per context kind and hosting facility, independent of each other: a facility with
        # load balancers and no firewalls (or the reverse) still gets the one, and a metro with two firewall
        # zones gets a context on each. The contexts' locks are per context, so the runs are sequential.
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
                    await self._reconcile_border_vrfs(context_id)
            # A single serving link is replaced by link_serving_context; a many one is converged here. Only a run
            # that built every context it set out to may drop a link (a failed one raises under the generator
            # logger before this point).
            if self._serving_many and complete:
                await self.unlink_stale_serving_contexts(
                    spec,
                    customer_id=customer_id,
                    current_ids=self.serving_context_ids(customer, spec),
                    served_ids=served,
                )
