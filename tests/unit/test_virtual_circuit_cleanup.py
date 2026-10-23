"""The tracked cleanup of add_virtual_circuit, with the SDK's real group semantics.

infrahub_sdk's ``update_group`` returns BEFORE any cleanup when a run tracked nothing, so a run that builds nothing
(decommissioned circuit, an end missing, no carrier yet, a wholly decommissioned carrier) deletes the old session only
if the circuit itself is in the run's group. ``SdkTrackingGroup`` (tests/unit/simulators/circuit_fakes.py) mirrors that
function, so these tests fail if the generator stops tracking its target circuit.
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
                lambda: interconnect_payload(ends=[dc_border_port(circuits=[]), colo_edge_port(circuits=[])]),
                id="no-carrier",
            ),
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
    async def test_every_other_no_session_path_deletes_the_old_session_too(self, early: Callable[[], Any]) -> None:
        """An end missing, no carrier any more, a wholly decommissioned carrier: nothing is built, so the SDK would
        skip the cleanup unless the circuit is tracked."""
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
