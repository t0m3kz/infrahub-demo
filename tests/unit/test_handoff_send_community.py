"""A handoff circuit's eBGP session sends no communities; the interconnect sessions keep today's behaviour."""

from __future__ import annotations

from typing import Any

import pytest

from generators.topology.virtual_circuit import PeeringPlan, SessionEnd, SessionPlan
from tests.unit.simulators.circuit_fakes import FakeClient, handoff_payload, interconnect_payload, session_generator
from utils.data_cleaning import clean_data


def _plan(payload: dict[str, Any]) -> SessionPlan:
    """The plan the kind's planner builds for a payload (pure: no client call)."""
    gen: Any = session_generator(FakeClient())
    (circuit,) = clean_data(payload)["TopologySessionCircuit"]
    ends = [end for port in circuit["capabilities"] if (end := SessionEnd.parse(port)) is not None]
    planner = gen._PLANNERS[circuit["circuit_type"]]
    return planner(gen, circuit, circuit["name"], ends)


def _peerings(plan: SessionPlan) -> dict[str, PeeringPlan]:
    """The plan's peerings by suffix."""
    return {peering.suffix: peering for peering in plan.peerings}


def test_peering_plan_defaults_to_sending_communities() -> None:
    """Failure side: a plan that does not say otherwise still sends standard communities."""
    plan = PeeringPlan(
        suffix="x",
        description="d",
        peering_role="regular",
        session_type="EBGP",
        address_family=("ipv4", "unicast"),
        interface_ids=("a", "b"),
    )

    assert plan.send_community is True
    assert plan.send_extended_community is False


def test_handoff_plan_sends_no_communities() -> None:
    """The handoff session has nothing to strip yet: neither standard nor extended communities are sent."""
    (peering,) = _plan(handoff_payload()).peerings

    assert peering.peering_role == "regular"
    assert peering.send_community is False
    assert peering.send_extended_community is False


def test_interconnect_plans_keep_send_community() -> None:
    """The DCI underlay and EVPN sessions still send communities; only the EVPN one sends extended ones."""
    peerings = _peerings(_plan(interconnect_payload()))

    assert set(peerings) == {"underlay", "evpn"}
    assert peerings["underlay"].send_community is True
    assert peerings["underlay"].send_extended_community is False
    assert peerings["evpn"].send_community is True
    assert peerings["evpn"].send_extended_community is True


@pytest.mark.asyncio
async def test_handoff_session_is_written_without_communities() -> None:
    """End to end over the fake client: the upserted handoff peering carries both flags False."""
    client = FakeClient()

    await session_generator(client).generate(handoff_payload())

    (peering,) = [data for data, _ in client.created_of("ManagedBGPPeering")]
    assert peering["send_community"] is False
    assert peering["send_extended_community"] is False


@pytest.mark.asyncio
async def test_interconnect_sessions_are_written_with_communities() -> None:
    """End to end: neither DCI peering loses send_community."""
    client = FakeClient()

    await session_generator(client).generate(interconnect_payload())

    peerings = [data for data, _ in client.created_of("ManagedBGPPeering")]
    assert len(peerings) == 2
    assert all(data["send_community"] is True for data in peerings)


@pytest.mark.asyncio
async def test_rerunning_the_handoff_upserts_the_same_peering_with_the_flags_still_false() -> None:
    """Idempotency at the data-dict level: the second run writes the same name and key, flags unchanged."""
    client = FakeClient()

    await session_generator(client).generate(handoff_payload())
    await session_generator(client).generate(handoff_payload())

    first, second = [data for data, _ in client.created_of("ManagedBGPPeering")]
    assert first == second
    assert second["send_community"] is False
    assert second["send_extended_community"] is False


@pytest.mark.asyncio
async def test_an_interconnect_rerun_does_not_turn_the_handoff_default_on_its_peerings() -> None:
    """Failure side: the handoff opt-out is per plan, a later DCI run in the same client keeps both flags as planned."""
    client = FakeClient()

    await session_generator(client).generate(handoff_payload())
    await session_generator(client).generate(interconnect_payload())

    handoff, *interconnect = [data for data, _ in client.created_of("ManagedBGPPeering")]
    assert handoff["send_community"] is False
    assert [data["send_community"] for data in interconnect] == [True, True]
