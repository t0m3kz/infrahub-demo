"""CheckCircuitPlacement: a cloud transport end (no deployment, placed by its region) and a routed-port handoff end."""

from __future__ import annotations

from typing import Any

from tests.unit.circuit_placement_check_helpers import AWS_VLAN as AWS_SUB
from tests.unit.circuit_placement_check_helpers import (
    BL10_END,
    EG_FR_END,
    EG_FR_VLAN,
    EUC1,
    FR2,
    PA4,
    XC,
    _carrier_circuit,
    _handoff,
    _place,
    _run,
    _transport_end,
    _vport,
)

SAAS = _place("webex-eu", "TopologySaasRegion")
DX_END = _transport_end("dx-c900-fra", "Ethernet1", EUC1)
SAAS_END = _transport_end("webex-eu-1", "Ethernet1", SAAS, saas=True)
DX_XC = _carrier_circuit("XC-EQXFR2-DX", [FR2, EUC1], [EG_FR_END, DX_END])


def _peering(vlan_id: int | None = 3101) -> dict[str, Any]:
    """The cloud end of a handoff: a CloudPeeringInterface of the Direct Connect transport."""
    return _vport("dx-c900-fra", "vif-3101", kind="CloudPeeringInterface", vlan_id=vlan_id, region=EUC1)


def _routed(device: str, name: str, carriers: tuple[dict[str, Any], ...], **kwargs: Any) -> dict[str, Any]:
    """A routed physical port: a DcimPhysicalInterface with an IP address, carried at the port itself."""
    return _vport(device, name, carriers, kind="DcimPhysicalInterface", ip_address=True, **kwargs)


class TestCarrierOnACloudTransport:
    """A carrier end on a transport port is placed by the transport's region, not by a deployment."""

    def test_a_transport_port_is_placed_by_its_region(self) -> None:
        """The cross-connect to a Direct Connect port: the cage and the region are the two ends' places."""
        assert _run(carrier=[DX_XC]) == []

    def test_a_saas_transport_port_is_placed_by_its_saas_region(self) -> None:
        """A transport of a SaaS holder sits in a TopologySaasRegion."""
        circuit = _carrier_circuit("XC-EQXFR2-WEBEX", [FR2, SAAS], [EG_FR_END, SAAS_END])
        assert _run(carrier=[circuit]) == []

    def test_a_location_that_is_not_the_transports_region_fails(self) -> None:
        """The circuit lists the cage and the DC while one end sits in a region: the region is not a location."""
        circuit = _carrier_circuit("XC-BAD", [FR2, SAAS], [EG_FR_END, DX_END])
        (message,) = _run(carrier=[circuit])
        assert "TopologyCloudRegion 'eu-central-1'" in message and "TopologySaasRegion 'webex-eu'" in message

    def test_a_transport_with_neither_region_is_reported_as_undeployed(self) -> None:
        """A transport with no region and no SaaS region is placed nowhere: the finding names the port."""
        nowhere = _transport_end("dx-x", "Ethernet1", EUC1)
        nowhere["device"]["node"]["region"] = {"node": None}
        circuit = _carrier_circuit("XC-NOWHERE", [FR2, EUC1], [EG_FR_END, nowhere])
        (message,) = _run(carrier=[circuit])
        assert "end 'dx-x:Ethernet1' is on a device with no deployment" in message

    def test_a_dc_end_and_a_transport_end_pass(self) -> None:
        """Both ends placed by different means (a deployment, a region) are two distinct locations."""
        circuit = _carrier_circuit("DX-DC10", [_place("DC10", "TopologyDataCenter"), EUC1], [BL10_END, DX_END])
        assert _run(carrier=[circuit]) == []


