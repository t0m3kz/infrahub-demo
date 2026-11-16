"""What the fabric's routing plans read before they plan (generators/helpers/routing_plan.py): the loopbacks, processes and
MLAG pairs of the devices of a layer pair, and the shared objects the DC generator creates (``SharedFacts``).

The shared overlay AS, the OSPF area and the overlay processes of the layer above are another generator's output (add_dc,
add_pod): a caller whose plan names those producers (``TopologyPlan.producers``) passes them, and this read waits for
the objects with the shared retry policy, going on without the overlay processes of the layer above when they never
appear. A caller with no producer (add_dc plans the shared objects itself) reads once.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from generators.capabilities.bgp import (
    Ref,
    RoutingReads,
    RoutingShared,
    SharedFacts,
    evpn_address_family,
    read_routing_reads,
    read_shared_facts,
)
from generators.helpers.routing import RoutingStrategy
from generators.plan import ExternalProducer
from utils.limits import WAIT_FOR_PEER_GENERATOR


@dataclass(frozen=True)
class RoutingInputs:
    """The reads of the routing of one or more layer pairs of a fabric."""

    reads: RoutingReads
    facts: SharedFacts


async def read_routing_inputs(
    generator: Any,
    *,
    fabric_name: str,
    strategy: str,
    names: Sequence[str],
    top: Sequence[str] = (),
    skip_underlay: bool = False,
    no_overlay: bool = False,
    producers: Sequence[ExternalProducer] = (),
) -> RoutingInputs:
    """Read the routing state of ``names`` and the fabric's shared objects, retrying while the shared overlay AS, the OSPF
    area (when the underlay is OSPF and not skipped) or the overlay processes of the ``top`` devices are not there yet,
    as long as ``producers`` (the plan's) may still create them; with none, one read.
    """
    needs_overlay_as = not no_overlay and strategy in (RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value)
    needs_ospf_area = strategy == RoutingStrategy.OSPF_IBGP.value and not skip_underlay
    reads, facts = RoutingReads(), SharedFacts()
    attempts = WAIT_FOR_PEER_GENERATOR.attempts if producers else 1
    for attempt in range(attempts):
        reads = await read_routing_reads(generator.client, list(names))
        facts = await read_shared_facts(generator.client, fabric_name)
        waiting_on = []
        if needs_overlay_as and facts.overlay_as_id is None:
            waiting_on.append("shared overlay AS")
        if needs_ospf_area and facts.ospf_area_id is None:
            waiting_on.append("shared OSPF area")
        if missing := sorted(set(top) - reads.overlay) if not no_overlay else []:
            waiting_on.append(f"overlay BGP for {missing}")
        if not waiting_on:
            break
        # A producer run in flight is awaited instead of sleeping; one that failed fails this run at once
        waited = False
        for producer in producers:
            waited = await generator.wait_for_parent_generators(producer.generators, producer.parent_id) or waited
        if attempt < attempts - 1 and not waited:
            delay = generator._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt)
            generator.logger.info(
                f"Waiting on: {', '.join(waiting_on)} — retrying in {delay:.2f}s "
                f"(attempt {attempt + 1}/{WAIT_FOR_PEER_GENERATOR.attempts})"
            )
            await asyncio.sleep(delay)
    return RoutingInputs(reads=reads, facts=facts)


def routing_shared(
    facts: SharedFacts,
    *,
    strategy: str,
    asn_pool: Ref | None = None,
    shared_underlay_as: Ref | None = None,
    skip_underlay: bool = False,
    no_overlay: bool = False,
) -> RoutingShared:
    """The ``RoutingShared`` of a routing call: the shared objects as they exist, the EVPN address family unless there is
    no overlay.
    """
    return RoutingShared(
        strategy=strategy,
        asn_pool=asn_pool,
        overlay_as=None if no_overlay else facts.overlay_as_id,
        ospf_area=facts.ospf_area_id,
        underlay_password=None if skip_underlay else facts.underlay_password_id,
        overlay_password=None if no_overlay else facts.overlay_password_id,
        evpn_address_family=None if no_overlay else evpn_address_family(facts.evpn_address_family_id),
        shared_underlay_as=shared_underlay_as,
        skip_underlay=skip_underlay,
        no_overlay=no_overlay,
    )
