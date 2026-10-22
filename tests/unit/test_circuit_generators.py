"""Unit tests for the circuit generators (generators/topology/circuit.py).

PhysicalCircuitGenerator builds the DCI session a peering_role=dci circuit
asks for (P2P from the DCI pool, both interfaces addressed, one eBGP peering)
and fans out add_virtual_circuit; VirtualCircuitGenerator builds the overlay
eBGP peering a peering_role=overlay tunnel circuit asks for. Every refusal
must happen before the first write. Fakes: tests/unit/simulators/circuit_fakes.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from generators.logger import GeneratorError
from generators.topology.circuit import (
    CircuitEndpoint,
    CircuitLocation,
    circuit_endpoints,
    endpoint_problem,
    order_endpoints,
    plan_locations,
)
from tests.unit.simulators.circuit_fakes import (
    CAGE,
    CIRCUIT_ID,
    DC_KEY_ID,
    ISP,
    METRO,
    PREFIX_ID,
    REGION,
    SIBLING_CAGE,
    V4_POOL_ID,
    V6_POOL_ID,
    VIRTUAL_COLOCATION,
    FakeClient,
    cage_edge,
    dc_border_leaf,
    dc_place,
    interface,
    physical_generator,
    physical_payload,
    place,
    sdwan_edge,
    sdwan_gateway,
    underlay_circuit,
    virtual_generator,
    virtual_payload,
)
from utils.circuit_underlay import derived_underlay
from utils.data_cleaning import clean_data

TRACKED = {"allow_upsert": True}


def _peerings(client: FakeClient) -> list[dict[str, Any]]:
    return [data for data, _ in client.created_of("ManagedBGPPeering")]


def _info_messages(gen: Any) -> list[str]:
    """The text of every logger.info call the generator made."""
    return [str(c.args[0]) for c in gen.logger.info.call_args_list]


# ===========================================================================
# Endpoint parsing
# ===========================================================================


class TestEndpoints:
    def test_both_ends_come_from_capabilities_deduplicated(self) -> None:
        """The circuit's ends are its capabilities, one list; a port listed twice counts once."""
        circuit = clean_data(physical_payload(ends=[dc_border_leaf(), cage_edge(), cage_edge()]))
        endpoints = circuit_endpoints(circuit["TopologyPhysicalCircuit"][0])

        assert [e.label for e in endpoints] == ["bl-dc101101/Ethernet1/35", "eg-fr201/Ethernet1/10"]

    def test_dc_end_carries_its_fabric_and_overlay_process(self) -> None:
        """A device whose deployment is a TopologyDataCenter is a DC fabric end."""
        bl = CircuitEndpoint.parse(clean_data(dc_border_leaf()))
        eg = CircuitEndpoint.parse(clean_data(cage_edge()))

        assert bl is not None and eg is not None
        assert (bl.fabric, bl.underlay_protocol) == ("dc10", "ipv6")
        assert eg.fabric is None
        assert bl.bgp_process("overlay") == "bl-dc101101-bgp-overlay"

    def test_the_facility_end_comes_first(self) -> None:
        """The non-DC end takes the lower P2P address, the DC end the upper one."""
        bl = CircuitEndpoint.parse(clean_data(dc_border_leaf()))
        eg = CircuitEndpoint.parse(clean_data(cage_edge()))
        assert bl is not None and eg is not None

        assert order_endpoints([bl, eg]) == (eg, bl)

    @pytest.mark.parametrize(
        ("interfaces", "problem"),
        [
            ([dc_border_leaf()], "exactly 2 endpoint interfaces, found 1"),
            ([dc_border_leaf(), cage_edge(), sdwan_edge()], "exactly 2 endpoint interfaces, found 3"),
            (
                [dc_border_leaf(), interface("if-bl-2", "Ethernet1/36", "bl-dc101101", dc="DC10")],
                "endpoints on 2 different devices, both are on bl-dc101101",
            ),
        ],
    )
    def test_a_session_needs_two_interfaces_on_two_devices(self, interfaces: list, problem: str) -> None:
        """Anything but exactly two interfaces on two devices carries no session."""
        endpoints = [CircuitEndpoint.parse(clean_data(i)) for i in interfaces]

        assert endpoint_problem([e for e in endpoints if e is not None]) == problem


