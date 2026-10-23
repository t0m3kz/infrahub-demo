"""Unit tests for the physical circuit generator (generators/topology/circuit.py).

PhysicalCircuitGenerator derives a circuit's facility locations from its end devices' deployments and adds the
missing ones; a placement problem must fail the run before any write. It builds no session, but re-runs
add_virtual_circuit for every interconnect and handoff circuit on its ports. Fakes:
tests/unit/simulators/circuit_fakes.py.
"""

from __future__ import annotations

from typing import Any

import pytest

from generators.logger import GeneratorError
from generators.topology.circuit import (
    SESSION_CIRCUIT_KINDS,
    VIRTUAL_CIRCUIT_GENERATOR,
    CircuitEndpoint,
    CircuitLocation,
    circuit_endpoints,
    plan_locations,
)
from tests.unit.simulators.circuit_fakes import (
    CAGE,
    CIRCUIT_ID,
    ISP,
    METRO,
    REGION,
    SIBLING_CAGE,
    VIRTUAL_COLOCATION,
    FakeClient,
    cage_edge,
    dc_border_leaf,
    dc_place,
    interface,
    physical_generator,
    physical_payload,
    place,
)
from utils.data_cleaning import clean_data


class TestEndpoints:
    def test_both_ends_come_from_capabilities_deduplicated(self) -> None:
        """The circuit's ends are its capabilities, one list; a port listed twice counts once."""
        circuit = clean_data(physical_payload(ends=[dc_border_leaf(), cage_edge(), cage_edge()]))
        endpoints = circuit_endpoints(circuit["TopologyPhysicalCircuit"][0])

        assert [e.label for e in endpoints] == ["bl-dc101101/Ethernet1/35", "eg-fr201/Ethernet1/10"]

    def test_an_end_carries_its_devices_deployment(self) -> None:
        """The deployment places the end; a device without one has none."""
        bl = CircuitEndpoint.parse(clean_data(dc_border_leaf()))
        bare = CircuitEndpoint.parse(clean_data(interface("if-x", "eth1", "dev-x", no_deployment=True)))

        assert bl is not None and bare is not None
        assert bl.deployment == CircuitLocation(id="dep-DC10", kind="TopologyDataCenter", label="DC10")
        assert bare.deployment is None


class TestPhysicalCircuitInput:
    @pytest.mark.asyncio
    async def test_empty_response_fails(self) -> None:
        with pytest.raises(GeneratorError, match="No TopologyPhysicalCircuit data"):
            await physical_generator(FakeClient()).generate({"TopologyPhysicalCircuit": {"edges": []}})

    @pytest.mark.asyncio
    async def test_a_circuit_without_a_name_fails(self) -> None:
        payload = physical_payload()
        node = payload["TopologyPhysicalCircuit"]["edges"][0]["node"]
        node["name"], node["circuit_id"] = {"value": None}, {"value": None}

        with pytest.raises(GeneratorError, match="missing circuit_id/name"):
            await physical_generator(FakeClient()).generate(payload)


def _aws_router(**kwargs: Any) -> dict[str, Any]:
    """The provider router of a Direct Connect, deployed in its cloud region."""
    return interface("if-aws", "Ethernet1", "EXT-AWS-DX-FR2", deployment=REGION, **kwargs)


def _virtual_edge(**kwargs: Any) -> dict[str, Any]:
    """The virtual edge of a provider-hosted presence, deployed in its TopologyVirtualColocation."""
    return interface("if-ve", "Gi1", "eg-am-ne01", deployment=VIRTUAL_COLOCATION, **kwargs)


def _isp_pe(**kwargs: Any) -> dict[str, Any]:
    """The ISP's access PE, deployed in its TopologyExternalProviders."""
    return interface("if-pe", "xe-0/0/1", "pe-orange-fr", deployment=ISP, **kwargs)


def _added_locations(client: FakeClient) -> list[tuple[str, dict[str, Any]]]:
    return client.related()


