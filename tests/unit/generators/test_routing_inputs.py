"""read_routing_inputs waits for the shared routing objects only while the plan names a generator that creates them."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from generators.capabilities.bgp import RoutingReads, SharedFacts
from generators.dc import routing_inputs
from generators.helpers.routing import RoutingStrategy
from generators.plan import ExternalProducer
from utils.limits import WAIT_FOR_PEER_GENERATOR


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The two reads, counted; the shared facts have no overlay AS and no OSPF area (a fabric's first run)."""
    shared = AsyncMock(return_value=SharedFacts())
    monkeypatch.setattr(routing_inputs, "read_routing_reads", AsyncMock(return_value=RoutingReads()))
    monkeypatch.setattr(routing_inputs, "read_shared_facts", shared)
    sleep = AsyncMock()
    monkeypatch.setattr(routing_inputs.asyncio, "sleep", sleep)
    return {"shared": shared, "sleep": sleep}


def _generator() -> MagicMock:
    """A generator with a logger and the retry delay, whose producers are not running."""
    generator = MagicMock()
    generator._retry_delay = lambda delay, attempt: delay
    generator.wait_for_parent_generators = AsyncMock(return_value=False)
    return generator


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", [RoutingStrategy.EBGP_IBGP.value, RoutingStrategy.OSPF_IBGP.value])
async def test_a_caller_with_no_producer_reads_once(reads: dict[str, Any], strategy: str) -> None:
    """add_dc names no producer (it plans the shared overlay AS and OSPF area itself): their absence is no reason to
    wait.
    """
    await routing_inputs.read_routing_inputs(
        _generator(), fabric_name="dc6", strategy=strategy, names=["dc6-super-spine-01"]
    )

    assert reads["shared"].await_count == 1
    reads["sleep"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_pod_waits_for_the_overlay_as_its_producer_creates(reads: dict[str, Any]) -> None:
    """A pod names add_dc as its producer: it retries while the DC's overlay AS is missing, then goes on with what it
    read.
    """
    inputs = await routing_inputs.read_routing_inputs(
        _generator(),
        fabric_name="dc6",
        strategy=RoutingStrategy.EBGP_IBGP.value,
        names=["dc6-pod-1-spine-01"],
        producers=[ExternalProducer(("add_dc",), "dc-1")],
    )

    assert reads["shared"].await_count == WAIT_FOR_PEER_GENERATOR.attempts
    assert reads["sleep"].await_count == WAIT_FOR_PEER_GENERATOR.attempts - 1
    assert inputs.facts.overlay_as_id is None


@pytest.mark.asyncio
async def test_a_producer_that_failed_stops_the_wait_at_once(reads: dict[str, Any]) -> None:
    """Between attempts the producers are asked: one whose last run failed raises there (the generator's error log),
    so the read never sleeps through its retries.
    """
    generator = _generator()
    generator.wait_for_parent_generators = AsyncMock(side_effect=RuntimeError("Parent generator 'add_dc' failed"))

    with pytest.raises(RuntimeError, match="add_dc"):
        await routing_inputs.read_routing_inputs(
            generator,
            fabric_name="dc6",
            strategy=RoutingStrategy.EBGP_IBGP.value,
            names=["dc6-pod-1-spine-01"],
            producers=[ExternalProducer(("add_dc",), "dc-1")],
        )

    assert reads["shared"].await_count == 1
    reads["sleep"].assert_not_awaited()
