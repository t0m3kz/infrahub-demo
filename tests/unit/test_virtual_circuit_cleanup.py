"""The tracked cleanup of add_virtual_circuit, with the SDK's real group semantics.

infrahub_sdk's ``update_group`` returns BEFORE any cleanup when a run tracked nothing. Cleanup therefore runs for
EXPLICIT reasons only, the ones where the generator tracks the circuit (its target) so the group is never empty: the
circuit is decommissioned, it has fewer than two ends, or its whole carrier is decommissioned. A run that merely does
not see enough carried ends (no carrier found, one end of an interconnect only) cannot tell "not linked yet / not
visible yet" from "gone": it tracks nothing, the SDK cleans nothing and the session an earlier run built stays.
``SdkTrackingGroup`` (tests/unit/simulators/circuit_fakes.py) mirrors the SDK function, so these tests fail if the
generator stops tracking on an explicit path, or starts tracking on the transient one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tests.unit.simulators.circuit_fakes import (
    VC_ID,
    FakeClient,
    SdkTrackingGroup,
    colo_edge_port,
    dc_border_port,
    handoff_edge_port,
    handoff_party_port,
    handoff_payload,
    interconnect_payload,
    session_generator,
    tunnel_payload,
    underlay_circuit,
)

DCI = "VC-DCI-DC10-FR2"
HANDOFF = "VC-C001-EQXFR-AWS"
PayloadFactory = Callable[..., dict[str, Any]]


async def _run(client: FakeClient, group: SdkTrackingGroup, payload: dict[str, Any]) -> set[str]:
    """One add_virtual_circuit run, then the SDK's end-of-run group update; returns the ids it deletes as unused."""
    group.start_run()
    await session_generator(client).generate(payload)
    return group.finish_run()


def _outputs(client: FakeClient, circuit: str, suffixes: tuple[str, ...]) -> set[str]:
    """The tracked outputs of a full run: the prefix, its two addresses and the peerings."""
    return {
        *(f"ManagedBGPPeering:{circuit}-{suffix}" for suffix in suffixes),
        *(label for label in client.claimed() if label.startswith(("prefix-", "IpamIPAddress:"))),
    }


class TestTheGroupSemanticsOfTheFake:
    def test_a_run_that_tracked_nothing_cleans_nothing(self) -> None:
        """The SDK skips the whole update (and so the cleanup) when the run's group would be empty."""
        client = FakeClient()
        group = SdkTrackingGroup(client)
        group.previous = {"old-peering"}

        group.start_run()

        assert group.finish_run() == set()
        assert group.previous == {"old-peering"}

    def test_a_run_with_members_deletes_the_previous_ones_it_did_not_reach(self) -> None:
        """A non-empty group: previous members minus current ones are deleted, the current ones become the baseline."""
        client = FakeClient()
        group = SdkTrackingGroup(client)
        group.previous = {"old-peering", VC_ID}

        group.start_run()
        client.targets.append(VC_ID)

        assert group.finish_run() == {"old-peering"}
        assert group.previous == {VC_ID}