# ===========================================================================
# PhysicalCircuitGenerator
# ===========================================================================


class TestPhysicalCircuitNoSession:
    @pytest.mark.asyncio
    async def test_role_none_writes_nothing(self) -> None:
        """peering_role none (the default) builds no session."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none"))

        assert client.writes == []
        assert client.created == []

    @pytest.mark.asyncio
    async def test_unset_role_is_none(self) -> None:
        """A circuit loaded before the attribute existed reads as none."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(role=None))

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_role_none_with_one_endpoint_only_logs(self) -> None:
        """A one-ended circuit (an interface-side write the schema cap does not see) is only logged without a session."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none", ends=[dc_border_leaf()]))

        assert client.writes == []
        assert any("exactly 2 endpoint interfaces" in str(c) for c in gen.logger.info.call_args_list)

    @pytest.mark.parametrize("role", ["underlay", "transit"])
    @pytest.mark.asyncio
    async def test_unimplemented_roles_log_and_write_nothing(self, role: str) -> None:
        """underlay and transit are accepted values, not generated yet — and not a failure."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role=role))

        assert client.writes == []
        assert any("not implemented yet" in str(c) for c in gen.logger.info.call_args_list)

    @pytest.mark.asyncio
    async def test_decommissioned_dci_circuit_writes_nothing(self) -> None:
        """No session: the run tracks nothing, so cleanup removes what an earlier run built."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(status="decommissioned"))

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_empty_response_fails(self) -> None:
        with pytest.raises(GeneratorError, match="No TopologyPhysicalCircuit"):
            await physical_generator(FakeClient()).generate({"TopologyPhysicalCircuit": {"edges": []}})


class TestPhysicalCircuitDci:
    @pytest.mark.asyncio
    async def test_prefix_comes_from_the_ipv6_dci_pool(self) -> None:
        """One /127 per circuit, idempotent per circuit node id."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        assert len(client.allocations) == 1
        allocation = client.allocations[0]
        assert allocation["resource_pool"].id == V6_POOL_ID
        assert allocation["identifier"] == "dci-p2p__circ-df10"
        assert allocation["prefix_length"] == 127
        assert allocation["member_type"] == "address"

    @pytest.mark.asyncio
    async def test_ipv4_fabric_uses_the_ipv4_dci_pool_and_unicast_family(self) -> None:
        """The DCI follows the DC fabric's underlay_protocol."""
        client = FakeClient()
        payload = physical_payload(ends=[dc_border_leaf(underlay="ipv4"), cage_edge()])

        await physical_generator(client).generate(payload)

        assert client.allocations[0]["resource_pool"].id == V4_POOL_ID
        assert client.allocations[0]["prefix_length"] == 31
        assert _peerings(client)[0]["address_families"] == [{"id": "af-ipv4-unicast"}, {"id": "af-l2vpn-evpn"}]

    @pytest.mark.asyncio
    async def test_prefix_and_both_addresses_are_tracked(self) -> None:
        """The prefix is claimed by id (allocation is no save); the addresses are tracked upserts."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        assert client.group_context.related_node_ids == [PREFIX_ID]
        addresses = [data["address"] for data, _ in client.created_of("IpamIPAddress")]
        assert addresses == ["fd00:2200::/127", "fd00:2200::1/127"]
        saves = client.saves()
        assert saves["IpamIPAddress:fd00:2200::/127"] == TRACKED
        assert saves["IpamIPAddress:fd00:2200::1/127"] == TRACKED

    @pytest.mark.asyncio
    async def test_interfaces_are_addressed_untracked(self) -> None:
        """The cage end takes the lower address, the DC end the upper one;
        both template interfaces are saved with update_group_context=False."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        cage, leaf = client.interfaces["if-eg"], client.interfaces["if-bl"]
        assert cage.ip_address == "IpamIPAddress:fd00:2200::/127"
        assert leaf.ip_address == "IpamIPAddress:fd00:2200::1/127"
        assert cage.description.value == "DCI DF-DC10-EQXFR2 to bl-dc101101 Ethernet1/35"
        assert leaf.description.value == "DCI DF-DC10-EQXFR2 to eg-fr201 Ethernet1/10"
        assert cage.status.value == leaf.status.value == "active"
        assert client.saves()["if-eg"] == {"update_group_context": False}
        assert client.saves()["if-bl"] == {"update_group_context": False}

    @pytest.mark.parametrize(
        ("circuit_status", "interface_status"), [("maintenance", "maintenance"), ("down", "outage")]
    )
    @pytest.mark.asyncio
    async def test_circuit_status_reaches_the_interfaces(self, circuit_status: str, interface_status: str) -> None:
        """A circuit in maintenance or down shuts its interfaces (templates render non-active shut)."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(status=circuit_status))

        assert client.interfaces["if-bl"].status.value == interface_status
        assert _peerings(client)[0]["status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_unchanged_interfaces_are_not_resaved(self) -> None:
        """A rerun over already-addressed interfaces writes them not at all."""
        client = FakeClient()
        client.interface_node(
            "if-eg",
            address_id="IpamIPAddress:fd00:2200::/127",
            description="DCI DF-DC10-EQXFR2 to bl-dc101101 Ethernet1/35",
            status="active",
        )

        await physical_generator(client).generate(physical_payload())

        assert "if-eg" not in client.saves()
        assert "if-bl" in client.saves()

    @pytest.mark.asyncio
    async def test_one_tracked_dci_peering(self) -> None:
        """The session the hand-written 05_dci_peerings.yml used to declare."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        peerings = client.created_of("ManagedBGPPeering")
        assert len(peerings) == 1
        data, node = peerings[0]
        assert data == {
            "name": "DCI-DF-DC10-EQXFR2",
            "description": "EVPN Multi-Site DCI eg-fr201 <-> bl-dc101101 over DF-DC10-EQXFR2",
            "status": "active",
            "session_type": "EBGP",
            "peering_role": "dci",
            "ttl": 1,
            "bfd_enabled": True,
            "send_community": True,
            "send_extended_community": True,
            "address_families": [{"id": "af-ipv6-unicast"}, {"id": "af-l2vpn-evpn"}],
            "bgp_processes": [{"id": "eg-fr201-bgp-overlay"}, {"id": "bl-dc101101-bgp-overlay"}],
            "password": {"id": DC_KEY_ID},
            "capabilities": [{"id": "if-eg"}, {"id": "if-bl"}],
        }
        assert client.saves()[node.id] == TRACKED

    @pytest.mark.asyncio
    async def test_key_is_the_dc_fabric_overlay_key(self) -> None:
        """Looked up by the DC end's fabric name, never written."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload())

        assert ("RoutingPassword", {"name__value": "dc10-overlay-key"}) in client.get_calls
        assert not client.created_of("RoutingPassword")

    @pytest.mark.asyncio
    async def test_missing_address_families_are_created_untracked_under_a_lock(self) -> None:
        """Global nodes: found or created inside resource_lock, never claimed."""
        client = FakeClient(address_families=False)
        gen = physical_generator(client)

        await gen.generate(physical_payload())

        assert gen.lock_keys == ["bgp-af-ipv6-unicast", "bgp-af-l2vpn-evpn"]
        families = client.created_of("RoutingBGPAddressFamily")
        assert [(d["afi"], d["safi"]) for d, _ in families] == [("ipv6", "unicast"), ("l2vpn", "evpn")]
        assert families[1][0]["advertise_all_vni"] is True
        for _, node in families:
            assert client.saves()[node.id] == {"allow_upsert": True, "update_group_context": False}
            assert node.id not in client.group_context.related_node_ids


class TestPhysicalCircuitDciRefusals:
    """Every precondition fails before the first write (save, allocation, claim)."""

    @pytest.mark.parametrize(
        ("payload", "client_kwargs", "message"),
        [
            pytest.param(
                physical_payload(ends=[dc_border_leaf()]), {}, "exactly 2 endpoint interfaces, found 1", id="one-end"
            ),
            pytest.param(
                physical_payload(
                    ends=[dc_border_leaf(), interface("if-bl-2", "Ethernet1/36", "bl-dc101101", dc="DC10")]
                ),
                {},
                "2 different devices",
                id="same-device",
            ),
            pytest.param(
                physical_payload(ends=[sdwan_edge(), cage_edge()]),
                {},
                "needs a DC fabric device at one end",
                id="no-dc-end",
            ),
            pytest.param(
                physical_payload(ends=[dc_border_leaf(), cage_edge(roles=("underlay",))]),
                {},
                "eg-fr201 has no single overlay ManagedBGP process",
                id="no-overlay-process",
            ),
            pytest.param(
                physical_payload(ends=[dc_border_leaf(kind="DcimLAGInterface"), cage_edge()]),
                {},
                "takes no address",
                id="lag-endpoint",
            ),
            pytest.param(physical_payload(), {"key": False}, "'dc10-overlay-key' not found", id="no-key"),
            pytest.param(physical_payload(), {"pools": False}, "DCI-Technical-IPv6' not found", id="no-pool"),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusal_writes_nothing(self, payload: dict, client_kwargs: dict, message: str) -> None:
        client = FakeClient(**client_kwargs)

        with pytest.raises(GeneratorError, match=message):
            await physical_generator(client).generate(payload)

        assert client.writes == []
        assert client.created == []


class TestPhysicalCircuitFanOut:
    @pytest.mark.asyncio
    async def test_reruns_the_overlay_circuits_on_its_ports(self) -> None:
        """Fire-and-forget, so a status change reaches the virtual circuits that share one of its ports."""
        client = FakeClient()
        client.virtual_circuits = [MagicMock(id="vc-2"), MagicMock(id="vc-1")]
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none"))

        assert (
            "TopologyVirtualCircuit",
            {"capabilities__ids": ["if-bl", "if-eg"], "peering_role__value": "overlay"},
        ) in client.filter_calls
        gen.run_generator.assert_awaited_once_with("add_virtual_circuit", ["vc-1", "vc-2"], wait=False)

    @pytest.mark.asyncio
    async def test_a_port_listed_twice_is_filtered_once(self) -> None:
        """The filter takes the deduplicated endpoint interface ids, in query order."""
        client = FakeClient()
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none", ends=[cage_edge(), dc_border_leaf(), cage_edge()]))

        assert client.filter_calls == [
            ("TopologyVirtualCircuit", {"capabilities__ids": ["if-eg", "if-bl"], "peering_role__value": "overlay"})
        ]

    @pytest.mark.asyncio
    async def test_no_endpoints_no_query_and_no_dispatch(self) -> None:
        """Without ports there is nothing for a virtual circuit to share: no unfiltered query, no fan-out."""
        client = FakeClient()
        client.virtual_circuits = [MagicMock(id="vc-1")]
        gen = physical_generator(client)

        await gen._fan_out_virtual_circuits([], "DF-DC10-EQXFR2")

        assert client.filter_calls == []
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_overlay_circuits_no_fan_out(self) -> None:
        gen = physical_generator(FakeClient())

        await gen.generate(physical_payload())

        gen.run_generator.assert_not_awaited()


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

        await physical_generator(client).generate(physical_payload(role="none", ends=ends, locations=authored))

        assert _added_locations(client) == [(CIRCUIT_ID, {"relation_to_update": "locations", "related_nodes": derived})]

    @pytest.mark.asyncio
    async def test_location_write_is_untracked(self) -> None:
        """The circuit is the run's target: no save, nothing joins the tracking group."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(role="none", locations=[CAGE]))

        assert client.saves() == {}
        assert list(client.group_context.related_node_ids) == []

    @pytest.mark.parametrize("role", ["none", "dci"])
    @pytest.mark.asyncio
    async def test_already_complete_rerun_adds_nothing(self, role: str) -> None:
        """Authored cage plus the DC an earlier run derived: the circuit is not even fetched."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(role=role, locations=[CAGE, dc_place()]))

        assert _added_locations(client) == []
        assert not any(name == "TopologyPhysicalCircuit" for name, _ in client.get_calls)

    @pytest.mark.asyncio
    async def test_dci_session_and_derived_location_both_written(self) -> None:
        """Placement does not change the DCI session; its location add comes after the session writes."""
        client = FakeClient()

        await physical_generator(client).generate(physical_payload(locations=[CAGE]))

        assert len(_peerings(client)) == 1
        assert client.writes[-1] == (
            "relate",
            CIRCUIT_ID,
            {"relation_to_update": "locations", "related_nodes": ["dep-DC10"]},
        )

    @pytest.mark.asyncio
    async def test_fan_out_still_runs_after_a_location_add(self) -> None:
        client = FakeClient()
        client.virtual_circuits = [MagicMock(id="vc-1")]
        gen = physical_generator(client)

        await gen.generate(physical_payload(role="none", locations=[CAGE]))

        gen.run_generator.assert_awaited_once_with("add_virtual_circuit", ["vc-1"], wait=False)

    @pytest.mark.asyncio
    async def test_a_device_without_deployment_derives_nothing(self) -> None:
        """Both ends authored: nothing to derive, nothing written."""
        client = FakeClient()
        ends = [cage_edge(), interface("if-x", "eth1", "dev-x", no_deployment=True)]

        await physical_generator(client).generate(physical_payload(role="none", ends=ends, locations=[CAGE, ISP]))

        assert client.writes == []


class TestPhysicalCircuitPlacementRefusals:
    """A placement problem fails the run before any write, session and fan-out included."""

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
        assert client.created == []
        gen.run_generator.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_names_the_circuit_and_its_endpoints(self) -> None:
        with pytest.raises(GeneratorError) as excinfo:
            await physical_generator(FakeClient()).generate(
                physical_payload(
                    role="none",
                    ends=[dc_border_leaf(), cage_edge(no_deployment=True)],
                    locations=[],
                )
            )

        assert str(excinfo.value).startswith(
            "DF-DC10-EQXFR2 (bl-dc101101/Ethernet1/35 | eg-fr201/Ethernet1/10): needs exactly 2 distinct locations"
        )

    @pytest.mark.asyncio
    async def test_session_refusal_writes_no_location_either(self) -> None:
        """A DCI precondition failing after placement still leaves nothing written."""
        client = FakeClient(key=False)

        with pytest.raises(GeneratorError, match="'dc10-overlay-key' not found"):
            await physical_generator(client).generate(physical_payload(locations=[CAGE]))

        assert client.writes == []


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


# ===========================================================================
# VirtualCircuitGenerator
# ===========================================================================


def _derived(**kwargs: Any) -> list[dict[str, Any]]:
    """derived_underlay() over a cleaned virtual_payload()."""
    circuit = clean_data(virtual_payload(**kwargs))["TopologyVirtualCircuit"][0]
    return derived_underlay(circuit)


class TestDerivedUnderlay:
    """derived_underlay: the physical circuits found in the capabilities of the circuit's own ports."""

    def test_the_circuits_on_the_ports_are_the_underlay(self) -> None:
        underlay = _derived(physical=[underlay_circuit("INET-A"), underlay_circuit("INET-B")])

        assert [pc["id"] for pc in underlay] == ["pc-INET-A", "pc-INET-B"]
        assert underlay[0]["circuit_id"] == "INET-A"
        assert underlay[0]["status"] == "active"

    def test_a_circuit_on_both_ports_counts_once(self) -> None:
        assert [pc["id"] for pc in _derived(on_every_port=True)] == ["pc-INET-C001-WAW-FR2"]

    def test_circuits_on_different_ports_are_kept_in_port_order(self) -> None:
        """First-seen order across the ports, deduplicated by id."""
        edge = _port_with(sdwan_edge(), [underlay_circuit("INET-B"), underlay_circuit("INET-A")])
        gateway = _port_with(sdwan_gateway(), [underlay_circuit("INET-C"), underlay_circuit("INET-B")])
        circuit = {"capabilities": [edge, gateway]}

        assert [pc["id"] for pc in derived_underlay(circuit)] == ["pc-INET-B", "pc-INET-A", "pc-INET-C"]

    def test_other_capability_kinds_are_not_underlay(self) -> None:
        """A BGP process or a virtual circuit on the port is not a physical circuit, even with an id."""
        port = _port_with(
            sdwan_edge(),
            [
                {"__typename": "ManagedBGP", "id": "bgp-1"},
                {"__typename": "TopologyVirtualCircuit", "id": "vc-2"},
                underlay_circuit("INET-A"),
            ],
        )

        assert [pc["id"] for pc in derived_underlay({"capabilities": [port]})] == ["pc-INET-A"]

    def test_a_port_with_nothing_on_it_derives_nothing(self) -> None:
        assert _derived(physical=[]) == []

    @pytest.mark.parametrize(
        "circuit",
        [
            pytest.param({}, id="no-capabilities-key"),
            pytest.param({"capabilities": None}, id="capabilities-none"),
            pytest.param({"capabilities": [{"id": "if-1", "capabilities": None}]}, id="port-capabilities-none"),
            pytest.param({"capabilities": [{"id": "if-1"}]}, id="port-without-capabilities"),
            pytest.param(
                {"capabilities": [{"id": "if-1", "capabilities": [{"typename": "TopologyPhysicalCircuit"}]}]},
                id="circuit-without-id",
            ),
        ],
    )
    def test_missing_data_derives_nothing(self, circuit: dict[str, Any]) -> None:
        assert derived_underlay(circuit) == []