class TestHandoffEnds:
    """The three shapes of a handoff end and what joins them."""

    def test_a_sub_interface_and_a_cloud_peering_with_the_same_vlan_pass(self) -> None:
        """Our edge sub-interface 3101 to the peering of the transport (vlan_id 3101); the far end needs no carrier."""
        assert _run(handoffs=[_handoff("HO-DX", None, [EG_FR_VLAN, _peering(3101)])]) == []

    def test_a_cloud_peering_without_a_vlan_passes(self) -> None:
        """The peering's vlan_id is optional: only the sub-interface states the VLAN."""
        assert _run(handoffs=[_handoff("HO-DX", None, [EG_FR_VLAN, _peering(None)])]) == []

    def test_a_cloud_peering_with_another_vlan_fails(self) -> None:
        """vlan_id 3102 against the sub-interface .3101: the VLAN that joins the two ends differs."""
        (message,) = _run(handoffs=[_handoff("HO-DX", None, [EG_FR_VLAN, _peering(3102)])])
        assert "ends carry different VLANs" in message
        assert "dx-c900-fra:vif-3101 = 3102" in message

    def test_the_tag_is_the_field_whatever_the_name_says(self) -> None:
        """A sub-interface named .9999 that is tagged 3101 joins the peering's 3101; the name is not read."""
        renamed = _vport("eg-fr201", "Ethernet1/13.9999", parent=("Ethernet1/13", (XC,)), vlan_id=3101)
        assert _run(handoffs=[_handoff("HO-DX", None, [renamed, _peering(3101)])]) == []
        (message,) = _run(handoffs=[_handoff("HO-DX", None, [renamed, _peering(9999)])])
        assert "ends carry different VLANs" in message
        assert "eg-fr201:Ethernet1/13.9999 = 3101" in message

    def test_an_untagged_sub_interface_carries_no_vlan_to_compare(self) -> None:
        """No vlan_id is untagged: it is still a sub-interface, and the peering's tag is not contradicted."""
        untagged = _vport("eg-fr201", "Ethernet1/13.100", parent=("Ethernet1/13", (XC,)), vlan_id=None)
        assert _run(handoffs=[_handoff("HO-DX", None, [untagged, _peering(3101)])]) == []

    def test_a_name_with_a_number_is_no_sub_interface_without_a_parent(self) -> None:
        """<port>.<vlan> in a name alone makes no sub-interface: it needs an encapsulation."""
        loose = _vport("eg-fr201", "Ethernet1/13.3101", (XC,), kind="DcimVirtualInterface")
        (message,) = _run(handoffs=[_handoff("HO-DX", None, [loose, _peering()])])
        assert "is neither a sub-interface" in message

    def test_the_near_end_still_needs_its_carrier(self) -> None:
        """The cloud far end is not required to be carried, but the near end on a physical device is."""
        bare = _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ()))
        (message,) = _run(handoffs=[_handoff("HO-DX", None, [bare, _peering()])])
        assert "has no live carrier circuit and no cable at eg-fr201:Ethernet1/13.3101" in message

    def test_the_handoff_locations_are_the_cage_and_the_transports_region(self) -> None:
        """The region is where the transport end sits: authoring another place is drift."""
        (message,) = _run(handoffs=[_handoff("HO-DX", [FR2, SAAS], [EG_FR_VLAN, _peering()])])
        assert "locations are exactly the facilities its two end devices are deployed in" in message

    def test_two_routed_physical_ports_pass(self) -> None:
        """A dedicated physical interface: an IP address, no VLAN, carried by a carrier circuit at the port itself."""
        ends = [_routed("eg-fr201", "Ethernet1/13", (XC,)), _routed("EXT-AWS-DX-FR2", "et-0/0/0", ())]
        assert _run(handoffs=[_handoff("HO-ROUTED", None, ends)]) == []

    def test_a_routed_port_and_a_cloud_peering_pass(self) -> None:
        """Our routed port to the peering of a dedicated Direct Connect (no VLAN on either end)."""
        ends = [_routed("eg-fr201", "Ethernet1/13", (XC,)), _peering(None)]
        assert _run(handoffs=[_handoff("HO-DX", None, ends)]) == []

    def test_a_routed_port_without_a_carrier_fails(self) -> None:
        """A routed port on a physical device is carried at the port: no circuit and no cable is reported."""
        ends = [_routed("eg-fr201", "Ethernet1/13", ()), _routed("EXT-AWS-DX-FR2", "et-0/0/0", ())]
        (message,) = _run(handoffs=[_handoff("HO-ROUTED", None, ends)])
        assert "has no live carrier circuit and no cable at eg-fr201:Ethernet1/13" in message

    def test_a_routed_port_carried_by_a_cable_passes(self) -> None:
        """Inside one facility the hop is a cable on the port itself."""
        ends = [_routed("eg-fr201", "Ethernet1/13", (), cabled=True), _routed("EXT-AWS-DX-FR2", "et-0/0/0", ())]
        assert _run(handoffs=[_handoff("HO-ROUTED", None, ends)]) == []

    def test_a_physical_port_with_no_address_and_no_vlan_fails(self) -> None:
        """Neither a sub-interface nor routed: the finding says what a handoff end can be."""
        bare = _vport("EXT-AWS-DX-FR2", "et-0/0/0", (XC,), kind="DcimPhysicalInterface")
        (message,) = _run(handoffs=[_handoff("HO-BARE", None, [_routed("eg-fr201", "Ethernet1/13", (XC,)), bare])])
        assert message == (
            "Handoff circuit 'HO-BARE' end 'EXT-AWS-DX-FR2:et-0/0/0' is neither a sub-interface (a "
            "virtual interface with an encapsulation) nor a routed physical port (an IP address, no VLAN) nor a "
            "cloud peering interface: a physical port with no IP address and no VLAN cannot carry a handoff."
        )

    def test_a_routed_port_and_a_sub_interface_fail(self) -> None:
        """One end routed and the other on a VLAN cannot form one link."""
        (message,) = _run(handoffs=[_handoff("HO-MIX", None, [_routed("eg-fr201", "Ethernet1/13", (XC,)), AWS_SUB])])
        assert "mixes a sub-interface and a routed port" in message

    def test_a_routed_port_and_a_cloud_peering_with_a_vlan_fail(self) -> None:
        """The peering states a VLAN the routed port cannot carry."""
        (message,) = _run(handoffs=[_handoff("HO-MIX", None, [_routed("eg-fr201", "Ethernet1/13", (XC,)), _peering()])])
        assert "ends carry different VLANs" in message and "eg-fr201:Ethernet1/13 = no VLAN" in message