class TestEarlyReturnsCleanTheOldSession:
    @pytest.mark.parametrize(
        ("full", "circuit", "suffixes"),
        [
            pytest.param(interconnect_payload, DCI, ("underlay", "evpn"), id="interconnect"),
            pytest.param(handoff_payload, HANDOFF, ("bgp",), id="handoff"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_decommissioned_circuit_deletes_the_session_it_built(
        self, full: PayloadFactory, circuit: str, suffixes: tuple[str, ...]
    ) -> None:
        """Full run, then the circuit is decommissioned: peerings, prefix and addresses are deleted; the circuit, its
        interfaces and the key are not; a later full run rebuilds them (and the interconnect with the SAME key)."""
        client = FakeClient()
        group = SdkTrackingGroup(client)

        assert await _run(client, group, full()) == set()
        built = _outputs(client, circuit, suffixes)
        assert len(built) == len(suffixes) + 3
        key_passwords = [data["password"] for data, _ in client.created_of("RoutingPassword")]

        deleted = await _run(client, group, full(status="decommissioned"))

        assert deleted == built
        assert VC_ID not in deleted
        assert not any(label.startswith(("if-", "RoutingPassword:")) for label in deleted)

        assert await _run(client, group, full()) == set()
        assert _outputs(client, circuit, suffixes) == built
        assert [data["password"] for data, _ in client.created_of("RoutingPassword")] == key_passwords

    @pytest.mark.parametrize(
        "early",
        [
            pytest.param(lambda: interconnect_payload(ends=[dc_border_port()]), id="one-end"),
            pytest.param(
                lambda: interconnect_payload(
                    ends=[
                        dc_border_port(circuits=[underlay_circuit(status="decommissioned")]),
                        colo_edge_port(circuits=[underlay_circuit(status="decommissioned")]),
                    ]
                ),
                id="whole-carrier-decommissioned",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_other_explicit_no_session_paths_delete_the_old_session_too(
        self, early: Callable[[], Any]
    ) -> None:
        """An end missing, a wholly decommissioned carrier: nothing is built, so the SDK would skip the cleanup unless
        the circuit is tracked. (No carrier at all is NOT one of them: see TestTransientNoCarrierKeepsTheSession.)"""
        client = FakeClient()
        group = SdkTrackingGroup(client)
        await _run(client, group, interconnect_payload())
        built = _outputs(client, DCI, ("underlay", "evpn"))

        deleted = await _run(client, group, early())

        assert deleted == built
        assert VC_ID not in deleted

    @pytest.mark.asyncio
    async def test_a_tunnel_is_never_tracked_or_cleaned(self) -> None:
        """A tunnel has no session and no tracking group of its own to update: the generator stops before tracking."""
        client = FakeClient()
        group = SdkTrackingGroup(client)

        assert await _run(client, group, tunnel_payload()) == set()
        assert client.targets == []


class TestTransientNoCarrierKeepsTheSession:
    """Not enough carried ends (the physical circuit or its ports are not linked, or not visible yet) is no reason
    to delete a good session: the run tracks nothing, so the SDK cleans nothing."""

    @pytest.mark.parametrize(
        ("full", "circuit", "suffixes", "transient"),
        [
            pytest.param(
                interconnect_payload,
                DCI,
                ("underlay", "evpn"),
                lambda: interconnect_payload(ends=[dc_border_port(circuits=[]), colo_edge_port(circuits=[])]),
                id="interconnect-no-carrier",
            ),
            pytest.param(
                interconnect_payload,
                DCI,
                ("underlay", "evpn"),
                lambda: interconnect_payload(ends=[dc_border_port(), colo_edge_port(circuits=[])]),
                id="interconnect-one-end-carried-only",
            ),
            pytest.param(
                handoff_payload,
                HANDOFF,
                ("bgp",),
                lambda: handoff_payload(
                    ends=[handoff_edge_port(parent_circuits=[]), handoff_party_port(parent_circuits=[])]
                ),
                id="handoff-no-carrier",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_run_that_sees_no_carrier_deletes_nothing_and_the_next_full_run_duplicates_nothing(
        self, full: PayloadFactory, circuit: str, suffixes: tuple[str, ...], transient: Callable[[], Any]
    ) -> None:
        """Full run, then a run that transiently sees too few carried ends: it writes, tracks and deletes nothing, so
        peerings, prefix and addresses stay; the following full run converges on the same objects."""
        client = FakeClient()
        group = SdkTrackingGroup(client)
        assert await _run(client, group, full()) == set()
        built = _outputs(client, circuit, suffixes)
        baseline = set(group.previous)
        writes, targets = len(client.writes), len(client.targets)

        assert await _run(client, group, transient()) == set()

        assert len(client.writes) == writes
        assert len(client.targets) == targets
        assert group.previous == baseline
        assert _outputs(client, circuit, suffixes) == built

        assert await _run(client, group, full()) == set()
        assert _outputs(client, circuit, suffixes) == built
        assert group.previous == baseline
        assert len({allocation["identifier"] for allocation in client.allocations}) == 1
        assert len(client.prefixes) == 1 and len(client.addresses) == 2

    @pytest.mark.asyncio
    async def test_a_transient_run_then_an_explicit_reason_still_cleans_up(self) -> None:
        """Keeping on a transient read does not defeat the explicit cleanup that follows it."""
        client = FakeClient()
        group = SdkTrackingGroup(client)
        await _run(client, group, interconnect_payload())
        built = _outputs(client, DCI, ("underlay", "evpn"))

        assert (
            await _run(client, group, interconnect_payload(ends=[dc_border_port(circuits=[]), colo_edge_port()]))
            == set()
        )
        deleted = await _run(client, group, interconnect_payload(status="decommissioned"))

        assert deleted == built

    @pytest.mark.asyncio
    async def test_the_first_ever_run_without_a_carrier_is_a_plain_no_op(self) -> None:
        """Nothing was built before: the run tracks and writes nothing, and says it keeps nothing to delete."""
        client = FakeClient()
        group = SdkTrackingGroup(client)

        assert (
            await _run(client, group, interconnect_payload(ends=[dc_border_port(circuits=[]), colo_edge_port()]))
            == set()
        )

        assert client.writes == [] and client.targets == [] and group.previous == set()