def _port_with(port: dict[str, Any], circuits: list[dict[str, Any]]) -> dict[str, Any]:
    """A cleaned port whose own capabilities hold ``circuits``."""
    return clean_data(
        {**port, "capabilities": {"edges": [{"node": {"__typename": "ManagedBGP"}}, *({"node": c} for c in circuits)]}}
    )


class TestVirtualCircuitOverlay:
    @pytest.mark.asyncio
    async def test_role_none_writes_nothing_and_waits_for_nothing(self) -> None:
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(role="none"))

        assert client.writes == []
        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_managed_link_type_is_skipped(self) -> None:
        """A cloud on-ramp is the provider's session, not ours, even if marked overlay."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(link_type="direct_connect_aws"))

        assert client.writes == []
        assert any("provider-managed" in str(c) for c in gen.logger.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_single_ended_provider_managed_circuit_is_not_warned(self) -> None:
        """A cloud on-ramp from the cage surfaces on one port (VC-C001-EQXFR-AWS-EUC1): its far end is the provider's."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(role="none", link_type="equinix_fabric", interfaces=[sdwan_gateway()]))

        assert not any("interfaces, found" in str(c) for c in gen.logger.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_single_ended_tunnel_is_warned(self) -> None:
        """A tunnel of ours has both ends."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(role="none", interfaces=[sdwan_gateway()]))

        assert any("expected 2 interfaces, found 1" in str(c) for c in gen.logger.warning.call_args_list)

    @pytest.mark.asyncio
    async def test_overlay_session_over_existing_addresses(self) -> None:
        """One tracked eBGP peering; nothing allocated, no interface fetched or written."""
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload())

        assert client.allocations == []
        assert client.interfaces == {}
        assert [name for name, _, _ in client.created] == ["ManagedBGPPeering"]
        _, node = client.created_of("ManagedBGPPeering")[0]
        assert client.saves() == {node.id: TRACKED}

    @pytest.mark.asyncio
    async def test_overlay_session_is_the_shared_peering_payload(self) -> None:
        """The overlay writes through upsert_bgp_peering like the DCI and fabric sessions: BFD,
        standard and extended communities, the endpoints' unicast family, the fabric key."""
        client = FakeClient()
        dc_end = interface(
            "if-bl", "Ethernet1/40", "bl-dc101101", address="10.99.1.1/30", dc="DC10", roles=("regular",)
        )

        await virtual_generator(client).generate(virtual_payload(interfaces=[sdwan_edge(), dc_end]))

        assert _peerings(client) == [
            {
                "name": "C001-SDWAN-FR2-bgp",
                "description": "Overlay eBGP C001-EDGE1/GE1 <-> bl-dc101101/Ethernet1/40 over C001-SDWAN-FR2",
                "status": "active",
                "session_type": "EBGP",
                "peering_role": "overlay",
                "ttl": 1,
                "bfd_enabled": True,
                "send_community": True,
                "send_extended_community": True,
                "address_families": [{"id": "af-ipv4-unicast"}],
                "bgp_processes": [{"id": "C001-EDGE1-bgp-underlay"}, {"id": "bl-dc101101-bgp-regular"}],
                "password": {"id": DC_KEY_ID},
                "capabilities": [{"id": "if-edge"}, {"id": "if-bl"}],
            }
        ]

    @pytest.mark.asyncio
    async def test_overlay_session_family_follows_the_endpoint_addresses(self) -> None:
        client = FakeClient()
        edge = sdwan_edge(address="fd00:99::2/127")
        gateway = sdwan_gateway(address="fd00:99::1/127")

        await virtual_generator(client).generate(virtual_payload(interfaces=[edge, gateway]))

        assert _peerings(client)[0]["address_families"] == [{"id": "af-ipv6-unicast"}]

    @pytest.mark.asyncio
    async def test_missing_address_family_is_created_untracked_under_a_lock(self) -> None:
        client = FakeClient(address_families=False)
        gen = virtual_generator(client)

        await gen.generate(virtual_payload())

        assert gen.lock_keys == ["bgp-af-ipv4-unicast"]
        assert [d["safi"] for d, _ in client.created_of("RoutingBGPAddressFamily")] == ["unicast"]

    @pytest.mark.asyncio
    async def test_session_between_non_dc_ends_is_written_without_a_password(self) -> None:
        """Edge <-> gateway have no fabric, so no overlay key: the session is unauthenticated, no password key."""
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload())

        assert "password" not in _peerings(client)[0]

    @pytest.mark.asyncio
    async def test_waits_for_every_derived_underlay_run(self) -> None:
        gen = virtual_generator(FakeClient())
        payload = virtual_payload(physical=[underlay_circuit("INET-A"), underlay_circuit("INET-B")])

        await gen.generate(payload)

        waited = [c.args for c in gen.wait_for_parent_generator_and_refetch.await_args_list]
        assert waited == [("add_circuit", "pc-INET-A"), ("add_circuit", "pc-INET-B")]

    @pytest.mark.asyncio
    async def test_a_circuit_shared_by_both_ports_is_waited_for_once(self) -> None:
        """Both ends of the tunnel list the same physical circuit: one wait, not two."""
        gen = virtual_generator(FakeClient())

        await gen.generate(virtual_payload(on_every_port=True))

        waited = [c.args for c in gen.wait_for_parent_generator_and_refetch.await_args_list]
        assert waited == [("add_circuit", "pc-INET-C001-WAW-FR2")]

    @pytest.mark.asyncio
    async def test_no_underlay_waits_for_nothing(self) -> None:
        gen = virtual_generator(FakeClient())

        await gen.generate(virtual_payload(physical=[]))

        gen.wait_for_parent_generator_and_refetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refetched_data_wins(self) -> None:
        """Data re-collected after the underlay run is what the session is built from."""
        client = FakeClient()
        gen = virtual_generator(client)
        gen.wait_for_parent_generator_and_refetch.return_value = virtual_payload(role="none")

        await gen.generate(virtual_payload())

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_dc_end_keys_the_session_with_its_fabric_key(self) -> None:
        client = FakeClient()
        dc_end = interface("if-bl", "Ethernet1/40", "bl-dc101101", address="10.0.0.1/31", dc="DC10", roles=("regular",))

        await virtual_generator(client).generate(virtual_payload(interfaces=[sdwan_edge(), dc_end]))

        assert _peerings(client)[0]["password"] == {"id": DC_KEY_ID}

    @pytest.mark.asyncio
    async def test_decommissioned_underlay_builds_no_session(self) -> None:
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload(physical=[underlay_circuit(status="decommissioned")]))

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_one_live_underlay_circuit_keeps_the_session(self) -> None:
        """Only a wholly decommissioned underlay removes the session; a live sibling keeps it."""
        client = FakeClient()
        underlay = [underlay_circuit("INET-A", status="decommissioned"), underlay_circuit("INET-B")]

        await virtual_generator(client).generate(virtual_payload(physical=underlay))

        assert _peerings(client)[0]["status"] == "active"

    @pytest.mark.parametrize("status", ["active", "provisioning"])
    @pytest.mark.asyncio
    async def test_an_empty_derived_underlay_builds_no_session_yet(self, status: str) -> None:
        """Ports not linked to a physical circuit yet: nothing is written and no error, an info line says why,
        and it is never reported as a decommissioned underlay."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(physical=[], status=status))

        assert client.writes == []
        gen.logger.error.assert_not_called()
        messages = _info_messages(gen)
        assert any("ports not linked to a physical circuit yet" in m for m in messages)
        assert not any("decommissioned" in m for m in messages)

    @pytest.mark.asyncio
    async def test_the_rerun_once_the_ports_are_linked_writes_exactly_one_active_peering(self) -> None:
        """The capabilities-updated trigger reruns the circuit: first run nothing, second run the one session."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(physical=[]))

        assert _peerings(client) == []
        assert any("ports not linked to a physical circuit yet" in m for m in _info_messages(gen))

        await gen.generate(virtual_payload(physical=[underlay_circuit("INET-A")]))

        assert [(d["name"], d["status"]) for d in _peerings(client)] == [("C001-SDWAN-FR2-bgp", "active")]
        _, node = client.created_of("ManagedBGPPeering")[0]
        assert client.saves()[node.id] == TRACKED
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_live_underlay_builds_the_session_once_the_ports_are_linked(self) -> None:
        """The same active circuit with a live derived underlay: the session is written active."""
        client = FakeClient()
        gen = virtual_generator(client)

        await gen.generate(virtual_payload(physical=[underlay_circuit("INET-A")], status="active"))

        assert [d["name"] for d in _peerings(client)] == ["C001-SDWAN-FR2-bgp"]
        assert _peerings(client)[0]["status"] == "active"
        gen.logger.error.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_decommissioned_circuit_ignored_for_underlay_active(self) -> None:
        """A decommissioned sibling beside a live one that is down: the session stays provisioning."""
        client = FakeClient()
        underlay = [underlay_circuit("INET-A", status="decommissioned"), underlay_circuit("INET-B", status="down")]

        await virtual_generator(client).generate(virtual_payload(physical=underlay))

        assert _peerings(client)[0]["status"] == "provisioning"

    @pytest.mark.asyncio
    async def test_a_decommissioned_circuit_without_underlay_builds_no_session(self) -> None:
        """The circuit's own status still stops the session when no underlay is derived."""
        client = FakeClient()

        gen = virtual_generator(client)

        await gen.generate(virtual_payload(physical=[], status="decommissioned"))

        assert client.writes == []
        assert any("decommissioned" in m for m in _info_messages(gen))

    @pytest.mark.asyncio
    async def test_underlay_down_keeps_the_session_provisioning(self) -> None:
        client = FakeClient()

        await virtual_generator(client).generate(virtual_payload(physical=[underlay_circuit(status="down")]))

        assert _peerings(client)[0]["status"] == "provisioning"