class TestPhysicalCircuitPlacement:
    """Each end deployed in a DC, ISP or cloud region places itself; the rest is authored."""

    @pytest.mark.parametrize(
        ("ends", "authored", "derived"),
        [
            pytest.param([dc_border_leaf(), cage_edge()], [CAGE], ["dep-DC10"], id="cage-authored-dc-derived"),
            pytest.param([cage_edge(), _aws_router()], [CAGE], ["loc-eu-central-1"], id="cloud-region-derived"),
            pytest.param([cage_edge(), _isp_pe()], [CAGE], ["loc-ORANGE-FR"], id="isp-derived"),
            pytest.param([dc_border_leaf(), _isp_pe()], [], ["dep-DC10", "loc-ORANGE-FR"], id="both-ends-derived"),
            # The on-ramp edge deploys to its zone: the cage end is derived too, nothing has to be authored.
            pytest.param([dc_border_leaf(), cage_edge()], [], ["dep-DC10", "loc-FR2"], id="zone-and-dc-derived"),
            pytest.param(
                [dc_border_leaf(), _virtual_edge()], [], ["dep-DC10", "loc-AM-NE"], id="virtual-colocation-derived"
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_derived_locations_are_added_in_one_relationship_add(
        self, ends: list[dict[str, Any]], authored: list[dict[str, Any]], derived: list[str]
    ) -> None:
        """Only the derived locations the circuit lacks are sent; the authored ones are never re-sent."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(ends=ends, locations=authored))

        assert _added_locations(client) == [(CIRCUIT_ID, {"relation_to_update": "locations", "related_nodes": derived})]

    @pytest.mark.asyncio
    async def test_location_write_is_untracked(self) -> None:
        """The circuit is the run's target: no save, nothing joins the tracking group."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(locations=[CAGE]))

        assert [action for action, _, _ in client.writes] == ["relate"]
        assert client.created == []

    @pytest.mark.asyncio
    async def test_the_only_write_is_the_locations_relationship_so_the_status_trigger_cannot_loop(self) -> None:
        """data/events/99_actions.yml fires add_circuit on a physical circuit's status and capabilities; the run
        writes neither (only a RelationshipAdd on `locations`), so it cannot re-trigger itself."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(locations=[CAGE]))

        assert {kwargs["relation_to_update"] for _, kwargs in client.related()} == {"locations"}
        assert [action for action, _, _ in client.writes if action == "save"] == []

    @pytest.mark.asyncio
    async def test_already_complete_rerun_adds_nothing(self) -> None:
        """Authored cage plus the DC an earlier run derived: the circuit is not even fetched."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(locations=[CAGE, dc_place()]))

        assert _added_locations(client) == []
        assert not any(name == "TopologyPhysicalCircuit" for name, _ in client.get_calls)

    @pytest.mark.asyncio
    async def test_a_device_without_deployment_derives_nothing(self) -> None:
        """Both ends authored: nothing to derive, nothing written."""
        client = FakeClient()
        ends = [cage_edge(), interface("if-x", "eth1", "dev-x", no_deployment=True)]

        await physical_generator(client).generate(physical_payload(ends=ends, locations=[CAGE, ISP]))

        assert client.writes == []


class TestPhysicalCircuitPlacementRefusals:
    """A placement problem fails the run before any write."""

    @pytest.mark.parametrize(
        ("ends", "authored", "message"),
        [
            pytest.param(
                [dc_border_leaf(), cage_edge()],
                [CAGE, dc_place("DC11")],
                r"location DC11 \(TopologyDataCenter\) contradicts the endpoint devices' deployment: "
                r"bl-dc101101 is in DC10",
                id="authored-dc-contradicts-derived-dc",
            ),
            pytest.param(
                [cage_edge(), _aws_router()],
                [CAGE, place("TopologyCloudRegion", "eu-west-1")],
                r"location eu-west-1 .* EXT-AWS-DX-FR2 is in eu-central-1",
                id="authored-region-contradicts-derived-region",
            ),
            pytest.param(
                [dc_border_leaf(), cage_edge()],
                [SIBLING_CAGE, dc_place()],
                r"location FR6 \(TopologyColocationZone\) contradicts the endpoint devices' deployment: "
                r"eg-fr201 is in FR2",
                id="authored-zone-contradicts-derived-zone",
            ),
            pytest.param(
                [dc_border_leaf(), interface("if-x", "eth1", "dev-x", no_deployment=True)],
                [],
                r"found 1: DC10$",
                id="second-end-not-placed",
            ),
            pytest.param(
                [dc_border_leaf(), cage_edge()],
                [CAGE, ISP],
                r"found 3: FR2, ORANGE-FR, DC10$",
                id="more-than-two",
            ),
            pytest.param(
                [cage_edge(), interface("if-x", "eth1", "dev-x", no_deployment=True)],
                [CAGE],
                r"found 1: FR2$",
                id="device-without-deployment",
            ),
            pytest.param(
                [cage_edge(), interface("if-y", "eth1", "dev-y", deployment=METRO)],
                [CAGE],
                r"found 1: FR2$",
                id="metro-deployment-not-derived",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusal_writes_nothing(
        self, ends: list[dict[str, Any]], authored: list[dict[str, Any]], message: str
    ) -> None:
        client = FakeClient()
        gen = physical_generator(client)

        with pytest.raises(GeneratorError, match=message):
            await gen.generate(physical_payload(ends=ends, locations=authored))

        assert client.writes == []
        assert client.get_calls == []

    @pytest.mark.asyncio
    async def test_error_names_the_circuit_and_its_endpoints(self) -> None:
        with pytest.raises(GeneratorError) as excinfo:
            await physical_generator(FakeClient()).generate(
                physical_payload(
                    ends=[dc_border_leaf(), cage_edge(no_deployment=True)],
                    locations=[],
                )
            )

        assert str(excinfo.value).startswith(
            "DF-DC10-EQXFR2 (bl-dc101101/Ethernet1/35 | eg-fr201/Ethernet1/10): needs exactly 2 distinct locations"
        )


class TestFanOutToSessionCircuits:
    """add_circuit re-runs add_virtual_circuit, without waiting, for the session circuits riding its ports."""

    @pytest.mark.asyncio
    async def test_session_circuits_on_the_ports_are_rerun_without_waiting(self) -> None:
        client = FakeClient()
        client.session_circuits["TopologyInterconnectCircuit"] = [("vc-b", "VC-B")]
        client.session_circuits["TopologyHandoffCircuit"] = [("vc-a", "VC-A")]
        gen = physical_generator(client)

        await gen.generate(physical_payload())

        gen.run_generator.assert_awaited_once_with(VIRTUAL_CIRCUIT_GENERATOR, ["vc-a", "vc-b"], wait=False)
        assert VIRTUAL_CIRCUIT_GENERATOR == "add_virtual_circuit"

    @pytest.mark.asyncio
    async def test_a_circuit_found_by_both_kinds_is_rerun_once(self) -> None:
        client = FakeClient()
        client.session_circuits["TopologyInterconnectCircuit"] = [("vc-a", "VC-A")]
        client.session_circuits["TopologyHandoffCircuit"] = [("vc-a", "VC-A")]
        gen = physical_generator(client)

        await gen.generate(physical_payload())

        gen.run_generator.assert_awaited_once_with(VIRTUAL_CIRCUIT_GENERATOR, ["vc-a"], wait=False)

    @pytest.mark.asyncio
    async def test_only_the_two_session_kinds_are_queried_never_the_tunnel_circuit(self) -> None:
        """The tunnel circuit is not in the generator's target group: running it there would raise."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        asked = [kind for kind, kwargs in client.filter_calls if "capabilities__ids" in kwargs]
        assert asked == [kind.__name__ for kind in SESSION_CIRCUIT_KINDS]
        assert {"TopologyTunnelCircuit", "TopologyVirtualCircuit"}.isdisjoint(kind for kind, _ in client.filter_calls)

    @pytest.mark.asyncio
    async def test_sub_interfaces_of_the_ports_count_as_ends(self) -> None:
        """A handoff ends on the VLAN sub-interface of the carrier's port: found through its parent_interface."""
        client = FakeClient()
        client.sub_interfaces = ["sub-1900", "sub-1901"]

        await physical_generator(client).generate(physical_payload())

        assert ("DcimVirtualInterface", {"parent_interface__ids": ["if-bl", "if-eg"]}) in client.filter_calls
        session_queries = [kwargs for _, kwargs in client.filter_calls if "capabilities__ids" in kwargs]
        assert session_queries == [{"capabilities__ids": ["if-bl", "if-eg", "sub-1900", "sub-1901"]}] * 2

    @pytest.mark.asyncio
    async def test_no_session_circuit_means_no_rerun(self) -> None:
        gen = physical_generator(FakeClient())

        await gen.generate(physical_payload())

        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_circuit_without_ports_queries_nothing(self) -> None:
        """An empty id list may mean 'no filter' to the server: it is never sent."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(ends=[], locations=[CAGE, ISP]))

        assert client.filter_calls == []
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_refused_placement_fans_nothing_out(self) -> None:
        client = FakeClient()
        gen = physical_generator(client)

        with pytest.raises(GeneratorError):
            await gen.generate(physical_payload(locations=[CAGE, ISP]))

        assert client.filter_calls == []
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_fan_out_follows_the_location_write(self) -> None:
        """The session run waits for this one, so it sees the derived locations: written first, fired last."""
        client = FakeClient()
        client.session_circuits["TopologyHandoffCircuit"] = [("vc-a", "VC-A")]
        gen = physical_generator(client)

        await gen.generate(physical_payload(locations=[CAGE]))

        assert [action for action, _, _ in client.writes] == ["relate"]
        gen.run_generator.assert_awaited_once()


def _endpoint(interface_id: str, device: str, deployment: dict[str, Any] | None) -> CircuitEndpoint:
    """A parsed circuit end on ``device`` deployed to ``deployment`` (a raw place node, or None)."""
    parsed = CircuitEndpoint.parse(
        clean_data(
            interface(interface_id, "Ethernet1/1", device, deployment=deployment, no_deployment=deployment is None)
        )
    )
    assert parsed is not None
    return parsed


class TestPlanLocations:
    """plan_locations: which locations an end device's deployment derives, and what refuses the plan."""

    @pytest.mark.parametrize(
        ("deployment", "derived_id"),
        [
            pytest.param(CAGE, "loc-FR2", id="zone"),
            pytest.param(VIRTUAL_COLOCATION, "loc-AM-NE", id="virtual-colocation"),
            pytest.param(dc_place(), "dep-DC10", id="dc"),
            pytest.param(REGION, "loc-eu-central-1", id="cloud-region"),
            pytest.param(ISP, "loc-ORANGE-FR", id="isp"),
        ],
    )
    def test_a_facility_deployment_derives_its_end(self, deployment: dict[str, Any], derived_id: str) -> None:
        """An end deployed to a derivable facility places that end; the other end completes the pair."""
        ends = [_endpoint("if-a", "dev-a", deployment), _endpoint("if-b", "dev-b", dc_place("DC11"))]

        derived, problem = plan_locations([], ends)

        assert problem is None
        assert {location.id for location in derived} == {derived_id, "dep-DC11"}

    def test_an_authored_zone_that_is_the_edges_zone_is_not_derived_again(self) -> None:
        ends = [_endpoint("if-a", "eg-fr201", CAGE), _endpoint("if-b", "bl-dc101101", dc_place())]
        authored = [CircuitLocation.parse(clean_data(CAGE))]

        derived, problem = plan_locations([loc for loc in authored if loc], ends)

        assert problem is None
        assert [location.id for location in derived] == ["dep-DC10"]

    def test_a_metro_deployment_derives_nothing(self) -> None:
        """The metro is no facility: a device still deployed to it places no end."""
        ends = [_endpoint("if-a", "eg-fr01", METRO), _endpoint("if-b", "bl-dc101101", dc_place())]

        derived, problem = plan_locations([], ends)

        assert derived == []
        assert problem is not None
        assert "found 1: DC10" in problem

    def test_an_authored_zone_other_than_the_edges_zone_contradicts(self) -> None:
        """FR6 authored, the edge sits in FR2: same kind, different place."""
        ends = [_endpoint("if-a", "eg-fr201", CAGE), _endpoint("if-b", "bl-dc101101", dc_place())]
        authored = [CircuitLocation.parse(clean_data(SIBLING_CAGE))]

        derived, problem = plan_locations([loc for loc in authored if loc], ends)

        assert derived == []
        assert problem is not None
        assert "location FR6 (TopologyColocationZone) contradicts the endpoint devices' deployment" in problem
        assert "eg-fr201 is in FR2" in problem

    def test_a_zone_and_a_virtual_colocation_do_not_contradict_each_other(self) -> None:
        """Different kinds: an authored zone does not contradict a virtual colocation derived from the other end."""
        ends = [_endpoint("if-a", "eg-fr201", CAGE), _endpoint("if-b", "eg-am-ne01", VIRTUAL_COLOCATION)]

        derived, problem = plan_locations([], ends)

        assert problem is None
        assert {location.id for location in derived} == {"loc-FR2", "loc-AM-NE"}

    def test_the_problem_text_names_the_colocation_source(self) -> None:
        """The refusal lists what a deployment can derive, colocation included."""
        _, problem = plan_locations([], [_endpoint("if-a", "dev-x", None)])

        assert problem is not None
        assert "a DC, ISP, cloud region or colocation derived from an endpoint device's deployment" in problem
