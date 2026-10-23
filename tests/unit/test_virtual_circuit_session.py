"""Unit tests for the session generator (generators/topology/virtual_circuit.py, add_virtual_circuit).

An interconnect circuit builds a DCI (underlay eBGP on a /31 plus a loopback-to-loopback EVPN session), a handoff
circuit one eBGP session over a /31 of its namespace's pool, a tunnel circuit nothing. Every precondition must fail
before the first write. Fakes: tests/unit/simulators/circuit_fakes.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from generators.logger import GeneratorError
from generators.topology.circuit import PHYSICAL_CIRCUIT_GENERATOR, SESSION_CIRCUIT_KINDS
from generators.topology.virtual_circuit import SessionEnd, handoff_pool_name
from tests.unit.simulators.circuit_fakes import (
    DC_KEY_ID,
    HANDOFF_POOL_NETWORK,
    V4_POOL_NETWORK,
    VC_ID,
    FakeClient,
    colo_edge_port,
    dc_border_port,
    deployment,
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
from utils.data_cleaning import clean_data

HANDOFF = "VC-C001-EQXFR-AWS"
DCI = "VC-DCI-DC10-FR2"
DCI_KEY = f"RoutingPassword:{DCI}-dci-key"
UNTRACKED = {"update_group_context": False}


async def _run(client: FakeClient, payload: dict[str, Any]) -> Any:
    """Run add_virtual_circuit once over the fake client; returns the generator."""
    gen = session_generator(client)
    await gen.generate(payload)
    return gen


async def _assert_refused(payload: dict[str, Any], message: str, client: FakeClient | None = None) -> None:
    """The run fails with ``message`` and has written, allocated and created nothing."""
    client = client or FakeClient()
    with pytest.raises(GeneratorError, match=message):
        await _run(client, payload)
    assert client.writes == []
    assert client.allocations == []
    assert client.created == []


def _peering(client: FakeClient, suffix: str) -> dict[str, Any]:
    """The data the run sent for the peering named <vc>-<suffix>."""
    (data,) = [data for data, _ in client.created_of("ManagedBGPPeering") if data["name"].endswith(f"-{suffix}")]
    return data


def _interface_saves(client: FakeClient) -> list[str]:
    return [label for action, label, _ in client.writes if action == "save" and label.startswith("if-")]


class TestNothingToBuild:
    """A run that has no session to build writes nothing, and says so without failing."""

    @pytest.mark.asyncio
    async def test_a_tunnel_circuit_has_no_session(self) -> None:
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
        client = FakeClient()
        ends = [dc_border_port(circuits=[]), colo_edge_port(circuits=[])]

        await _run(client, interconnect_payload(ends=ends))

        assert client.writes == [] and client.filter_calls == []

    @pytest.mark.asyncio
    async def test_a_wholly_decommissioned_carrier_builds_nothing(self) -> None:
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
        client = FakeClient()
        carriers = [underlay_circuit("XC-OLD", status="decommissioned"), underlay_circuit("XC-FR2-AWS")]
        ends = [handoff_edge_port(parent_circuits=carriers), handoff_party_port()]

        await _run(client, handoff_payload(ends=ends))

        assert _peering(client, "bgp")["name"] == f"{HANDOFF}-bgp"

    @pytest.mark.asyncio
    async def test_empty_response_is_an_error(self) -> None:
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
        client = FakeClient()
        client.session_circuits[other_kind] = [("vc-2", "VC-OTHER")]

        await _assert_refused(
            payload_for(), r"are also ends of VC-OTHER; a port carries one interconnect or handoff session", client
        )

    @pytest.mark.asyncio
    async def test_the_circuit_itself_does_not_count(self) -> None:
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
        gen = session_generator(FakeClient())

        assert set(gen._planners()) == {kind.__name__ for kind in SESSION_CIRCUIT_KINDS}


class TestHandoffSession:
    @pytest.mark.asyncio
    async def test_allocates_the_namespace_pool_by_circuit_identifier(self) -> None:
        client = FakeClient()

        await _run(client, handoff_payload())

        (allocation,) = client.allocations
        assert allocation["resource_pool"].pool_name == "Handoff-PROD-IPv4"
        assert allocation["identifier"] == f"vc-p2p__{VC_ID}"
        assert allocation["prefix_length"] == 31
        assert allocation["member_type"] == "address"
        assert allocation["data"] == {"role": "technical", "is_pool": True, "description": f"Handoff {HANDOFF}"}

    @pytest.mark.asyncio
    async def test_the_prefix_joins_the_tracking_group_before_anything_else_is_written(self) -> None:
        client = FakeClient()

        await _run(client, handoff_payload())

        assert [action for action, _, _ in client.writes[:2]] == ["allocate", "track"]
        assert f"prefix-vc-p2p__{VC_ID}" in client.group_context.related_node_ids

    @pytest.mark.asyncio
    async def test_both_ends_are_addressed_ours_first_untracked_with_no_mtu_change(self) -> None:
        """Our end (the device with an overlay process) takes the lower address; interfaces are never claimed."""
        client = FakeClient()

        await _run(client, handoff_payload())

        base = HANDOFF_POOL_NETWORK.split("/")[0]
        assert client.interfaces["if-hs"].ip_address.id == f"IpamIPAddress:{base}/31"
        assert client.interfaces["if-aws"].ip_address.id == "IpamIPAddress:10.255.128.1/31"
        assert client.interfaces["if-hs"].description.value == f"Handoff {HANDOFF} to EXT-AWS-DX-FR2 Ethernet1.1900"
        assert client.interfaces["if-aws"].description.value == f"Handoff {HANDOFF} to eg-fr201 Ethernet1/10.1900"
        assert client.interfaces["if-hs"].status.value == "active"
        assert client.interfaces["if-hs"].mtu.value == 9000
        saves = client.saves()
        assert saves["if-hs"] == UNTRACKED and saves["if-aws"] == UNTRACKED

    @pytest.mark.asyncio
    async def test_one_peering_with_these_exact_attributes(self) -> None:
        client = FakeClient()

        await _run(client, handoff_payload(password="pw-1"))

        (peering,) = [data for data, _ in client.created_of("ManagedBGPPeering")]
        assert peering == {
            "name": f"{HANDOFF}-bgp",
            "description": (
                f"Handoff eBGP eg-fr201/Ethernet1/10.1900 <-> EXT-AWS-DX-FR2/Ethernet1.1900 over {HANDOFF}"
            ),
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "regular",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": False,
            "maximum_routes": 100,
            "address_families": [{"id": "af-ipv4-unicast"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "EXT-AWS-DX-FR2-bgp-regular"}],
            "password": {"id": "pw-1"},
            "capabilities": [{"id": "if-hs"}, {"id": "if-aws"}],
        }

    @pytest.mark.asyncio
    async def test_no_password_and_no_maximum_routes_are_not_written(self) -> None:
        client = FakeClient()

        await _run(client, handoff_payload(maximum_routes=None))

        peering = _peering(client, "bgp")
        assert "password" not in peering and "maximum_routes" not in peering
        assert client.created_of("RoutingPassword") == []

    @pytest.mark.asyncio
    async def test_a_circuit_that_is_not_active_is_provisioning(self) -> None:
        client = FakeClient()

        await _run(client, handoff_payload(status="provisioning"))

        assert _peering(client, "bgp")["status"] == "provisioning"
        assert client.interfaces["if-hs"].status.value == "provisioning"

    @pytest.mark.asyncio
    async def test_the_session_runs_on_the_overlay_process_else_the_only_one(self) -> None:
        """Both ends with a single process and none overlay: ordered by device name."""
        client = FakeClient()
        ends = [handoff_edge_port(roles=("regular",)), handoff_party_port()]

        await _run(client, handoff_payload(ends=ends))

        assert _peering(client, "bgp")["bgp_processes"] == [
            {"id": "EXT-AWS-DX-FR2-bgp-regular"},
            {"id": "eg-fr201-bgp-regular"},
        ]

    @pytest.mark.asyncio
    async def test_the_writes_run_under_one_lock_per_circuit(self) -> None:
        gen = await _run(FakeClient(), handoff_payload())

        assert gen.lock_keys.count(f"vc-session-{VC_ID}") == 1

    @pytest.mark.asyncio
    async def test_claims_prefix_addresses_and_peering_only(self) -> None:
        """Interfaces are written untracked; the global address family is only referenced."""
        client = FakeClient()

        await _run(client, handoff_payload())

        assert client.claimed() == {
            f"prefix-vc-p2p__{VC_ID}",
            "IpamIPAddress:10.255.128.0/31",
            "IpamIPAddress:10.255.128.1/31",
            f"ManagedBGPPeering:{HANDOFF}-bgp",
        }

    @pytest.mark.asyncio
    async def test_a_second_run_allocates_the_same_prefix_and_writes_no_interface_again(self) -> None:
        client = FakeClient()
        await _run(client, handoff_payload())
        first_labels = [label for _, label, _ in client.writes]
        first_interface_saves = _interface_saves(client)

        await _run(client, handoff_payload())

        assert [a["identifier"] for a in client.allocations] == [f"vc-p2p__{VC_ID}"] * 2
        assert len(client.prefixes) == 1
        assert _interface_saves(client) == first_interface_saves
        assert {label for _, label, _ in client.writes} == set(first_labels)
        assert len(client.addresses) == 2

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(handoff_payload(namespace=None), r"needs its ip_namespace", id="no-namespace"),
            pytest.param(
                handoff_payload(namespace="NON-PROD"),
                r"pool 'Handoff-NON-PROD-IPv4' not found",
                id="namespace-without-pool",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_namespace_problems_are_refused_before_any_write(self, payload: dict[str, Any], message: str) -> None:
        await _assert_refused(payload, message)

    @pytest.mark.parametrize(
        ("party", "message"),
        [
            pytest.param(
                session_port("if-aws", "Ethernet1", "EXT-AWS-DX-FR2", where=deployment("TopologyCloudRegion", "x")),
                r"EXT-AWS-DX-FR2/Ethernet1 must be a DcimVirtualInterface named <parent>.<vlan>",
                id="physical-port",
            ),
            pytest.param(
                session_port("if-aws", "lo0", "EXT-AWS-DX-FR2", kind="DcimVirtualInterface"),
                r"EXT-AWS-DX-FR2/lo0 must be a DcimVirtualInterface named <parent>.<vlan>",
                id="no-vlan-in-the-name",
            ),
            pytest.param(
                handoff_party_port(roles=("underlay", "regular")),
                r"EXT-AWS-DX-FR2 needs one overlay ManagedBGP process, or only one ManagedBGP process; has: "
                r"EXT-AWS-DX-FR2-bgp-underlay, EXT-AWS-DX-FR2-bgp-regular",
                id="two-processes-no-overlay",
            ),
            pytest.param(handoff_party_port(roles=()), r"has: none", id="no-process"),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_bad_end_is_refused_before_any_write(self, party: dict[str, Any], message: str) -> None:
        await _assert_refused(handoff_payload(ends=[handoff_edge_port(), party]), message)

    @pytest.mark.asyncio
    async def test_an_interface_that_cannot_be_fetched_is_refused_before_the_allocation(self) -> None:
        client = FakeClient()

        async def _only_one(kind: Any, **kwargs: Any) -> list[Any]:
            return [client.interface_node("if-hs")] if "ids" in kwargs else []

        setattr(client, "filters", _only_one)

        with pytest.raises(GeneratorError, match=r"interface\(s\) EXT-AWS-DX-FR2/Ethernet1.1900 not found"):
            await _run(client, handoff_payload())

        assert client.allocations == []

    @pytest.mark.asyncio
    async def test_an_exhausted_pool_fails_before_any_other_write(self) -> None:
        client = FakeClient()

        async def _none(**kwargs: Any) -> None:
            return None

        setattr(client, "allocate_next_ip_prefix", _none)

        with pytest.raises(GeneratorError, match="returned no prefix"):
            await _run(client, handoff_payload())

        assert client.writes == []


def _two_dc_ends(protocol: str, address_for: Any) -> list[dict[str, Any]]:
    """Two DC border leafs of one underlay protocol, each with a VTEP loopback of the matching family."""
    return [
        dc_border_port(
            where=deployment("TopologyDataCenter", "DC10", underlay=protocol),
            loopbacks=[loopback("bl-dc101101", "loopback2", "loopback-vtep", address_for("1"))],
        ),
        session_port(
            "if-bl2",
            "Ethernet1/35",
            "bl-dc111101",
            where=deployment("TopologyDataCenter", "DC11", underlay=protocol),
            circuits=[underlay_circuit()],
            loopbacks=[loopback("bl-dc111101", "loopback2", "loopback-vtep", address_for("2"))],
        ),
    ]


class TestInterconnectSession:
    @pytest.mark.asyncio
    async def test_allocates_the_dci_pool_by_the_dc_ends_underlay_protocol(self) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload())

        (allocation,) = client.allocations
        assert allocation["resource_pool"].pool_name == "DCI-Technical-IPv6"
        assert allocation["prefix_length"] == 127
        assert allocation["identifier"] == f"vc-p2p__{VC_ID}"

    @pytest.mark.asyncio
    async def test_an_ipv4_fabric_between_two_dcs_uses_the_ipv4_pool_and_family(self) -> None:
        client = FakeClient()
        ends = _two_dc_ends("ipv4", lambda n: f"10.0.0.{n}/32")

        await _run(client, interconnect_payload(ends=ends))

        (allocation,) = client.allocations
        assert allocation["resource_pool"].pool_name == "DCI-Technical-IPv4" and allocation["prefix_length"] == 31
        assert _peering(client, "underlay")["address_families"] == [{"id": "af-ipv4-unicast"}]
        assert client.interfaces["if-bl"].ip_address.id == f"IpamIPAddress:{V4_POOL_NETWORK.split('/')[0]}/31"

    @pytest.mark.asyncio
    async def test_the_colocation_edge_takes_the_lower_address(self) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert client.interfaces["if-eg"].ip_address.id == "IpamIPAddress:fd00:2200::/127"
        assert client.interfaces["if-bl"].ip_address.id == "IpamIPAddress:fd00:2200::1/127"

    @pytest.mark.asyncio
    async def test_the_underlay_peering_has_these_exact_attributes(self) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert _peering(client, "underlay") == {
            "name": f"{DCI}-underlay",
            "description": f"DCI underlay eBGP eg-fr201/Ethernet1/10 <-> bl-dc101101/Ethernet1/35 over {DCI}",
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "dci-underlay",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": False,
            "maximum_routes": 16,
            "address_families": [{"id": "af-ipv6-unicast"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "bl-dc101101-bgp-overlay"}],
            "password": {"id": DCI_KEY},
            "capabilities": [{"id": "if-eg"}, {"id": "if-bl"}],
        }

    @pytest.mark.asyncio
    async def test_the_evpn_peering_has_these_exact_attributes_on_the_vtep_loopbacks(self) -> None:
        """The VTEP loopback beats the plain one (loopback1) and the multisite VIP (loopback3) is never used."""
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert _peering(client, "evpn") == {
            "name": f"{DCI}-evpn",
            "description": f"DCI EVPN multihop eg-fr201 <-> bl-dc101101 over {DCI}",
            "status": "active",
            "session_type": "EBGP_MULTIHOP",
            "peering_role": "dci",
            "ttl": 2,
            "bfd_enabled": False,
            "send_community": True,
            "send_extended_community": True,
            "address_families": [{"id": "af-l2vpn-evpn"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "bl-dc101101-bgp-overlay"}],
            "password": {"id": DCI_KEY},
            "capabilities": [{"id": "lo-eg-fr201-loopback1"}, {"id": "lo-bl-dc101101-loopback2"}],
        }
        assert "maximum_routes" not in _peering(client, "evpn")

    @pytest.mark.parametrize(
        ("loopbacks", "expected"),
        [
            pytest.param(
                [
                    loopback("bl-dc101101", "lo1", "loopback", "fd00:10::1/128"),
                    loopback("bl-dc101101", "lo2", "loopback-vtep", "fd00:10::2/128"),
                ],
                "lo-bl-dc101101-lo2",
                id="vtep-before-loopback",
            ),
            pytest.param(
                [loopback("bl-dc101101", "lo1", "loopback", "fd00:10::1/128")],
                "lo-bl-dc101101-lo1",
                id="loopback-when-no-vtep",
            ),
            pytest.param(
                [
                    loopback("bl-dc101101", "lo1", "loopback", "fd00:10::1/128"),
                    loopback("bl-dc101101", "lo2", "loopback-vtep", "10.0.0.2/32"),
                ],
                "lo-bl-dc101101-lo1",
                id="vtep-of-the-wrong-family-is-skipped",
            ),
            pytest.param(
                [
                    loopback("bl-dc101101", "lo3", "loopback-vtep", "fd00:10::3/128"),
                    loopback("bl-dc101101", "lo2", "loopback-vtep", "fd00:10::2/128"),
                ],
                "lo-bl-dc101101-lo2",
                id="first-by-name-among-equals",
            ),
            pytest.param(
                [
                    loopback("bl-dc101101", "lo1", "loopback-vtep", None),
                    loopback("bl-dc101101", "lo2", "loopback", "fd00:10::2/128"),
                ],
                "lo-bl-dc101101-lo2",
                id="vtep-without-an-address-is-skipped",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_loopback_selection(self, loopbacks: list[dict[str, Any]], expected: str) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload(ends=[dc_border_port(loopbacks=loopbacks), colo_edge_port()]))

        assert _peering(client, "evpn")["capabilities"] == [{"id": "lo-eg-fr201-loopback1"}, {"id": expected}]

    @pytest.mark.asyncio
    async def test_the_key_is_created_once_tracked_and_shared_by_both_peerings(self) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload())

        (key_data, _) = client.created_of("RoutingPassword")[0]
        assert key_data["name"] == f"{DCI}-dci-key" and key_data["password"]
        assert DCI_KEY in client.claimed()
        assert {_peering(client, s)["password"]["id"] for s in ("underlay", "evpn")} == {DCI_KEY}

    @pytest.mark.asyncio
    async def test_a_second_run_reuses_the_key_and_creates_no_second_one(self) -> None:
        client = FakeClient()
        await _run(client, interconnect_payload())
        first_password = client.created_of("RoutingPassword")[0][0]["password"]

        await _run(client, interconnect_payload())

        assert len(client.created_of("RoutingPassword")) == 1
        assert client.keys[f"{DCI}-dci-key"].id == DCI_KEY
        assert first_password == client.created_of("RoutingPassword")[0][0]["password"]
        assert len(client.prefixes) == 1 and len(client.addresses) == 2

    @pytest.mark.asyncio
    async def test_an_existing_key_is_reused_and_tracked(self) -> None:
        client = FakeClient(key=True)

        await _run(client, interconnect_payload())

        assert client.created_of("RoutingPassword") == []
        assert DC_KEY_ID in client.group_context.related_node_ids
        assert _peering(client, "underlay")["password"] == {"id": DC_KEY_ID}

    @pytest.mark.asyncio
    async def test_the_two_physical_ports_get_mtu_9216_untracked(self) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload())

        for port in ("if-eg", "if-bl"):
            assert client.interfaces[port].mtu.value == 9216
            assert client.saves()[port] == UNTRACKED
        assert client.interfaces["if-eg"].description.value == f"DCI {DCI} to bl-dc101101 Ethernet1/35"

    @pytest.mark.asyncio
    async def test_claims_prefix_addresses_peerings_and_key_only(self) -> None:
        client = FakeClient()

        await _run(client, interconnect_payload())

        assert client.claimed() == {
            f"prefix-vc-p2p__{VC_ID}",
            "IpamIPAddress:fd00:2200::/127",
            "IpamIPAddress:fd00:2200::1/127",
            f"ManagedBGPPeering:{DCI}-underlay",
            f"ManagedBGPPeering:{DCI}-evpn",
            DCI_KEY,
        }

    @pytest.mark.asyncio
    async def test_a_second_run_writes_no_interface_again(self) -> None:
        client = FakeClient()
        await _run(client, interconnect_payload())
        first_interface_saves = _interface_saves(client)

        await _run(client, interconnect_payload())

        assert _interface_saves(client) == first_interface_saves

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(
                interconnect_payload(
                    ends=[
                        dc_border_port(where=deployment("TopologyDataCenter", "DC10", underlay="dual_stack")),
                        colo_edge_port(),
                    ]
                ),
                r"DC10 is dual_stack, a DCI needs one address family",
                id="dual-stack",
            ),
            pytest.param(
                interconnect_payload(
                    ends=[
                        dc_border_port(where=deployment("TopologyDataCenter", "DC10", underlay="ipv4")),
                        colo_edge_port(),
                    ]
                ),
                r"eg-fr201 is a colocation edge, whose VTEP loopback is IPv6 only",
                id="ipv4-towards-a-colocation-edge",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(roles=("underlay",)), colo_edge_port()]),
                r"bl-dc101101 needs exactly one overlay ManagedBGP process",
                id="no-overlay-process",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(), colo_edge_port(roles=("overlay", "overlay"))]),
                r"eg-fr201 needs exactly one overlay ManagedBGP process",
                id="two-overlay-processes",
            ),
            pytest.param(
                interconnect_payload(
                    ends=[
                        colo_edge_port(),
                        session_port(
                            "if-eg2",
                            "Ethernet1/10",
                            "eg-fr202",
                            where=deployment("TopologyColocationZone", "FR3"),
                            circuits=[underlay_circuit()],
                        ),
                    ]
                ),
                r"a DCI needs a DC fabric device at one end",
                id="no-dc-end",
            ),
            pytest.param(
                interconnect_payload(
                    ends=_two_dc_ends("ipv4", lambda n: f"10.0.0.{n}/32")[:1]
                    + [
                        session_port(
                            "if-bl2",
                            "Ethernet1/35",
                            "bl-dc111101",
                            where=deployment("TopologyDataCenter", "DC11", underlay="ipv6"),
                            circuits=[underlay_circuit()],
                            loopbacks=[loopback("bl-dc111101", "loopback2", "loopback-vtep", "fd00:11::2/128")],
                        )
                    ]
                ),
                r"the DC ends disagree on underlay_protocol \(ipv4, ipv6\)",
                id="dc-ends-disagree",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(kind="DcimVirtualInterface"), colo_edge_port()]),
                r"is a DcimVirtualInterface, a DCI ends on physical ports",
                id="end-is-not-a-physical-port",
            ),
            pytest.param(
                interconnect_payload(
                    ends=[
                        dc_border_port(
                            loopbacks=[loopback("bl-dc101101", "loopback3", "multisite-vip", "fd00:10::99/128")]
                        ),
                        colo_edge_port(),
                    ]
                ),
                r"bl-dc101101 has no loopback-vtep or loopback with an IPv6 address.*multisite-vip is never used",
                id="only-the-multisite-vip",
            ),
            pytest.param(
                interconnect_payload(ends=[dc_border_port(), colo_edge_port(loopbacks=[])]),
                r"eg-fr201 has no loopback-vtep or loopback with an IPv6 address",
                id="no-loopback",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_a_refused_dci_has_written_nothing(self, payload: dict[str, Any], message: str) -> None:
        """Pool, key, prefix, addresses, interfaces and peerings: not one write before a refusal."""
        client = FakeClient()

        await _assert_refused(payload, message, client)

        assert client.created_of("RoutingPassword") == [] and client.interfaces == {}

    @pytest.mark.asyncio
    async def test_a_missing_pool_is_refused_before_any_write(self) -> None:
        await _assert_refused(interconnect_payload(), r"pool 'DCI-Technical-IPv6' not found", FakeClient(pools=False))


class TestSessionEnd:
    def test_a_loopback_without_an_address_is_not_a_candidate(self) -> None:
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

    def test_an_empty_slot_is_not_an_end(self) -> None:
        assert SessionEnd.parse({}) is None


class TestHandoffPoolData:
    """data/bootstrap/26_handoff_pools.yml: one /31 pool per environment namespace, named as the generator expects."""

    _ROOT = Path(__file__).parents[2]

    def _documents(self, name: str) -> list[dict[str, Any]]:
        return [doc["spec"] for doc in yaml.safe_load_all((self._ROOT / "data" / "bootstrap" / name).read_text())]

    def test_every_namespace_has_its_pool_in_that_namespace(self) -> None:
        namespaces = [row["name"] for spec in self._documents("22_namespaces.yml") for row in spec["data"]]
        pools = {row["name"]: row for spec in self._documents("26_handoff_pools.yml") for row in spec["data"]}

        assert sorted(pools) == sorted(handoff_pool_name(ns) for ns in namespaces)
        for namespace in namespaces:
            pool = pools[handoff_pool_name(namespace)]
            assert pool["ip_namespace"] == namespace
            assert pool["default_prefix_length"] == 31
            assert [resource["ip_namespace"] for resource in pool["resources"]["data"]] == [namespace]
            assert [resource["member_type"] for resource in pool["resources"]["data"]] == ["prefix"]

    def test_the_pool_prefixes_do_not_overlap_each_other_or_the_authored_handoffs(self) -> None:
        import ipaddress

        networks = [
            ipaddress.ip_network(resource["prefix"])
            for spec in self._documents("26_handoff_pools.yml")
            for row in spec["data"]
            for resource in row["resources"]["data"]
        ]
        authored = [ipaddress.ip_network("10.255.5.0/24")]

        assert len(networks) == 4
        for index, network in enumerate(networks):
            assert not any(network.overlaps(other) for other in [*networks[:index], *networks[index + 1 :], *authored])
