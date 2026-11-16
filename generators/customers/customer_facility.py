"""The ``generate()`` of the customer boarding generators that provision service contexts on a facility.
customer_dc.py and customer_colocation.py differ only in their ``FacilityConfig``; the provisioning is firewall_context.py's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from generators.base import BaseGenerator
from generators.capabilities.vrf import reconcile_vrfs
from generators.customers.firewall_context import (
    ContextSpec,
    ensure_transit_legs,
    resolve_context_target,
    serving_context_ids,
    unlink_stale_serving_contexts,
)
from generators.dcim.controllers import controllers_of
from generators.plan import CustomerPoolsSpec, TopologyPlan
from utils.data_cleaning import clean_data


def hosting_parent(customer: dict[str, Any]) -> list[dict[str, Any]]:
    """The facilities whose pairs host this footprint's contexts: its parent."""
    parent = customer.get("parent")
    return [parent] if isinstance(parent, dict) and parent.get("id") else []


@dataclass(frozen=True)
class FacilityConfig:
    """What differs between the customer boarding generators: the footprint kind, what its facility is called in a
    message, the generators that own the facility's HA pairs (waited for), the context kinds it provisions and the
    facilities that host them. ``parent_required`` False: a footprint with no hosting facility (a metro without a zone)
    just has no contexts.
    """

    customer_kind: str
    parent_label: str
    parent_generators: tuple[str, ...]
    context_specs: tuple[ContextSpec, ...]
    parent_required: bool = True
    hosting_parents: Callable[[dict[str, Any]], list[dict[str, Any]]] = field(default=hosting_parent)


async def board_customer(gen: BaseGenerator, data: dict[str, Any], config: FacilityConfig) -> None:
    """Board a customer footprint: its pools, then per context kind and facility its context and legs, and the serving
    links the run converges. Facility pairs another generator is still writing are waited for.
    """
    kind = config.customer_kind
    cleaned = clean_data(data)

    entries = cleaned.get(kind, [])
    if not entries:
        gen.logger.info(f"No {kind} data in GraphQL response — not this generator's kind")
        return
    customer = entries[0]

    customer_id: str = customer.get("id", "")
    if not customer_id:
        gen.fail("Deployment missing id — cannot proceed")

    gen.logger.info(f"Processing {kind} deployment {customer.get('name', customer_id)}")
    # The plan root is the customer: tracked first, then its pools are boarded
    await gen.apply_plan(
        TopologyPlan(
            root_id=customer_id,
            specs=[
                CustomerPoolsSpec(
                    kind=kind,
                    org_id=customer["owner"]["org_id"],
                    environment=customer["environment"],
                    design=customer.get("design") or {},
                    footprint_name=customer.get("name", customer_id),
                )
            ],
        )
    )

    # Wait for each in-flight facility generator: it writes the firewall/LB devices read below.
    for facility in config.hosting_parents(customer):
        refreshed = await gen.wait_for_parent_generator_and_refetch(config.parent_generators, facility["id"])
        if refreshed is not None:
            entries = clean_data(refreshed).get(kind, [])
            if not entries:
                gen.fail(f"No {kind} data in GraphQL response")
            customer = entries[0]

    parent = customer.get("parent")
    controllers = controllers_of(parent if isinstance(parent, dict) else {})
    facilities = config.hosting_parents(customer)
    if not facilities:
        message = f"Deployment {customer.get('name', customer_id)}: no {config.parent_label} to provision its service contexts"
        if config.parent_required:
            gen.fail(message)
        gen.logger.info(message)

    # One run per context kind and facility; locks are per context, so the runs are sequential.
    for spec in config.context_specs:
        served: list[str] = []
        for facility in facilities:
            target = await resolve_context_target(
                gen,
                spec,
                customer,
                customer_id,
                facility,
                parent_generators=config.parent_generators,
                controllers=controllers,
            )
            if target is None:
                continue
            outcome = await ensure_transit_legs(
                gen, customer=customer, customer_id=customer_id, customer_kind=kind, target=target
            )
            served.append(outcome.context_id)
            # After the leg writes and outside the context's lock
            await reconcile_vrfs(gen, outcome.border_device_ids)
        # A many link is converged per spec (each drops only links of its own role)
        if spec.serving_many:
            await unlink_stale_serving_contexts(
                gen,
                spec,
                customer_kind=kind,
                customer_id=customer_id,
                current_ids=serving_context_ids(customer, spec),
                served_ids=served,
            )
