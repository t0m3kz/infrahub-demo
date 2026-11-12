"""Unit tests for CheckCircuitPlacement (where circuits terminate, what carries them, which environment they are in)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.unit.circuit_placement_check_helpers import (
    AM_NE,
    AWS_END,
    AWS_VLAN,
    AWS_VLAN_NO_CARRIER,
    BL10,
    BL10_END,
    BL11,
    BL11_END,
    C001_OFFICE,
    C002_OFFICE,
    DC10,
    DC11,
    DCI,
    DF10,
    DF11,
    EG_FR_DF10,
    EG_FR_DF11,
    EG_FR_END,
    EG_FR_METRO_END,
    EG_FR_VLAN,
    EUC1,
    FR2,
    FR6,
    GW_ETH0_END,
    GW_PUBLIC,
    HANDOFF,
    INET_GW,
    INET_OFFICE,
    LUMEN_PE_END,
    OFFICE_EDGE,
    OFFICE_EDGE_END,
    ORANGE_PE_END,
    PA4,
    TUNNEL,
    XC,
    XC_AM_NE,
    _carrier_circuit,
    _decommissioned,
    _edges,
    _end,
    _handoff,
    _interconnect,
    _place,
    _run,
    _tunnel,
    _vport,
)


class TestCarrierCircuitPlacement:
    @pytest.mark.parametrize(
        "circuit",
        [
            DF10,
            XC,
            INET_OFFICE,
            INET_GW,
            # A device racked in the cage itself places the cage end too.
            _carrier_circuit("XC-GW-AWS", [FR2, EUC1], [GW_ETH0_END, AWS_END]),
            # A virtual colocation is a circuit end like a zone.
            XC_AM_NE,
        ],
        ids=[
            "dc-to-cage",
            "cage-to-region",
            "office-to-isp",
            "gateway-to-isp",
            "device-in-the-cage",
            "virtual-colocation-to-region",
        ],
    )
    def test_placed_circuit_passes(self, circuit: dict[str, Any]) -> None:
        """Each location is the deployment of one end's device: no finding."""
        assert _run(carrier=[circuit]) == []

    @pytest.mark.parametrize("locations", [[FR2], []], ids=["only-the-authored-cage", "none"])
    def test_a_placed_end_without_its_location_fails(self, locations: list[dict[str, Any]]) -> None:
        """Two placed ends need two locations (a derived end missing) and each must be the end's deployment."""
        messages = _run(carrier=[_carrier_circuit("DF-DC10-EQXFR2", locations, [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "Carrier circuit 'DF-DC10-EQXFR2' terminates on" in messages[0]
        assert "its placed ends are" in messages[0]

    def test_three_locations_fail(self) -> None:
        """At most two locations."""
        messages = _run(carrier=[_carrier_circuit("X", [DC10, DC11, FR2], [BL10_END, EG_FR_END])])
        assert len(messages) == 1 and "at most 2 distinct locations" in messages[0]

    def test_a_router_not_owned_by_the_circuit_provider_fails(self) -> None:
        """The circuit's provider must name the router end."""
        circuit = _carrier_circuit(
            "X", [C001_OFFICE], [OFFICE_EDGE_END, ORANGE_PE_END], provider="LUMEN", circuit_type="internet"
        )
        messages = _run(carrier=[circuit])
        assert len(messages) == 1 and "none is owned by the circuit's provider" in messages[0]

    def test_two_provider_routers_need_no_location(self) -> None:
        """A partner's access from its ISP: both ends are provider routers, the circuit has no location."""
        partner = _end("EXT-ACME-EDGE1", "et-0/0/0", None, owner="ACME")
        circuit = _carrier_circuit("X", [], [partner, LUMEN_PE_END], provider="LUMEN", circuit_type="internet")
        assert _run(carrier=[circuit]) == []

    def test_a_router_with_a_location_that_matches_no_end_fails(self) -> None:
        """A location with no placed end behind it is drift."""
        circuit = _carrier_circuit("X", [C001_OFFICE, FR2], [OFFICE_EDGE_END, ORANGE_PE_END], provider="ORANGE")
        assert len(_run(carrier=[circuit])) == 1

    def test_end_device_without_deployment_fails(self) -> None:
        """An end on a device with no deployment cannot place its end: error naming the port."""
        undeployed = _end("EXT-AWS-DX-FR2", "et-0/0/0", None)
        messages = _run(carrier=[_carrier_circuit("XC-EQXFR2-AWS-EUC1", [FR2, EUC1], [EG_FR_END, undeployed])])
        assert len(messages) == 1
        assert "end 'EXT-AWS-DX-FR2:et-0/0/0' is on a device with no deployment" in messages[0]

    def test_dc_end_other_than_the_device_deployment_fails(self) -> None:
        """Authored DC10 while the border leaf sits in DC11: the DC end is the device's deployment."""
        messages = _run(carrier=[_carrier_circuit("DF-DC10-EQXFR2", [DC10, FR2], [BL11_END, EG_FR_END])])
        assert len(messages) == 1
        assert "Carrier circuit 'DF-DC10-EQXFR2' terminates on" in messages[0]
        assert "bl-dc1111101:Ethernet1/35 in TopologyDataCenter 'DC11'" in messages[0]

    def test_cage_of_another_metro_fails(self) -> None:
        """A colocation end must be the edge's zone itself: PA4 with an edge in FR2 fails."""
        messages = _run(carrier=[_carrier_circuit("DF-DC10-EQXPA4", [DC10, PA4], [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "not a sibling zone of its metro" in messages[0]

    def test_virtual_colocation_of_another_metro_fails(self) -> None:
        """AM-NE is not where an edge in zone FR2 is deployed."""
        messages = _run(carrier=[_carrier_circuit("DF-DC10-EQXAMNE", [DC10, AM_NE], [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "Carrier circuit 'DF-DC10-EQXAMNE' terminates on" in messages[0]

    def test_sibling_cage_of_the_same_metro_fails(self) -> None:
        """Authored FR6 while the edge sits in FR2: add_circuit."""
        messages = _run(carrier=[_carrier_circuit("DF-DC10-EQXFR6", [DC10, FR6], [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "eg-fr201:Ethernet1/13 in TopologyColocationZone 'FR2'" in messages[0]
        assert "TopologyColocationZone 'FR6'" in messages[0]

    def test_device_deployed_to_the_metro_fails(self) -> None:
        """No device is deployed to a metro any more: a legacy metro deployment places no zone of it."""
        messages = _run(carrier=[_carrier_circuit("DF-DC10-EQXFR2-METRO", [DC10, FR2], [BL10_END, EG_FR_METRO_END])])
        assert len(messages) == 1
        assert "eg-fr01:Ethernet1/13 in TopologyColocationMetro 'FR'" in messages[0]

    def test_two_zones_of_different_metros_with_edges_in_one_fail(self) -> None:
        """FR2 and PA4 with both edges in FR2: one location has no end."""
        messages = _run(carrier=[_carrier_circuit("XC-FR2-PA4", [FR2, PA4], [EG_FR_END, _end("eg-fr202", "e1", FR2)])])
        assert len(messages) == 1

    def test_ends_must_match_locations_one_to_one(self) -> None:
        """Both ends in DC10 leave the FR2 location without an end."""
        messages = _run(
            carrier=[_carrier_circuit("DF-DC10-EQXFR2", [DC10, FR2], [BL10_END, _end("bl-dc101102", "e1", DC10)])]
        )
        assert len(messages) == 1

    def test_end_count_is_left_to_carrier_circuit_ports(self) -> None:
        """A circuit with one end gets no placement finding: checks/carrier_circuit_ports.py reports it."""
        assert _run(carrier=[_carrier_circuit("DF-DC10-EQXFR2", [DC10, FR2], [BL10_END])]) == []


class TestCarrierCircuitOwnership:
    """The owner of a circuit is derived from the offices among its locations: they are of one owner."""

    def test_two_offices_of_one_owner_pass(self) -> None:
        """Two offices of the same owner derive one owner."""
        same = _place("C001-OFFICE-2", "TopologyCustomerOffice", owner="C001")
        circuit = _carrier_circuit("OFFICE-LINK", [C001_OFFICE, same], [OFFICE_EDGE_END, _end("o2", "GE1", same)])
        assert _run(carrier=[circuit]) == []

    def test_two_offices_of_different_owners_fail(self) -> None:
        """The owner cannot be derived from offices of C001 and C002."""
        circuit = _carrier_circuit(
            "OFFICE-LINK", [C001_OFFICE, C002_OFFICE], [OFFICE_EDGE_END, _end("C002-OFFICE-EDGE1", "GE1", C002_OFFICE)]
        )
        messages = _run(carrier=[circuit])
        assert len(messages) == 1
        assert "Carrier circuit 'OFFICE-LINK' has an owner that cannot be derived" in messages[0]
        assert "offices of different owners (C001, C002)" in messages[0]

    def test_a_circuit_with_no_office_has_no_owner_to_derive(self) -> None:
        """A dark fibre has no office: shared underlay, no finding."""
        assert _run(carrier=[DF10]) == []

    def test_ownership_is_checked_with_a_location_missing(self) -> None:
        """The owner rule does not wait for the derived end: both offices are visible with the shape broken."""
        circuit = _carrier_circuit("X", [C001_OFFICE, C002_OFFICE], [OFFICE_EDGE_END, ORANGE_PE_END])
        messages = _run(carrier=[circuit])
        assert "cannot be derived" in messages[0]


class TestTunnelCircuit:
    """Office edge to SD-WAN gateway: owned by the office's owner, every end on its own live internet circuit."""

    def test_tunnel_passes(self) -> None:
        """The reference tunnel (office edge to SD-WAN gateway, each on its own internet circuit) has no finding."""
        assert _run(tunnels=[TUNNEL]) == []

    def test_location_order_and_end_order_do_not_matter(self) -> None:
        """The query does not order locations or ends, so the check must not depend on it."""
        circuit = _tunnel("C001-OFFICE-SDWAN-FR2", [FR2, C001_OFFICE], [GW_PUBLIC, OFFICE_EDGE])
        assert _run(tunnels=[circuit]) == []

    def test_tunnels_sharing_a_gateway_port_pass(self) -> None:
        """One gateway port serves many tunnels: a tunnel is exempt from the one-session-per-port rule."""
        other_edge = _vport(
            "C002-OFFICE-EDGE1",
            "GE1",
            (_carrier_circuit("INET-C002-OFFICE", [C002_OFFICE], [], circuit_type="internet"),),
        )
        other = _tunnel("C002-OFFICE-SDWAN-FR2", [C002_OFFICE, FR2], [other_edge, GW_PUBLIC])
        assert _run(tunnels=[TUNNEL, other]) == []

    def test_not_two_locations_fails(self) -> None:
        """A tunnel with one location is reported; it needs the office and the gateway's facility."""
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE], [OFFICE_EDGE, GW_PUBLIC])])
        assert len(messages) == 1
        assert "Tunnel circuit 'T' terminates on 1 location(s)" in messages[0]
        assert "exactly 2 distinct locations" in messages[0]

    def test_not_two_end_ports_fails(self) -> None:
        """A tunnel with a single end port is reported: one port per end, two in all."""
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE])])
        assert messages == ["Tunnel circuit 'T' has 1 end port(s): it terminates on exactly 2 ports, one per end."]

    def test_no_office_fails(self) -> None:
        """A tunnel with no office among its locations has no owner to derive."""
        messages = _run(tunnels=[_tunnel("T", [FR2, EUC1], [OFFICE_EDGE, GW_PUBLIC])])
        assert len(messages) == 1
        assert "Tunnel circuit 'T' has no office among its locations" in messages[0]
        assert "a tunnel is owned by its office's owner" in messages[0]

    def test_offices_of_different_owners_fail(self) -> None:
        """Two offices of different owners on one tunnel: the owner cannot be derived."""
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, C002_OFFICE], [OFFICE_EDGE, GW_PUBLIC])])
        assert len(messages) == 1 and "cannot be derived" in messages[0]

    def test_end_on_no_carrier_circuit_fails(self) -> None:
        """A tunnel end whose port rides no carrier circuit is reported as riding no live internet circuit."""
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, _vport("EQX-FR2-SDWAN-GW1", "eth1")])])
        assert len(messages) == 1
        assert "end 'EQX-FR2-SDWAN-GW1:eth1' rides no live internet carrier circuit" in messages[0]
        assert "it is on no live one" in messages[0]

    def test_end_on_a_circuit_that_is_not_internet_fails(self) -> None:
        """The gateway port is on a dark fibre, not on the internet."""
        messages = _run(
            tunnels=[_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, _vport("EQX-FR2-SDWAN-GW1", "eth1", (DF10,))])]
        )
        assert len(messages) == 1
        assert "its live carrier circuits are DF-DC10-EQXFR2 (DC10 - FR2)" in messages[0]

    def test_end_on_a_decommissioned_internet_circuit_fails(self) -> None:
        """A decommissioned internet circuit is not a carrier, and is left out of the message."""
        port = _vport("C001-OFFICE-EDGE1", "GE1", (_decommissioned(INET_OFFICE),))
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [port, GW_PUBLIC])])
        assert len(messages) == 1
        assert "end 'C001-OFFICE-EDGE1:GE1' rides no live internet carrier circuit" in messages[0]
        assert "INET-C001-OFFICE" not in messages[0]

    def test_a_cloud_peering_end_needs_no_carrier(self) -> None:
        """The far end of a SaaS tunnel is the transport's CloudPeeringInterface: the provider's side, no carrier."""
        peering = _vport("zscaler-c900-muc", "Tunnel1", kind="CloudPeeringInterface", region=EUC1)
        assert _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, peering])]) == []

    def test_a_cloud_peering_end_does_not_excuse_the_office_end(self) -> None:
        """The exemption is for the provider's end only: our own end still needs its internet access."""
        peering = _vport("zscaler-c900-muc", "Tunnel1", kind="CloudPeeringInterface", region=EUC1)
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [_vport("C001-OFFICE-EDGE1", "GE1"), peering])])
        assert len(messages) == 1 and "end 'C001-OFFICE-EDGE1:GE1' rides no live internet carrier" in messages[0]

    def test_each_end_needs_its_own_internet_circuit(self) -> None:
        """A shared carrier would not do: both ends are checked, one finding per end."""
        messages = _run(
            tunnels=[
                _tunnel(
                    "T", [C001_OFFICE, FR2], [_vport("C001-OFFICE-EDGE1", "GE1"), _vport("EQX-FR2-SDWAN-GW1", "eth1")]
                )
            ]
        )
        assert len(messages) == 2