class TestVirtualCircuitRefusals:
    @pytest.mark.parametrize(
        ("payload", "client_kwargs", "message"),
        [
            pytest.param(virtual_payload(interfaces=[sdwan_edge()]), {}, "exactly 2 endpoint interfaces", id="one-end"),
            pytest.param(
                virtual_payload(interfaces=[sdwan_edge(), sdwan_gateway(address=None)]),
                {},
                "has no IP address",
                id="no-address",
            ),
            pytest.param(
                virtual_payload(
                    interfaces=[
                        sdwan_edge(),
                        interface("if-x", "eth1", "GW2", address="10.0.0.1/31", roles=("underlay", "overlay")),
                    ]
                ),
                {},
                "GW2 needs one ManagedBGP process",
                id="ambiguous-process",
            ),
            pytest.param(
                virtual_payload(
                    interfaces=[
                        sdwan_edge(),
                        interface("if-bl", "Ethernet1/40", "bl-dc101101", address="10.0.0.1/31", dc="DC10"),
                    ]
                ),
                {},
                "bl-dc101101 needs one ManagedBGP process",
                id="dc-end-without-regular-process",
            ),
            pytest.param(
                virtual_payload(
                    interfaces=[
                        sdwan_edge(),
                        interface(
                            "if-bl", "Ethernet1/40", "bl-dc101101", address="10.0.0.1/31", dc="DC10", roles=("regular",)
                        ),
                    ]
                ),
                {"key": False},
                "'dc10-overlay-key' not found",
                id="dc-end-without-key",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_refusal_writes_nothing(self, payload: dict, client_kwargs: dict, message: str) -> None:
        client = FakeClient(**client_kwargs)

        with pytest.raises(GeneratorError, match=message):
            await virtual_generator(client).generate(payload)

        assert client.writes == []

    @pytest.mark.asyncio
    async def test_empty_response_fails(self) -> None:
        with pytest.raises(GeneratorError, match="No TopologyVirtualCircuit"):
            await virtual_generator(FakeClient()).generate({"TopologyVirtualCircuit": {"edges": []}})