def _fabric_end(device: str, name: str, location: dict[str, Any] | None) -> dict[str, Any]:
    """An end port of the provider's connect device: no deployment, its own location (a zone) places it."""
    place = {key: location[key] for key in ("id", "__typename", "name")} if location else None
    return {
        "id": f"id-{device}:{name}",
        "name": {"value": name},
        "device": {"node": {"id": f"id-{device}", "name": {"value": device}, "location": {"node": place}}},
    }


FABRIC_END = _fabric_end("EXT-FABRIC-FR", "et-0/0/0", FR2)


class TestCarrierOnAProviderConnect:
    """The provider's connect device has no deployment: its location is the place of its end."""

    def test_its_location_places_the_end(self) -> None:
        """The cage where the Fabric port sits and the cloud region are the two ends' places."""
        circuit = _carrier_circuit("EQX-FR-1", [FR2, EUC1], [FABRIC_END, DX_END])
        assert _run(carrier=[circuit]) == []

    def test_a_location_that_is_not_the_ports_fails(self) -> None:
        """The Paris cage is not the place of a Fabric port that sits in the Frankfurt cage."""
        circuit = _carrier_circuit("EQX-BAD", [PA4, EUC1], [FABRIC_END, DX_END])
        (message,) = _run(carrier=[circuit])
        assert "EQX-BAD" in message and "not a sibling zone" in message

    def test_a_port_with_no_location_is_reported_as_undeployed(self) -> None:
        """A port whose location is missing is placed nowhere: the finding names the port."""
        nowhere = _fabric_end("EXT-FABRIC-X", "et-0/0/0", None)
        circuit = _carrier_circuit("EQX-NOWHERE", [FR2, EUC1], [nowhere, DX_END])
        (message,) = _run(carrier=[circuit])
        assert "end 'EXT-FABRIC-X:et-0/0/0' is on a device with no deployment" in message