class TestInterconnectCircuit:
    """DCI between two border gateways: unowned, each end on a live circuit, one contiguous path, pure fibre."""

    def test_pure_fibre_passes(self) -> None:
        """The reference DCI (border leaf to cage edge over one dark fibre) has no finding."""
        assert _run(interconnects=[DCI]) == []

    def test_ends_and_locations_in_either_order_pass(self) -> None:
        """The query does not order locations or ends, so the check must not depend on it."""
        circuit = _interconnect("DCI-DC10-FR2", [FR2, DC10], [EG_FR_DF10, BL10])
        assert _run(interconnects=[circuit]) == []

    def test_a_chain_of_carriers_in_any_order_passes(self) -> None:
        """DC10 to DC11 over the fibre to FR2 and the fibre back: contiguous, whatever the peers' order."""
        for ends in ([BL10, BL11], [BL11, BL10]):
            assert _run(interconnects=[_interconnect("DCI-DC10-DC11", [DC10, DC11], ends)]) == []

    def test_an_office_in_the_locations_fails(self) -> None:
        """A DCI is shared platform: an office with an owner among its locations is reported."""
        messages = _run(interconnects=[_interconnect("DCI-DC10-FR2", [DC10, C001_OFFICE], [BL10, EG_FR_DF10])])
        assert any(
            "Interconnect circuit 'DCI-DC10-FR2' terminates on" in m and "names no office" in m for m in messages
        )

    def test_not_two_locations_fails(self) -> None:
        """A DCI with one location is reported."""
        messages = _run(interconnects=[_interconnect("DCI", [DC10], [BL10, EG_FR_DF10])])
        assert len(messages) == 1
        assert "Interconnect circuit 'DCI' terminates on 1 location(s)" in messages[0]

    def test_not_two_end_ports_fails(self) -> None:
        """A DCI with a single end port is reported."""
        messages = _run(interconnects=[_interconnect("DCI", [DC10, FR2], [BL10])])
        assert any("has 1 end port(s)" in message for message in messages)

    def test_end_on_no_live_carrier_circuit_fails(self) -> None:
        """A DCI end whose port is on no carrier circuit is reported with the fix to apply."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, _vport("eg-fr201", "Ethernet1/10")])
        messages = _run(interconnects=[circuit])
        assert messages == [
            "Interconnect circuit 'DCI' end 'eg-fr201:Ethernet1/10' is on no live carrier circuit: terminate it on "
            "a port cabled through a carrier circuit."
        ]

    def test_chain_not_reaching_the_far_location_fails(self) -> None:
        """The dark fibre stops at FR2, short of the region."""
        messages = _run(interconnects=[_interconnect("DCI", [DC10, EUC1], [BL10, EG_FR_DF10])])
        assert len(messages) == 1
        assert (
            "carrier DF-DC10-EQXFR2 (DC10 - FR2) is not one contiguous path from TopologyDataCenter 'DC10' to"
            in (messages[0])
        )
        assert "TopologyCloudRegion 'eu-central-1'" in messages[0]

    def test_carrier_touching_neither_location_fails(self) -> None:
        """DC11's fibre is no path between DC10 and FR2."""
        messages = _run(
            interconnects=[
                _interconnect("DCI", [DC10, FR2], [_vport("bl-dc101101", "Ethernet1/35", (DF11,)), EG_FR_DF11])
            ]
        )
        assert len(messages) >= 1
        assert any("is not one contiguous path" in message for message in messages)

    def test_extra_carrier_breaks_the_path(self) -> None:
        """Every derived circuit belongs to the one path: DC11's fibre is a stray."""
        circuit = _interconnect("DCI", [DC10, FR2], [_vport("bl-dc101101", "Ethernet1/35", (DF10, DF11)), EG_FR_DF10])
        messages = _run(interconnects=[circuit])
        assert len(messages) == 1
        assert "is not one contiguous path" in messages[0]

    def test_a_decommissioned_circuit_is_ignored(self) -> None:
        """A decommissioned stray does not break the live path."""
        circuit = _interconnect(
            "DCI", [DC10, FR2], [_vport("bl-dc101101", "Ethernet1/35", (DF10, _decommissioned(DF11))), EG_FR_DF10]
        )
        assert _run(interconnects=[circuit]) == []

    def test_a_decommissioned_carrier_is_no_carrier(self) -> None:
        """When both ends only ride a decommissioned circuit, each end is reported as on no live circuit."""
        circuit = _interconnect(
            "DCI",
            [DC10, FR2],
            [
                _vport("bl-dc101101", "Ethernet1/35", (_decommissioned(DF10),)),
                _vport("eg-fr201", "Ethernet1/10", (_decommissioned(DF10),)),
            ],
        )
        messages = _run(interconnects=[circuit])
        assert len(messages) == 2
        assert all("is on no live carrier circuit" in message for message in messages)

    def test_a_carrier_on_other_ports_than_the_circuit_names_fails(self) -> None:
        """The circuit names eg-fr201 Ethernet1/12 but DF-DC10-EQXFR2 terminates on Ethernet1/10: drift."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, _vport("eg-fr201", "Ethernet1/12", (DF10,))])
        messages = _run(interconnects=[circuit])
        assert len(messages) == 1
        assert (
            "are not the ends of its carrier DF-DC10-EQXFR2 (bl-dc101101:Ethernet1/35, eg-fr201:Ethernet1/10)"
            in (messages[0])
        )
        assert "pure fibre" in messages[0]

    def test_sub_interface_ends_are_not_pure_fibre(self) -> None:
        """Sub-interfaces derive the parent's circuit, whose ends are the parents, not the circuit's ports."""
        ends = [
            _vport("bl-dc101101", "Ethernet1/35.100", parent=("Ethernet1/35", (DF10,))),
            _vport("eg-fr201", "Ethernet1/10.100", parent=("Ethernet1/10", (DF10,))),
        ]
        messages = _run(interconnects=[_interconnect("DCI", [DC10, FR2], ends)])
        assert len(messages) == 1
        assert "pure fibre" in messages[0]

    @pytest.mark.parametrize(
        ("bgp", "count"),
        [((), 0), (("underlay",), 0), (("overlay", "overlay"), 2)],
        ids=["none", "underlay-only", "two"],
    )
    def test_end_device_without_exactly_one_overlay_process_fails(self, bgp: tuple[str, ...], count: int) -> None:
        """A Network Edge router has no overlay process: it is kept out of a DCI."""
        ends = [BL10, _vport("eg-fr201", "Ethernet1/10", (DF10,), bgp=bgp)]
        messages = _run(interconnects=[_interconnect("DCI", [DC10, FR2], ends)])
        assert messages == [
            f"Interconnect circuit 'DCI' end device 'eg-fr201' has {count} ManagedBGP process(es) of "
            "process_role 'overlay': a border gateway runs exactly one."
        ]


class TestHandoffCircuit:
    """Our edge sub-interface to a party sub-interface: a VLAN joins them, a physical end rides a circuit."""

    def test_handoff_passes(self) -> None:
        """The reference handoff (VLAN 3101 sub-interfaces on both ends, riding a cross-connect) has no finding."""
        assert _run(handoffs=[HANDOFF]) == []

    @pytest.mark.parametrize(
        ("locations", "ends"),
        [
            pytest.param(
                [FR2],
                [EG_FR_VLAN, _vport("EQX-FR2-SDWAN-GW1", "eth0.3101", parent=("eth0", (XC,)))],
                id="one-facility",
            ),
            pytest.param([FR2, EUC1], [EG_FR_VLAN, AWS_VLAN], id="two-facilities"),
        ],
    )
    def test_one_or_two_locations_pass(self, locations: list[dict[str, Any]], ends: list[dict[str, Any]]) -> None:
        """A handoff sits in one facility (both ends in one cage) or spans two (cage and region): both are valid."""
        assert _run(handoffs=[_handoff("HO", locations, ends)]) == []

    @pytest.mark.parametrize(
        ("locations", "ends"),
        [
            pytest.param([FR2], [EG_FR_VLAN, AWS_VLAN], id="an-end-is-deployed-in-no-location"),
            pytest.param(
                [FR2, EUC1],
                [EG_FR_VLAN, _vport("EQX-FR2-SDWAN-GW1", "eth0.3101", (XC,), parent=("eth0", ()))],
                id="location-no-end-is-in",
            ),
            pytest.param([FR2, PA4], [EG_FR_VLAN, AWS_VLAN], id="wrong-facility"),
        ],
    )
    def test_locations_that_differ_from_where_the_ends_are_deployed_fail(
        self, locations: list[dict[str, Any]], ends: list[dict[str, Any]]
    ) -> None:
        """Nothing derives a handoff's locations: they must be exactly the facilities its two end devices sit in."""
        messages = _run(handoffs=[_handoff("HO", locations, ends)])
        assert len(messages) == 1
        assert messages[0].startswith("Handoff circuit 'HO' terminates on ")
        assert "locations are exactly the facilities its two end devices are deployed in" in messages[0]

    def test_a_location_that_no_end_device_is_deployed_in_is_named(self) -> None:
        """The finding names the location and the deployment of each end."""
        (message,) = _run(handoffs=[_handoff("HO", [FR2, PA4], [EG_FR_VLAN, AWS_VLAN])])
        assert "TopologyColocationZone 'PA4'" in message
        assert "eg-fr201:Ethernet1/13.3101 in TopologyColocationZone 'FR2'" in message
        assert "EXT-AWS-DX-FR2:et-0/0/0.3101 in TopologyCloudRegion 'eu-central-1'" in message

    def test_an_end_device_with_no_deployment_fails(self) -> None:
        """A device with no deployment can sit in none of the handoff's locations."""
        undeployed = _vport("STUB-1", "et-0/0/0.3101", parent=("et-0/0/0", (XC,)))
        (message,) = _run(handoffs=[_handoff("HO", [FR2], [EG_FR_VLAN, undeployed])])
        assert "STUB-1:et-0/0/0.3101 in <unknown kind> '<unknown>'" in message

    def test_a_decommissioned_handoff_is_not_held_to_where_its_ends_are(self) -> None:
        """add_virtual_circuit builds nothing for it: like every rule, this one skips a decommissioned circuit."""
        circuit = {**_handoff("HO", [FR2], [EG_FR_VLAN, AWS_VLAN]), "status": {"value": "decommissioned"}}
        assert _run(handoffs=[circuit]) == []

    def test_no_location_fails(self) -> None:
        """A handoff with no location is reported; it needs one or two."""
        messages = _run(handoffs=[_handoff("HO", [], [EG_FR_VLAN, AWS_VLAN])])
        assert len(messages) == 1
        assert "Handoff circuit 'HO' terminates on 0 location(s)" in messages[0]
        assert "1 or 2 distinct locations" in messages[0]

    def test_not_two_end_ports_fails(self) -> None:
        """A handoff with a single end port is reported."""
        messages = _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN])])
        assert messages == ["Handoff circuit 'HO' has 1 end port(s): it terminates on exactly 2 ports, one per end."]

    def test_a_handoff_naming_an_office_fails(self) -> None:
        """A handoff is shared platform: an office with an owner among its locations is reported."""
        assert _run(handoffs=[HANDOFF]) == []
        messages = _run(handoffs=[_handoff("HO", [FR2, C001_OFFICE], [EG_FR_VLAN, AWS_VLAN])])
        assert any("Handoff circuit 'HO' terminates on" in m and "names no office" in m for m in messages)

    def test_a_handoff_without_a_namespace_fails(self) -> None:
        """The schema leaves ip_namespace optional (a dci and a tunnel have none); a handoff needs it."""
        messages = _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, AWS_VLAN], namespace=None)])
        assert messages == [
            "Handoff circuit 'HO' has no vrf: its /31 is allocated and its session runs in an environment namespace."
        ]

    @pytest.mark.parametrize("field", ["registered_prefixes"])
    def test_a_handoff_with_a_tunnel_only_field_fails(self, field: str) -> None:
        """Only a tunnel has registered_prefixes."""
        circuit = {**HANDOFF, field: _edges([{"id": "pfx-1"}])}
        messages = _run(handoffs=[circuit])
        assert len(messages) == 1 and field in messages[0]

    def test_carrier_on_the_port_itself_passes(self) -> None:
        """A handoff on an unchannelled port: the port's own circuit is the carrier."""
        ends = [_vport("eg-fr201", "Ethernet1/13.3101", (XC,), parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        assert _run(handoffs=[_handoff("HO", None, ends)]) == []

    def test_one_physical_end_with_a_carrier_is_enough(self) -> None:
        """The stub has no circuit at its parent port, the edge has: the handoff rides it."""
        assert _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, AWS_VLAN_NO_CARRIER])]) == []

    def test_a_cable_on_the_parent_port_is_a_carrier(self) -> None:
        """Both ends in one cage: a hop inside one facility is a DcimCable, not a carrier circuit."""
        ends = [
            _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ()), parent_cabled=True),
            _vport("EQX-FR2-SDWAN-GW1", "eth0.3101", parent=("eth0", ())),
        ]
        assert _run(handoffs=[_handoff("HO", None, ends)]) == []

    def test_a_cable_with_a_decommissioned_circuit_still_passes(self) -> None:
        """A cable has no status: the dead circuit is ignored, the cable carries."""
        ends = [
            _vport(
                "eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (_decommissioned(XC),)), parent_cabled=True
            ),
            AWS_VLAN_NO_CARRIER,
        ]
        assert _run(handoffs=[_handoff("HO", None, ends)]) == []

    def test_a_decommissioned_circuit_and_no_cable_fails(self) -> None:
        """A decommissioned circuit and no cable on the only physical end leaves the handoff with no carrier."""
        ends = [
            _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (_decommissioned(XC),))),
            AWS_VLAN_NO_CARRIER,
        ]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert len(messages) == 1
        assert "no live carrier circuit and no cable" in messages[0]

    def test_no_physical_end_needs_no_carrier(self) -> None:
        """Both ends on Network Edge appliances or virtual devices: there is no carrier circuit to require."""
        ends = [
            _vport("eg-am-ne01", "Ethernet1/13.3101", parent=("Ethernet1/13", ()), device_kind="CloudNetworkAppliance"),
            _vport("EXT-AWS-DX-AM", "et-0/0/0.3101", parent=("et-0/0/0", ()), device_kind="DcimVirtualDevice"),
        ]
        assert _run(handoffs=[_handoff("HO", None, ends)]) == []

    def test_an_appliance_end_with_a_physical_end_without_carrier_fails(self) -> None:
        """The appliance needs none, but the physical end is the one that is held to it."""
        ends = [
            _vport("eg-am-ne01", "Ethernet1/13.3101", parent=("Ethernet1/13", ()), device_kind="CloudNetworkAppliance"),
            AWS_VLAN_NO_CARRIER,
        ]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert len(messages) == 1
        assert "has no live carrier circuit and no cable at EXT-AWS-DX-FR2:et-0/0/0.3101" in messages[0]

    def test_no_carrier_on_physical_ends_fails(self) -> None:
        """Physical ends with no circuit and no cable are reported, naming the first such port."""
        ends = [_vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert len(messages) == 1
        assert (
            "Handoff circuit 'HO' has no live carrier circuit and no cable at eg-fr201:Ethernet1/13.3101" in messages[0]
        )

    def test_decommissioned_carriers_do_not_count(self) -> None:
        """A decommissioned circuit on every end carries nothing: the handoff has no carrier."""
        ends = [
            _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (_decommissioned(XC),))),
            _vport("EXT-AWS-DX-FR2", "et-0/0/0.3101", parent=("et-0/0/0", (_decommissioned(XC),))),
        ]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert len(messages) == 1
        assert "has no live carrier circuit and no cable" in messages[0]

    def test_end_that_is_not_a_vlan_sub_interface_fails(self) -> None:
        """A handoff end must be a sub-interface with an encapsulation (or a routed port)."""
        ends = [EG_FR_VLAN, _vport("EXT-AWS-DX-FR2", "et-0/0/0", (XC,))]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert messages == [
            "Handoff circuit 'HO' end 'EXT-AWS-DX-FR2:et-0/0/0' is neither a sub-interface (a virtual interface with an encapsulation) "
            "nor a routed physical port (an IP address, no VLAN) nor a cloud peering interface: a physical port with "
            "no IP address and no VLAN cannot carry a handoff."
        ]

    def test_different_vlans_fail(self) -> None:
        """The VLAN is what joins the two ends (3101 against 3102 here), so differing VLANs are reported."""
        other = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        messages = _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, other])])
        assert len(messages) == 1
        assert (
            "ends carry different VLANs (eg-fr201:Ethernet1/13.3101 = 3101, EXT-AWS-DX-FR2:et-0/0/0.3102 = 3102)"
            in (messages[0])
        )
