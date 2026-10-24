"""Unit tests for the session generator (generators/topology/virtual_circuit.py, add_virtual_circuit).

An interconnect circuit builds a DCI (underlay eBGP on a /31 plus a loopback-to-loopback EVPN session), a handoff
circuit one eBGP session over a /31 of its namespace's pool, a tunnel circuit nothing. Every precondition must fail
before the first write. Fakes: tests/unit/simulators/circuit_fakes.py.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from generators.logger import GeneratorError
from generators.topology.circuit import PHYSICAL_CIRCUIT_GENERATOR
from generators.topology.virtual_circuit import SessionEnd, VirtualCircuitSessionGenerator
from tests.unit.simulators.circuit_fakes import (
    VC_ID,
    FakeClient,
    colo_edge_port,
    dc_border_port,
    handoff_edge_port,
    handoff_party_port,
    handoff_payload,
    interconnect_payload,
    loopback,
    session_generator,
    session_payload,
    session_port,
    tunnel_payload,
    underlay_circuit,
)
from tests.unit.virtual_circuit_session_helpers import (
    DCI,
    HANDOFF,
    _assert_refused,
    _peering,
    _run,
)
from utils.circuit_underlay import SESSION_CIRCUIT_KINDS
from utils.data_cleaning import clean_data


class TestNothingToBuild:
    """A run that has no session to build writes nothing, and says so without failing."""

    @pytest.mark.asyncio
    async def test_a_tunnel_circuit_has_no_session(self) -> None:
        """A tunnel circuit is the SD-WAN orchestrator's, so the run only logs that and writes nothing."""
        client = FakeClient()

        gen = await _run(client, tunnel_payload())

        assert client.writes == [] and client.filter_calls == [] and client.get_calls == []
        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        assert "no session is generated" in gen.logger.info.call_args_list[-1].args[0]

    @pytest.mark.parametrize("payload_for", [handoff_payload, interconnect_payload])
    @pytest.mark.asyncio
    async def test_a_decommissioned_circuit_builds_nothing(self, payload_for: Any) -> None:
        """Not an error: the run's tracked cleanup removes what an earlier run built."""
        client = FakeClient()

        gen = await _run(client, payload_for(status="decommissioned"))

        assert client.writes == [] and client.filter_calls == []
        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        assert any("decommissioned" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.parametrize("ends", [[], [handoff_edge_port()]], ids=["no-end", "one-end"])
    @pytest.mark.asyncio
    async def test_fewer_than_two_ends_builds_nothing(self, ends: list[dict[str, Any]]) -> None:
        """A session needs two ends: with none or one the run only logs it, as a port may be linked later."""
        client = FakeClient()

        gen = await _run(client, session_payload("TopologyHandoffCircuit", ends))

        assert client.writes == [] and client.filter_calls == []
        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        assert any("session needs 2" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(
                handoff_payload(
                    ends=[
                        handoff_edge_port(),
                        handoff_party_port(),
                        session_port("if-aws2", "Ethernet2.1900", "EXT-AWS-DX-FR3", kind="DcimVirtualInterface"),
                    ]
                ),
                r"a session needs exactly 2 ends, found 3: ",
                id="three-ends",
            ),
            pytest.param(
                handoff_payload(
                    ends=[
                        handoff_edge_port(),
                        session_port("if-other", "Ethernet1/11.1901", "eg-fr201", kind="DcimVirtualInterface"),
                    ]
                ),
                r"the 2 ends must be on 2 different devices, both are on eg-fr201",
                id="one-device",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_malformed_pair_of_ends_is_an_error(self, payload: dict[str, Any], message: str) -> None:
        """Three ends, or two ends on one device, cannot be a session: refused with nothing written."""
        await _assert_refused(payload, message)

    @pytest.mark.parametrize(
        ("payload_for", "ends"),
        [
            pytest.param(
                handoff_payload,
                [handoff_edge_port(parent_circuits=[]), handoff_party_port()],
                id="handoff-no-circuit-and-no-cable",
            ),
            pytest.param(
                interconnect_payload,
                [dc_border_port(circuits=[]), colo_edge_port(circuits=[])],
                id="interconnect-no-circuit-and-no-cable",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_not_enough_carried_ends_builds_nothing(self, payload_for: Any, ends: list[dict[str, Any]]) -> None:
        """Neither a circuit nor a cable on the ends that need one: an info line, no error, no write.

        add_circuit reruns this generator once a port is linked to a circuit.
        """
        client = FakeClient()

        gen = await _run(client, payload_for(ends=ends))

        assert client.writes == [] and client.filter_calls == []
        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        assert any("not linked to a physical carrier yet" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_an_interconnect_with_one_uncarried_end_builds_nothing(self) -> None:
        """A DCI needs both ends carried (a handoff needs one): the carried end's circuit is still waited for."""
        client = FakeClient()

        gen = await _run(client, interconnect_payload(ends=[dc_border_port(circuits=[]), colo_edge_port()]))

        assert client.writes == [] and client.filter_calls == []
        assert any("not linked to a physical carrier yet" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_an_interconnect_without_a_carrier_builds_nothing(self) -> None:
        """A DCI whose two ends ride no physical circuit and no cable has nothing to run over, so nothing is built."""
        client = FakeClient()
        ends = [dc_border_port(circuits=[]), colo_edge_port(circuits=[])]

        await _run(client, interconnect_payload(ends=ends))

        assert client.writes == [] and client.filter_calls == []

    @pytest.mark.asyncio
    async def test_a_wholly_decommissioned_carrier_builds_nothing(self) -> None:
        """The only carrier is decommissioned and the party end has no cable: nothing live carries the handoff."""
        client = FakeClient()
        dead = underlay_circuit("XC-FR2-AWS", status="decommissioned")
        ends = [handoff_edge_port(parent_circuits=[dead]), handoff_party_port()]

        await _run(client, handoff_payload(ends=ends))

        assert client.writes == [] and client.filter_calls == []

    @pytest.mark.asyncio
    async def test_a_decommissioned_circuit_and_no_cable_on_the_other_end_builds_nothing(self) -> None:
        """The party end has neither: nothing live carries the handoff."""
        client = FakeClient()
        dead = underlay_circuit("XC-FR2-AWS", status="decommissioned")
        ends = [handoff_edge_port(parent_circuits=[dead]), handoff_party_port(parent_cabled=False)]

        gen = await _run(client, handoff_payload(ends=ends))

        assert client.writes == [] and client.filter_calls == []
        assert any("whole carrier is decommissioned" in call.args[0] for call in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_a_cable_beats_a_decommissioned_circuit(self) -> None:
        """A cable has no status: a cabled end is live whatever the dead circuit beside it."""
        client = FakeClient()
        dead = underlay_circuit("XC-FR2-AWS", status="decommissioned")
        ends = [handoff_edge_port(parent_circuits=[dead], parent_cabled=True), handoff_party_port()]

        await _run(client, handoff_payload(ends=ends))

        assert _peering(client, "bgp")["name"] == f"{HANDOFF}-bgp"

    @pytest.mark.asyncio
    async def test_one_live_carrier_among_decommissioned_ones_still_builds(self) -> None:
        """A single live circuit is enough, whatever retired circuits also sit on the port."""
        client = FakeClient()
        carriers = [underlay_circuit("XC-OLD", status="decommissioned"), underlay_circuit("XC-FR2-AWS")]
        ends = [handoff_edge_port(parent_circuits=carriers), handoff_party_port()]

        await _run(client, handoff_payload(ends=ends))

        assert _peering(client, "bgp")["name"] == f"{HANDOFF}-bgp"

    @pytest.mark.asyncio
    async def test_empty_response_is_an_error(self) -> None:
        """A query response with no circuit at all is an error, not a silent no-op."""
        with pytest.raises(GeneratorError, match="No TopologyVirtualCircuit data"):
            await _run(FakeClient(), {"TopologyVirtualCircuit": {"edges": []}})


class TestCableCarrier:
    """A hop inside one facility is a DcimCable, not a circuit: a cabled port is always carried."""

    @pytest.mark.asyncio
    async def test_a_cable_only_handoff_builds_without_waiting(self) -> None:
        """A cable on the parent of the sub-interface, no physical circuit anywhere: nothing to wait for, built.

        (A handoff end is always a sub-interface, so the cable is on its parent.)
        """
        client = FakeClient()
        edge = handoff_edge_port(parent_circuits=[], parent_cabled=True)

        gen = await _run(client, handoff_payload(ends=[edge, handoff_party_port()]))

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()
        assert _peering(client, "bgp")["name"] == f"{HANDOFF}-bgp"
        assert client.allocations

    @pytest.mark.asyncio
    async def test_a_cabled_party_end_is_enough_for_a_handoff(self) -> None:
        """One carried end is enough for a handoff, whichever it is."""
        client = FakeClient()
        ends = [handoff_edge_port(parent_circuits=[]), handoff_party_port(parent_cabled=True)]

        await _run(client, handoff_payload(ends=ends))

        assert _peering(client, "bgp")["name"] == f"{HANDOFF}-bgp"

    @pytest.mark.asyncio
    async def test_a_cabled_interconnect_end_counts_as_carried(self) -> None:
        """An interconnect needs both ends carried: a cable on one and a circuit on the other do."""
        client = FakeClient()
        ends = [dc_border_port(circuits=[], cabled=True), colo_edge_port()]

        await _run(client, interconnect_payload(ends=ends))

        assert _peering(client, "underlay")["name"] == f"{DCI}-underlay"

    @pytest.mark.asyncio
    async def test_the_circuits_beside_a_cable_are_still_waited_for(self) -> None:
        """A cabled end does not skip the wait for the circuit on the other."""
        client = FakeClient()
        ends = [
            handoff_edge_port(parent_circuits=[], parent_cabled=True),
            handoff_party_port(parent_circuits=[underlay_circuit("XC-B")]),
        ]

        gen = await _run(client, handoff_payload(ends=ends))

        assert [call.args for call in gen.wait_for_parent_generator_and_refetch.await_args_list] == [
            (PHYSICAL_CIRCUIT_GENERATOR, "pc-XC-B")
        ]


class TestWaitForTheCarrier:
    @pytest.mark.asyncio
    async def test_waits_for_add_circuit_on_every_carrier(self) -> None:
        """One wait per physical circuit the ports ride, whichever port (or parent) it terminates on."""
        client = FakeClient()
        other = underlay_circuit("XC-FR2-AWS-B")
        ends = [handoff_edge_port(), handoff_party_port(parent_circuits=[other])]

        gen = await _run(client, handoff_payload(ends=ends))

        waited = [call.args for call in gen.wait_for_parent_generator_and_refetch.await_args_list]
        assert waited == [
            (PHYSICAL_CIRCUIT_GENERATOR, "pc-XC-FR2-AWS"),
            (PHYSICAL_CIRCUIT_GENERATOR, "pc-XC-FR2-AWS-B"),
        ]

    @pytest.mark.asyncio
    async def test_the_refetched_circuit_is_the_one_built_from(self) -> None:
        """What add_circuit changed while this run waited decides: here the circuit was decommissioned meanwhile."""
        client = FakeClient()
        gen = session_generator(client)
        gen.wait_for_parent_generator_and_refetch.return_value = handoff_payload(status="decommissioned")

        await gen.generate(handoff_payload())

        assert client.writes == []


class TestWaitsForTheCarriersAtOnce:
    """The carriers are independent: all waits start before any finishes, and the data built from is the last
    non-None refetch in carrier order."""

    @staticmethod
    def _three_carriers() -> dict[str, Any]:
        """A handoff on three physical circuits: two under the edge's parent port, one under the party's."""
        ends = [
            handoff_edge_port(parent_circuits=[underlay_circuit("XC-A"), underlay_circuit("XC-B")]),
            handoff_party_port(parent_circuits=[underlay_circuit("XC-C")]),
        ]
        return handoff_payload(ends=ends)

    @staticmethod
    async def _generate_with_refetches(
        refetched: dict[str, dict[str, Any] | None],
    ) -> tuple[FakeClient, list[str]]:
        """Run over ``refetched`` (carrier id -> what its wait returns); no wait returns before all have started."""
        client = FakeClient()
        gen = session_generator(client)
        started: list[str] = []
        all_started = asyncio.Event()

        async def wait(generator: str, parent_id: str) -> dict[str, Any] | None:
            assert generator == PHYSICAL_CIRCUIT_GENERATOR
            started.append(parent_id)
            if len(started) == len(refetched):
                all_started.set()
            await all_started.wait()  # sequential waits would never get past the first one
            return refetched[parent_id]

        gen.wait_for_parent_generator_and_refetch = wait
        await asyncio.wait_for(gen.generate(TestWaitsForTheCarriersAtOnce._three_carriers()), timeout=2)
        return client, started

    @pytest.mark.asyncio
    async def test_every_wait_starts_before_any_finishes(self) -> None:
        """Three carriers, three waits in flight together (a sequential loop would time out here)."""
        _, started = await self._generate_with_refetches({"pc-XC-A": None, "pc-XC-B": None, "pc-XC-C": None})

        assert sorted(started) == ["pc-XC-A", "pc-XC-B", "pc-XC-C"]

    @pytest.mark.parametrize(
        ("refetched", "builds"),
        [
            pytest.param(
                {"pc-XC-A": handoff_payload(), "pc-XC-B": None, "pc-XC-C": handoff_payload(status="decommissioned")},
                False,
                id="last-non-none-is-the-last-carrier",
            ),
            pytest.param(
                {"pc-XC-A": handoff_payload(status="decommissioned"), "pc-XC-B": None, "pc-XC-C": handoff_payload()},
                True,
                id="first-is-stale-last-builds",
            ),
            pytest.param(
                {"pc-XC-A": handoff_payload(status="decommissioned"), "pc-XC-B": handoff_payload(), "pc-XC-C": None},
                True,
                id="trailing-none-is-skipped",
            ),
            pytest.param(
                {"pc-XC-A": None, "pc-XC-B": None, "pc-XC-C": None},
                True,
                id="no-refetch-builds-from-the-original",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_last_non_none_refetch_is_the_one_built_from(
        self, refetched: dict[str, dict[str, Any] | None], builds: bool
    ) -> None:
        """A decommissioned last refetch builds nothing; a live one builds, whatever the other carriers returned."""
        client, _ = await self._generate_with_refetches(refetched)

        assert bool(client.allocations) is builds


class _LaggingClient(FakeClient):
    """A client whose reads lag: another session circuit is visible to a run only once ITS session is built.

    The worst case of a stale read for the one-port-one-session check: both circuits exist, but each run reads before
    the other's session is there. Without a lock around check and write, two runs on one port both pass.
    """

    async def filters(self, kind: Any, **kwargs: Any) -> list[Any]:
        found = await super().filters(kind, **kwargs)
        if "capabilities__ids" not in kwargs:
            return found
        built = {allocation["identifier"].removeprefix("vc-p2p__") for allocation in self.allocations}
        return [circuit for circuit in found if circuit.id in built]


class TestConcurrentRunsOnOnePort:
    """Two circuits created together on one port: the port locks let exactly one build."""

    @staticmethod
    def _client() -> _LaggingClient:
        """Both circuits are known to the client (registered by kind), neither is visible before it is built."""
        client = _LaggingClient()
        client.circuit_ids.add("vc-2")
        client.session_circuits["TopologyHandoffCircuit"] = [(VC_ID, "VC-A"), ("vc-2", "VC-B")]
        return client

    @pytest.mark.parametrize("reverse_ends", [False, True], ids=["same-order", "opposite-order"])
    @pytest.mark.asyncio
    async def test_exactly_one_of_two_concurrent_runs_builds_and_the_other_is_refused(self, reverse_ends: bool) -> None:
        """Both runs share both ports (listed in opposite order for the second one: unsorted locks would deadlock).
        One session is built; the other run is refused for the shared port and wrote nothing."""
        client = self._client()
        first = handoff_payload(name="VC-A")
        ends = [handoff_edge_port(), handoff_party_port()]
        second = handoff_payload(name="VC-B", circuit_id="vc-2", ends=ends[::-1] if reverse_ends else ends)

        results = await asyncio.wait_for(
            asyncio.gather(
                session_generator(client).generate(first),
                session_generator(client).generate(second),
                return_exceptions=True,
            ),
            timeout=5,
        )

        refused = [result for result in results if isinstance(result, GeneratorError)]
        assert len(refused) == 1 and "a port carries one interconnect or handoff session" in str(refused[0])
        assert results.count(None) == 1
        built = "VC-A" if results[0] is None else "VC-B"
        assert {allocation["identifier"] for allocation in client.allocations} == {
            f"vc-p2p__{VC_ID if built == 'VC-A' else 'vc-2'}"
        }
        assert {data["name"] for data, _ in client.created_of("ManagedBGPPeering")} == {f"{built}-bgp"}
        assert len(client.prefixes) == 1 and len(client.addresses) == 2

    @pytest.mark.asyncio
    async def test_the_port_locks_are_sorted_and_taken_before_the_circuit_lock(self) -> None:
        """One lock per end port in sorted order, then the circuit's own, around check and writes."""
        gen = await _run(FakeClient(), handoff_payload())

        # (the global address family helper takes its own leaf lock after these)
        assert gen.lock_keys[:3] == ["vc-port-if-aws", "vc-port-if-hs", f"vc-session-{VC_ID}"]

    @pytest.mark.asyncio
    async def test_a_refused_run_releases_its_port_locks(self) -> None:
        """A run refused for a shared port leaves no port locked for the next one."""
        client = FakeClient()
        client.session_circuits["TopologyHandoffCircuit"] = [("vc-other", "VC-OTHER")]

        with pytest.raises(GeneratorError, match="a port carries one"):
            await _run(client, handoff_payload())

        assert not any(lock.locked() for lock in client.locks.values())


class TestOnePortOneSession:
    @pytest.mark.parametrize(
        ("payload_for", "other_kind"),
        [
            (handoff_payload, "TopologyHandoffCircuit"),
            (handoff_payload, "TopologyInterconnectCircuit"),
            (interconnect_payload, "TopologyInterconnectCircuit"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_port_that_ends_another_session_circuit_is_refused(self, payload_for: Any, other_kind: str) -> None:
        """Any other handoff or interconnect circuit on one of the two ports refuses the run before it writes."""
        client = FakeClient()
        client.session_circuits[other_kind] = [("vc-2", "VC-OTHER")]

        await _assert_refused(
            payload_for(), r"are also ends of VC-OTHER; a port carries one interconnect or handoff session", client
        )

    @pytest.mark.parametrize("payload_for", [handoff_payload, interconnect_payload])
    @pytest.mark.parametrize("status", ["active", "provisioning", "maintenance", "down"])
    @pytest.mark.asyncio
    async def test_a_live_holder_of_the_port_blocks_before_any_write(self, payload_for: Any, status: str) -> None:
        """Any status but decommissioned keeps its ports: the new circuit is refused with nothing written."""
        client = FakeClient()
        client.session_circuits["TopologyHandoffCircuit"] = [("vc-2", "VC-LIVE", status)]

        await _assert_refused(payload_for(), r"are also ends of VC-LIVE; a port carries one", client)

    @pytest.mark.parametrize("payload_for", [handoff_payload, interconnect_payload])
    @pytest.mark.parametrize("other_kind", ["TopologyHandoffCircuit", "TopologyInterconnectCircuit"])
    @pytest.mark.asyncio
    async def test_a_decommissioned_holder_of_the_port_does_not_block(self, payload_for: Any, other_kind: str) -> None:
        """A retired circuit that kept its ports must not block a live one; only the live holder is named."""
        client = FakeClient()
        client.session_circuits[other_kind] = [("vc-old", "VC-RETIRED", "decommissioned")]

        await _run(client, payload_for())

        assert len(client.created_of("ManagedBGPPeering")) >= 1

    @pytest.mark.asyncio
    async def test_a_decommissioned_holder_is_not_named_beside_a_live_one(self) -> None:
        """Only the live holder is named in the refusal; the retired circuit that kept its ports is left out."""
        client = FakeClient()
        client.session_circuits["TopologyHandoffCircuit"] = [
            ("vc-old", "VC-RETIRED", "decommissioned"),
            ("vc-2", "VC-LIVE", "active"),
        ]

        with pytest.raises(GeneratorError, match=r"are also ends of VC-LIVE; a port carries one") as raised:
            await _run(client, handoff_payload())

        assert "VC-RETIRED" not in str(raised.value)
        assert client.writes == []

    @pytest.mark.parametrize("payload_for", [handoff_payload, interconnect_payload])
    @pytest.mark.asyncio
    async def test_a_tunnel_circuit_on_the_same_port_does_not_block(self, payload_for: Any) -> None:
        """A tunnel port is shared by design: the tunnel kind is never queried, whatever its status."""
        client = FakeClient()
        client.session_circuits["TopologyTunnelCircuit"] = [("vc-t", "TUN-1", "active")]

        await _run(client, payload_for())

        assert "TopologyTunnelCircuit" not in {kind for kind, _ in client.filter_calls}
        assert len(client.created_of("ManagedBGPPeering")) >= 1

    @pytest.mark.asyncio
    async def test_the_circuit_itself_does_not_count(self) -> None:
        """The circuit being run is also found on its own ports and must not block itself."""
        client = FakeClient()
        client.session_circuits["TopologyHandoffCircuit"] = [(VC_ID, HANDOFF)]

        await _run(client, handoff_payload())

        assert _peering(client, "bgp")["name"] == f"{HANDOFF}-bgp"

    @pytest.mark.asyncio
    async def test_both_session_kinds_are_asked_for_the_two_ports_only(self) -> None:
        """The tunnel circuit (not a session kind, and it shares ports) is never part of the query."""
        client = FakeClient()

        await _run(client, handoff_payload())

        asked = [(kind, kwargs) for kind, kwargs in client.filter_calls if "capabilities__ids" in kwargs]
        assert asked == [
            ("TopologyInterconnectCircuit", {"capabilities__ids": ["if-hs", "if-aws"]}),
            ("TopologyHandoffCircuit", {"capabilities__ids": ["if-hs", "if-aws"]}),
        ]

    def test_the_planners_cover_exactly_the_session_kinds(self) -> None:
        """circuit.py's fan-out and the port check use SESSION_CIRCUIT_KINDS; the generator plans these kinds."""
        assert set(VirtualCircuitSessionGenerator._PLANNERS) == set(SESSION_CIRCUIT_KINDS)


class TestSessionEnd:
    def test_a_loopback_without_an_address_is_not_a_candidate(self) -> None:
        """A VTEP loopback with no address cannot source a session and is dropped from the candidates."""
        port = clean_data(
            dc_border_port(
                loopbacks=[loopback("bl", "lo1", "loopback-vtep", None), loopback("bl", "lo2", "loopback", "fd::1/128")]
            )
        )

        end = SessionEnd.parse(port)

        assert end is not None
        assert [lb.name for lb in end.loopbacks] == ["lo2"]

    def test_a_physical_interface_in_the_loopback_list_is_not_a_candidate(self) -> None:
        """Only a DcimVirtualInterface carries the address the query selects."""
        port = clean_data(
            dc_border_port(loopbacks=[loopback("bl", "Ethernet9", "loopback", None, kind="DcimPhysicalInterface")])
        )

        end = SessionEnd.parse(port)

        assert end is not None and end.loopbacks == ()

    @pytest.mark.parametrize(
        ("roles", "overlay", "session"),
        [
            pytest.param(("underlay", "overlay"), "bl-overlay", "bl-overlay", id="one-overlay"),
            pytest.param(("overlay", "overlay"), None, None, id="two-overlays"),
            pytest.param(("regular",), None, "bl-regular", id="only-process"),
            pytest.param(("underlay", "regular"), None, None, id="several-none-overlay"),
            pytest.param((), None, None, id="no-process"),
        ],
    )
    def test_the_overlay_process_is_the_single_one_else_the_only_process_runs_the_session(
        self, roles: tuple[str, ...], overlay: str | None, session: str | None
    ) -> None:
        """overlay_process() counts the overlay-role ManagedBGP processes (as the check does); session_process() falls
        back to the device's only ManagedBGP."""
        port = clean_data(dc_border_port(roles=roles))
        for process in port["device"]["capabilities"]:
            process["id"] = f"bl-{process.get('process_role', 'x')}" if process["typename"] == "ManagedBGP" else "x"

        end = SessionEnd.parse(port)

        assert end is not None
        assert end.overlay_process() == overlay
        assert end.session_process() == session

    def test_an_empty_slot_is_not_an_end(self) -> None:
        """An empty end slot of the response parses to no end rather than raising."""
        assert SessionEnd.parse({}) is None
