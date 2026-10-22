"""Unit tests for CheckCircuitPlacement (where physical and virtual circuits terminate).

The payloads are raw GraphQL responses shaped like queries/validation/circuit_placement.gql; ``validate`` runs
them through ``clean_data``. The places mirror data/demos/30_all: DC10/DC11 with their border leaves, the
Equinix FR metro with cages FR2/FR6 (the edges eg-fr201/eg-fr202 are deployed to the ZONE FR2, not the metro),
the Equinix AM metro with its virtual colocation AM-NE, AWS eu-central-1, the ISP Orange, C001's office
(owner of its access circuit INET-C001-OFFICE).

A virtual circuit's underlay is derived, never listed: the physical circuits found in the ``capabilities`` of its own
ports. ``_riding`` puts them there, nested under the port as the query selects them. A physical_backed circuit's
derived circuits must form one contiguous path between its two locations, an internet_backed one's from one end to the
internet; a provider_virtual_only (or unset) one has no physical rule. A Network Edge virtual device (AM-NE) sits on
no physical circuit, so its virtual circuits are provider_virtual_only.

The ports mirror the demo data: DF-DC10-EQXFR2 ends on bl-dc101101 Ethernet1/35 and eg-fr201 Ethernet1/10, while
eg-fr201 Ethernet1/13 carries only the cross-connect XC-EQXFR2-AWS-EUC1.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.circuit_placement import CheckCircuitPlacement
from utils.circuit_underlay import TUNNEL_LINK_TYPES


def _place(
    name: str,
    kind: str,
    parent: dict[str, Any] | None = None,
    owner: str | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """A location or deployment node, as the query selects it (``environment``: a DC or colocation footprint's)."""
    node: dict[str, Any] = {"id": f"id-{name}", "__typename": kind, "name": {"value": name}}
    if environment is not None:
        node["environment"] = {"value": environment}
    if parent is not None:
        node["parent"] = {"node": {key: parent[key] for key in ("id", "__typename", "name")}}
    if owner is not None:
        node["owner"] = {"node": {"id": f"id-{owner}", "org_id": {"value": owner}}}
    return node


DC10 = _place("DC10", "TopologyDataCenter")
DC11 = _place("DC11", "TopologyDataCenter")
FR = _place("FR", "TopologyColocationMetro")
PA = _place("PA", "TopologyColocationMetro")
AM = _place("AM", "TopologyColocationMetro")
FR2 = _place("FR2", "TopologyColocationZone", FR)
FR6 = _place("FR6", "TopologyColocationZone", FR)
PA4 = _place("PA4", "TopologyColocationZone", PA)
AM_NE = _place("AM-NE", "TopologyVirtualColocation", AM)
EUC1 = _place("eu-central-1", "TopologyCloudRegion")
ORANGE = _place("ISP-ORANGE", "TopologyExternalProviders")
C005_DC10 = _place("C005-P-DC10", "TopologyCustomerDC", DC10, owner="C005", environment="p")
C006_DC11 = _place("C006-P-DC11", "TopologyCustomerDC", DC11, owner="C006", environment="p")
C001_FR = _place("C001-P-FR", "TopologyCustomerColocation", FR, owner="C001", environment="p")
C001_AM = _place("C001-P-AM", "TopologyCustomerColocation", AM, owner="C001", environment="p")
C006_DEV_FR = _place("C006-D-FR", "TopologyCustomerColocation", FR, owner="C006", environment="d")
C007_TEST_FR = _place("C007-T-FR", "TopologyCustomerColocation", FR, owner="C007", environment="t")
C006_DEV_PA = _place("C006-D-PA", "TopologyCustomerColocation", PA, owner="C006", environment="d")
C008_CLOUD = _place("C008-CLOUD", "TopologyCustomerCloud", EUC1, owner="C008")  # no environment attribute
C001_OFFICE = _place("C001-OFFICE", "TopologyCustomerOffice", owner="C001")  # no environment: always PROD


def _port(device: str, name: str, deployment: dict[str, Any] | None) -> dict[str, Any]:
    """An end port on its device, with the device's deployment."""
    return {
        "id": f"id-{device}:{name}",
        "name": {"value": name},
        "device": {"node": {"id": f"id-{device}", "name": {"value": device}, "deployment": {"node": deployment}}},
    }


BL10 = _port("bl-dc101101", "Ethernet1/35", DC10)
BL11 = _port("bl-dc1111101", "Ethernet1/35", DC11)
EG_FR = _port("eg-fr201", "Ethernet1/13", FR2)  # the cross-connect's port
EG_FR_DF10 = _port("eg-fr201", "Ethernet1/10", FR2)  # the dark fibre to DC10 ends on another port of the edge
EG_FR_DF11 = _port("eg-fr201", "Ethernet1/11", FR2)
EG_FR_METRO = _port("eg-fr01", "Ethernet1/13", FR)  # legacy: no device is deployed to a metro any more
EG_PA = _port("eg-pa401", "Ethernet1/13", PA4)
EG_AM_NE = _port("eg-am-ne01", "Ethernet1/13", AM_NE)  # a virtual edge deployed to the virtual colocation
AWS = _port("EXT-AWS-DX-FR2", "et-0/0/0", EUC1)
GW = _port("EQX-FR2-SDWAN-GW1", "eth0", FR2)
OFFICE_EDGE = _port("C001-OFFICE-EDGE1", "GE1", C001_OFFICE)
ORANGE_PE = _port("EXT-ORANGE-1", "ge-0/0/1", ORANGE)


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _owner(owner: str | None) -> dict[str, Any]:
    """A circuit's owner edge, as the query selects it (empty when unowned)."""
    return {"node": {"id": f"id-{owner}", "org_id": {"value": owner}} if owner else None}


def _physical(
    name: str, locations: list[dict[str, Any]], ends: list[dict[str, Any]], owner: str | None = None
) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "owner": _owner(owner),
        "locations": _edges(locations),
        "capabilities": _edges(ends),
    }


DF10 = _physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10, EG_FR_DF10])
DF11 = _physical("DF-DC11-EQXFR2", [DC11, FR2], [BL11, EG_FR_DF11])
XC = _physical("XC-EQXFR2-AWS-EUC1", [FR2, EUC1], [EG_FR, AWS])
XC_AM_NE = _physical("XC-EQXAMNE-AWS-EUC1", [AM_NE, EUC1], [EG_AM_NE, AWS])
XC_FR6 = _physical("XC-EQXFR6-AWS-EUC1", [FR6, EUC1], [EG_FR, AWS])
XC_PA4 = _physical("XC-EQXPA4-AWS-EUC1", [PA4, EUC1], [EG_PA, AWS])
INET_OFFICE = _physical("INET-C001-OFFICE", [C001_OFFICE, ORANGE], [OFFICE_EDGE, ORANGE_PE], owner="C001")


def _riding(port: dict[str, Any], *circuits: dict[str, Any]) -> dict[str, Any]:
    """A virtual circuit's port with the circuits terminating on it, nested in the port's ``capabilities`` as
    queries/validation/circuit_placement.gql selects them: a physical circuit with its locations."""
    hops = [
        {
            "__typename": "TopologyPhysicalCircuit",
            "id": hop["id"],
            "name": hop["name"],
            "status": hop.get("status", {"value": "active"}),
            "locations": hop["locations"],
        }
        for hop in circuits
    ]
    return {**port, "capabilities": _edges(hops)}


def _decommissioned(circuit: dict[str, Any]) -> dict[str, Any]:
    """The physical circuit with its status set to decommissioned (as ``status { value }`` selects it)."""
    return {**circuit, "status": {"value": "decommissioned"}}


# The ports of the fixtures' physical circuits, riding them: a border leaf on its dark fibre, the cage edge's
# cross-connect port on the cross-connect to the region (one cable per port).
BL10_RIDING = _riding(BL10, DF10)
BL11_RIDING = _riding(BL11, DF11)
EG_FR_RIDING = _riding(EG_FR, XC)
OFFICE_EDGE_RIDING = _riding(OFFICE_EDGE, INET_OFFICE)


def _virtual(
    name: str,
    locations: list[dict[str, Any]],
    ends: list[dict[str, Any]],
    owner: str | None = None,
    transport_mode: str | None = "physical_backed",
    link_type: str = "direct_connect_aws",
) -> dict[str, Any]:
    """A virtual circuit; ``ends`` are its own ports, each optionally ``_riding`` physical circuits."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "link_type": {"value": link_type},
        "transport_mode": {"value": transport_mode},
        "owner": _owner(owner),
        "locations": _edges(locations),
        "capabilities": _edges(ends),
    }


def _run(physical: list[dict[str, Any]] | None = None, virtual: list[dict[str, Any]] | None = None) -> list[str]:
    """Validate the circuits and return the logged error messages."""
    check = cast(Any, CheckCircuitPlacement.__new__(CheckCircuitPlacement))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate({"TopologyPhysicalCircuit": _edges(physical or []), "TopologyVirtualCircuit": _edges(virtual or [])})
    for finding in findings:
        assert finding["object_id"] and finding["object_type"] in {"TopologyPhysicalCircuit", "TopologyVirtualCircuit"}
    return [finding["message"] for finding in findings]


class TestPhysicalCircuitPlacement:
    @pytest.mark.parametrize(
        "circuit",
        [
            DF10,
            XC,
            INET_OFFICE,
            # A device racked in the cage itself places the cage end too.
            _physical("XC-GW-AWS", [FR2, EUC1], [GW, AWS]),
            # A virtual colocation is a circuit end like a zone. Synthetic: no demo physical circuit ends on
            # AM-NE (eg-am-ne01 is a virtual edge); XC_AM_NE is the fixture, a cross-connect from AM-NE to AWS.
            XC_AM_NE,
        ],
        ids=[
            "dc-to-cage",
            "cage-to-region",
            "office-to-isp",
            "device-in-the-cage",
            "virtual-colocation-to-region",
        ],
    )
    def test_placed_circuit_passes(self, circuit: dict[str, Any]) -> None:
        """Each location is the deployment of one end's device: no finding."""
        assert _run(physical=[circuit]) == []

    @pytest.mark.parametrize("locations", [[FR2], []], ids=["only-the-authored-cage", "none"])
    def test_not_two_locations_fails(self, locations: list[dict[str, Any]]) -> None:
        """A physical circuit with fewer than two distinct locations (a derived end missing) is an error."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", locations, [BL10, EG_FR])])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXFR2' terminates on" in messages[0]
        assert "exactly 2 distinct locations" in messages[0]

    def test_end_device_without_deployment_fails(self) -> None:
        """An end on a device with no deployment cannot place its end: error naming the port."""
        undeployed = _port("EXT-AWS-DX-FR2", "et-0/0/0", None)
        messages = _run(physical=[_physical("XC-EQXFR2-AWS-EUC1", [FR2, EUC1], [EG_FR, undeployed])])
        assert len(messages) == 1
        assert "end 'EXT-AWS-DX-FR2:et-0/0/0' is on a device with no deployment" in messages[0]

    def test_dc_end_other_than_the_device_deployment_fails(self) -> None:
        """Authored DC10 while the border leaf sits in DC11: the DC end is the device's deployment."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL11, EG_FR])])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXFR2' terminates on" in messages[0]
        assert "bl-dc1111101:Ethernet1/35 in TopologyDataCenter 'DC11'" in messages[0]

    def test_cage_of_another_metro_fails(self) -> None:
        """A colocation end must be the edge's zone itself: PA4 with an edge in FR2 fails."""
        messages = _run(physical=[_physical("DF-DC10-EQXPA4", [DC10, PA4], [BL10, EG_FR])])
        assert len(messages) == 1
        assert "not a sibling zone of its metro" in messages[0]

    def test_virtual_colocation_of_another_metro_fails(self) -> None:
        """AM-NE is not in FR: an edge in zone FR2 does not place it."""
        messages = _run(physical=[_physical("DF-DC10-EQXAMNE", [DC10, AM_NE], [BL10, EG_FR])])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXAMNE' terminates on" in messages[0]

    def test_sibling_cage_of_the_same_metro_fails(self) -> None:
        """Authored FR6 while the edge sits in FR2: add_circuit (plan_locations) refuses it as contradicting the
        endpoint devices' deployment, so the check does too."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR6", [DC10, FR6], [BL10, EG_FR])])
        assert len(messages) == 1
        assert "eg-fr201:Ethernet1/13 in TopologyColocationZone 'FR2'" in messages[0]
        assert "TopologyColocationZone 'FR6'" in messages[0]

    def test_sibling_virtual_colocation_of_the_same_metro_fails(self) -> None:
        """A second presence of the metro is no more the end than a sibling cage is."""
        sibling = _place("AM-NE2", "TopologyVirtualColocation", AM)
        messages = _run(physical=[_physical("DF-DC10-EQXAMNE2", [DC10, sibling], [BL10, EG_AM_NE])])
        assert len(messages) == 1

    def test_device_deployed_to_the_metro_fails(self) -> None:
        """No device is deployed to a metro any more: a legacy metro deployment places no zone of it."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2-METRO", [DC10, FR2], [BL10, EG_FR_METRO])])
        assert len(messages) == 1
        assert "eg-fr01:Ethernet1/13 in TopologyColocationMetro 'FR'" in messages[0]

    def test_two_zones_of_different_metros_with_edges_in_one_fail(self) -> None:
        """FR2 and PA4 with both edges in FR2: one location has no end."""
        messages = _run(physical=[_physical("XC-FR2-PA4", [FR2, PA4], [EG_FR, _port("eg-fr202", "e1", FR2)])])
        assert len(messages) == 1

    def test_error_message_names_the_zone_end_and_the_ends_deployment(self) -> None:
        """The finding shows where the device really is, so the author can fix the location or the deployment."""
        messages = _run(physical=[_physical("DF-DC10-EQXPA4", [DC10, PA4], [BL10, EG_FR])])
        assert "eg-fr201:Ethernet1/13 in TopologyColocationZone 'FR2'" in messages[0]
        assert "TopologyColocationZone 'PA4'" in messages[0]

    def test_ends_must_match_locations_one_to_one(self) -> None:
        """Both ends in DC10 leave the FR2 location without an end."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10, _port("bl-dc101102", "e1", DC10)])])
        assert len(messages) == 1

    def test_end_count_is_left_to_physical_circuit_ports(self) -> None:
        """A circuit with one end gets no placement finding: checks/physical_circuit_ports.py reports it."""
        assert _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10])]) == []


class TestPhysicalCircuitOwnership:
    """Owned exactly when it is an office's access circuit (add_sdwan_edge's INET-<office>), then by the office's
    owner; the rest is shared underlay."""

    def test_office_access_circuit_owned_by_the_office_owner_passes(self) -> None:
        """INET-C001-OFFICE ends on C001's office and is owned by C001: no finding."""
        assert _run(physical=[INET_OFFICE]) == []

    @pytest.mark.parametrize(
        ("owner", "ownership"), [(None, "unowned"), ("C005", "owned by 'C005'")], ids=["unowned", "other-customer"]
    )
    def test_office_access_circuit_not_owned_by_the_office_owner_fails(self, owner: str | None, ownership: str) -> None:
        circuit = _physical("INET-C001-OFFICE", [C001_OFFICE, ORANGE], [OFFICE_EDGE, ORANGE_PE], owner=owner)
        messages = _run(physical=[circuit])
        assert messages == [
            f"Physical circuit 'INET-C001-OFFICE' ends on TopologyCustomerOffice 'C001-OFFICE' of 'C001' but is "
            f"{ownership}: an office's access circuit is owned by the office's owner."
        ]

    def test_owned_shared_underlay_fails(self) -> None:
        """A dark fibre carries every tenant: owned, it would pin the shared underlay on one customer."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10, EG_FR], owner="C005")])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXFR2' is owned by 'C005' but ends on no office" in messages[0]

    def test_ownership_is_checked_with_a_location_missing(self) -> None:
        """The owner rule does not wait for the derived end: one location is enough to see the office."""
        circuit = _physical("INET-C001-OFFICE", [C001_OFFICE], [OFFICE_EDGE, ORANGE_PE])
        messages = _run(physical=[circuit])
        assert len(messages) == 2
        assert "an office's access circuit is owned by the office's owner" in messages[0]
        assert "terminates on 1 location(s)" in messages[1]


class TestVirtualCircuitTermination:
    """Where a virtual circuit ends and who owns it. The underlay rules are TestVirtualCircuitUnderlay's."""

    def test_not_two_locations_fails(self) -> None:
        """A virtual circuit has exactly two distinct locations."""
        messages = _run(virtual=[_virtual("VC-X", [C005_DC10], [BL10_RIDING, EG_FR_RIDING], owner="C005")])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-X' terminates on 1 location(s)" in messages[0]

    @pytest.mark.parametrize(
        "circuit",
        [
            _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING], owner="C005"),
            # A colocation footprint is parented by the metro: the FR edge places it.
            # Single-ended on purpose: provider-managed, its cloud end is a CloudVirtualInterface, not a port.
            _virtual("VC-C001-EQXFR-AWS-EUC1", [C001_FR, EUC1], [EG_FR_RIDING], owner="C001"),
            # The same footprint with a chain starting at a SIBLING zone of the metro (FR6, not FR2).
            _virtual("VC-C001-EQXFR6-AWS-EUC1", [C001_FR, EUC1], [_riding(EG_FR, XC_FR6)], owner="C001"),
            # A colocation footprint of the AM metro: its virtual colocation AM-NE (not a cage) places it. A
            # Network Edge virtual device sits on no physical circuit, so the circuit is provider_virtual_only.
            _virtual(
                "VC-C001-EQXAM-AWS-EUC1",
                [C001_AM, EUC1],
                [EG_AM_NE],
                owner="C001",
                transport_mode="provider_virtual_only",
            ),
            # A virtual colocation is a provider far end of an owned circuit, like a cloud region.
            _virtual(
                "VC-C005-DC10-AMNE",
                [C005_DC10, AM_NE],
                [BL10, EG_AM_NE],
                owner="C005",
                transport_mode="provider_virtual_only",
            ),
            # Far end another footprint (of the same owner here).
            _virtual(
                "VC-C005-DC10-FR",
                [C005_DC10, _place("C005-P-FR", "TopologyCustomerColocation", FR, "C005")],
                [BL10_RIDING, EG_FR],
                owner="C005",
            ),
        ],
        ids=[
            "dc-footprint-to-region",
            "colo-footprint-via-metro",
            "colo-footprint-chain-on-sibling-zone",
            "colo-footprint-via-virtual-colocation",
            "footprint-to-virtual-colocation",
            "footprint-to-footprint",
        ],
    )
    def test_owned_circuit_passes(self, circuit: dict[str, Any]) -> None:
        """Tenant end a footprint of the owner with a device in its facility, far end a provider place."""
        assert _run(virtual=[circuit]) == []

    def test_owned_without_own_footprint_fails(self) -> None:
        """Owned by C006 while the only footprint is C005's: the tenant end is a footprint of the owner."""
        circuit = _virtual("VC-C006", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING], owner="C006")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-C006' is owned by 'C006' but none of its locations" in messages[0]

    def test_tenant_facility_without_end_device_fails(self) -> None:
        """C005-P-DC10 while the circuit ends on DC11's border leaf: no end device is in DC10."""
        circuit = _virtual(
            "VC-C005", [C005_DC10, EUC1], [BL11, EG_PA], owner="C005", transport_mode="provider_virtual_only"
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "tenant end TopologyCustomerDC 'C005-P-DC10' is in TopologyDataCenter 'DC10'" in messages[0]

    def test_owned_far_end_not_a_provider_facility_fails(self) -> None:
        """An owned circuit runs to a provider facility or a footprint, not a bare DC."""
        circuit = _virtual(
            "VC-C005-DC11", [C005_DC10, DC11], [BL10, BL11], owner="C005", transport_mode="provider_virtual_only"
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "far end is a provider facility" in messages[0]

    def test_colo_footprint_chain_starting_in_another_metro_fails(self) -> None:
        """C001-P-FR is in metro FR: a chain that starts at AM-NE (a VC of metro AM) is not its underlay."""
        circuit = _virtual("VC-C001-EQXFR-AWS-EUC1", [C001_FR, EUC1], [_riding(EG_FR, XC_AM_NE)], owner="C001")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is not one contiguous path from TopologyColocationMetro 'FR'" in messages[0]

    def test_unowned_between_facilities_passes(self) -> None:
        """A platform overlay (no owner) between two facilities."""
        assert _run(virtual=[_virtual("VC-SDWAN-FR2-AWS-EUC1", [FR2, EUC1], [GW, EG_FR_RIDING])]) == []

    def test_unowned_on_a_virtual_colocation_passes(self) -> None:
        """A virtual colocation is a facility: a platform overlay may end on AM-NE."""
        circuit = _virtual("VC-SDWAN-AMNE-AWS-EUC1", [AM_NE, EUC1], [EG_AM_NE], transport_mode="provider_virtual_only")
        assert _run(virtual=[circuit]) == []

    def test_unowned_on_a_footprint_fails(self) -> None:
        """No owner but a customer footprint end: a platform overlay terminates on facilities."""
        messages = _run(virtual=[_virtual("VC-PLATFORM", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING])])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-PLATFORM' has no owner but terminates on TopologyCustomerDC" in messages[0]


class TestVirtualCircuitUnderlay:
    """The underlay is the physical circuits of the circuit's own ports, each once, in one contiguous path."""

    def test_physical_backed_end_to_end_passes(self) -> None:
        """The dark fibre from DC10 and the cross-connect to the region join at FR2."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING], owner="C005")
        assert _run(virtual=[circuit]) == []

    def test_hops_in_any_order_pass(self) -> None:
        """Relationship peers carry no order: the reversed hop list (cross-connect first) is still one path."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [EG_FR_RIDING, BL10_RIDING], owner="C005")
        assert _run(virtual=[circuit]) == []

    def test_locations_in_either_order_pass(self) -> None:
        """The path may run from either location to the other."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [EUC1, C005_DC10], [BL10_RIDING, EG_FR_RIDING], owner="C005")
        assert _run(virtual=[circuit]) == []

    def test_chain_not_starting_at_the_tenant_facility_fails(self) -> None:
        """C005-P-DC10 with a border leaf in DC10 but riding DC11's fibre: the chain does not start at DC10."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, _riding(EG_FR, DF11, XC)], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-C005-DC10-AWS-EUC1' underlay" in messages[0]
        assert "is not one contiguous path from TopologyDataCenter 'DC10' to TopologyCloudRegion" in messages[0]

    def test_chain_not_reaching_the_far_end_fails(self) -> None:
        """The dark fibre alone stops at FR2, short of eu-central-1."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10_RIDING, EG_FR], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "underlay DF-DC10-EQXFR2 (DC10 - FR2) is not one contiguous path" in messages[0]

    def test_circuit_touching_neither_location_fails(self) -> None:
        """DF-DC11-EQXFR2 (DC11, FR2) under a circuit between DC10 and eu-central-1 is no path between them."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, _riding(EG_FR, DF11)], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "underlay DF-DC11-EQXFR2 (DC11 - FR2) is not one contiguous path" in messages[0]
        assert "TopologyCustomerDC" not in messages[0]
        assert "from TopologyDataCenter 'DC10' to TopologyCloudRegion 'eu-central-1'" in messages[0]

    def test_extra_derived_circuit_breaks_the_path(self) -> None:
        """Every derived circuit belongs to the one path: a stray one (DC11's fibre) makes it not contiguous."""
        circuit = _virtual(
            "VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING, BL11_RIDING], owner="C005"
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is not one contiguous path" in messages[0]

    def test_colo_footprint_chain_on_a_zone_of_another_metro_fails(self) -> None:
        """A cross-connect of PA4 (metro PA) is not within the FR metro C001-P-FR stands for."""
        circuit = _virtual("VC-C001-EQXFR-AWS-EUC1", [C001_FR, EUC1], [_riding(EG_FR, XC_PA4)], owner="C001")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "underlay XC-EQXPA4-AWS-EUC1 (PA4 - eu-central-1) is not one contiguous path" in messages[0]

    def test_physical_backed_without_underlay_fails(self) -> None:
        """physical_backed with ports on no physical circuit."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, EG_FR], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is physical_backed but its ports are on no physical circuit" in messages[0]
        assert "TopologyDataCenter 'DC10'" in messages[0]
        assert "TopologyCloudRegion 'eu-central-1'" in messages[0]

    def test_physical_backed_on_a_network_edge_port_fails(self) -> None:
        """A Network Edge virtual device sits on no physical circuit: its only port derives nothing."""
        circuit = _virtual("VC-C001-EQXAM-AWS-EUC1", [C001_AM, EUC1], [EG_AM_NE], owner="C001")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert (
            "Virtual circuit 'VC-C001-EQXAM-AWS-EUC1' is physical_backed but its ports are on no physical circuit"
            in (messages[0])
        )

    def test_a_port_capability_that_is_no_physical_circuit_derives_nothing(self) -> None:
        """Other capabilities on a port (a virtual circuit, a service) are no underlay."""
        port = {**BL10, "capabilities": _edges([{"__typename": "TopologyVirtualCircuit", "id": "id-VC-OTHER"}])}
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [port, EG_FR], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "its ports are on no physical circuit" in messages[0]

    @pytest.mark.parametrize(
        "ends",
        [[BL10, EG_FR], [BL10_RIDING, EG_FR_RIDING], [BL10, _riding(EG_FR, DF11)]],
        ids=["no-underlay", "with-underlay", "circuits-touching-neither-location"],
    )
    @pytest.mark.parametrize("transport_mode", ["provider_virtual_only", None])
    def test_no_physical_rule_without_a_physical_transport_mode(
        self, ends: list[dict[str, Any]], transport_mode: str | None
    ) -> None:
        """provider_virtual_only and unset pass with no derived circuit, a contiguous one, or one touching neither
        location: an Equinix Network Edge has no physical circuit, so there is nothing to hold it to."""
        circuit = _virtual(
            "VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], ends, owner="C005", transport_mode=transport_mode
        )
        assert _run(virtual=[circuit]) == []

    def test_internet_backed_reaching_the_internet_passes(self) -> None:
        """An internet-backed overlay's derived circuit ends on the internet, not on the far end."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE_RIDING, GW],
            owner="C001",
            transport_mode="internet_backed",
        )
        assert _run(virtual=[circuit]) == []

    def test_internet_backed_not_reaching_the_internet_fails(self) -> None:
        """A derived dark fibre instead of an internet access circuit."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE, _riding(GW, DF10)],
            owner="C001",
            transport_mode="internet_backed",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "underlay DF-DC10-EQXFR2 (DC10 - FR2) is not one contiguous path" in messages[0]
        assert "to the internet (a TopologyExternalProviders)" in messages[0]

    def test_internet_backed_with_a_stray_derived_circuit_fails(self) -> None:
        """The internet access circuit is a path, but a second derived circuit (the dark fibre) is not part of it."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE_RIDING, _riding(GW, DF10)],
            owner="C001",
            transport_mode="internet_backed",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is not one contiguous path" in messages[0]

    def test_internet_backed_without_underlay_fails(self) -> None:
        """internet_backed with ports on no physical circuit: like physical_backed, it names the expected path."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE, GW],
            owner="C001",
            transport_mode="internet_backed",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is internet_backed but its ports are on no physical circuit" in messages[0]
        assert "to the internet (a TopologyExternalProviders)" in messages[0]
        assert "TopologyCustomerOffice 'C001-OFFICE'" in messages[0]

    def test_two_ports_on_the_same_physical_circuit_are_one_hop(self) -> None:
        """The dark fibre under both ports is the underlay once: the message lists it once."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [_riding(OFFICE_EDGE, DF10), _riding(GW, DF10)],
            owner="C001",
            transport_mode="internet_backed",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert messages[0].count("DF-DC10-EQXFR2") == 1

    def test_a_decommissioned_circuit_next_to_a_good_chain_is_ignored(self) -> None:
        """A decommissioned extra circuit (DC11's fibre) does not break the contiguity of the live chain."""
        circuit = _virtual(
            "VC-C005-DC10-AWS-EUC1",
            [C005_DC10, EUC1],
            [BL10_RIDING, EG_FR_RIDING, _riding(BL11, _decommissioned(DF11))],
            owner="C005",
        )
        assert _run(virtual=[circuit]) == []

    def test_a_decommissioned_circuit_does_not_complete_the_chain(self) -> None:
        """The cross-connect to the region is decommissioned: the live fibre stops at FR2, short of eu-central-1."""
        circuit = _virtual(
            "VC-C005-DC10-AWS-EUC1",
            [C005_DC10, EUC1],
            [BL10_RIDING, _riding(EG_FR, _decommissioned(XC))],
            owner="C005",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "underlay DF-DC10-EQXFR2 (DC10 - FR2) is not one contiguous path" in messages[0]
        assert "XC-EQXFR2-AWS-EUC1" not in messages[0]

    def test_physical_backed_on_decommissioned_circuits_only_fails(self) -> None:
        """Only decommissioned circuits under the ports: the ports are on no (live) physical circuit."""
        circuit = _virtual(
            "VC-C005-DC10-AWS-EUC1",
            [C005_DC10, EUC1],
            [_riding(BL10, _decommissioned(DF10)), _riding(EG_FR, _decommissioned(XC))],
            owner="C005",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is physical_backed but its ports are on no physical circuit" in messages[0]

    def test_internet_backed_on_a_decommissioned_circuit_only_fails(self) -> None:
        """The office's access circuit is decommissioned: the internet_backed tunnel has no live underlay."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [_riding(OFFICE_EDGE, _decommissioned(INET_OFFICE)), GW],
            owner="C001",
            transport_mode="internet_backed",
            link_type="vpn_ipsec",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is internet_backed but its ports are on no physical circuit" in messages[0]


class TestTunnelTransportMode:
    """A tunnel link type is internet_backed or physical_backed: provider_virtual_only and unset escape every
    physical rule."""

    @pytest.mark.parametrize("link_type", sorted(TUNNEL_LINK_TYPES))
    @pytest.mark.parametrize("transport_mode", ["provider_virtual_only", None])
    def test_tunnel_without_a_physical_transport_mode_fails(self, link_type: str, transport_mode: str | None) -> None:
        circuit = _virtual(
            "VC-TUNNEL", [FR2, EUC1], [GW, EG_FR_RIDING], transport_mode=transport_mode, link_type=link_type
        )
        messages = _run(virtual=[circuit])
        assert messages == [
            f"Virtual circuit 'VC-TUNNEL' is a tunnel (link_type '{link_type}') with transport_mode "
            f"'{transport_mode or 'unset'}': a tunnel runs over the internet or a physical circuit, set its "
            "transport_mode to one of internet_backed, physical_backed."
        ]

    @pytest.mark.parametrize(
        "circuit",
        [
            _virtual("VC-TUNNEL", [FR2, EUC1], [GW, EG_FR_RIDING], link_type="gre"),
            _virtual(
                "VC-TUNNEL",
                [C001_OFFICE, FR2],
                [OFFICE_EDGE_RIDING, GW],
                transport_mode="internet_backed",
                link_type="gre",
            ),
        ],
        ids=["physical_backed", "internet_backed"],
    )
    def test_tunnel_with_a_physical_transport_mode_passes(self, circuit: dict[str, Any]) -> None:
        assert _run(virtual=[circuit]) == []

    @pytest.mark.parametrize("transport_mode", ["provider_virtual_only", None])
    def test_non_tunnel_link_type_needs_no_physical_transport_mode(self, transport_mode: str | None) -> None:
        circuit = _virtual(
            "VC-FABRIC", [FR2, EUC1], [GW, EG_FR], transport_mode=transport_mode, link_type="equinix_fabric"
        )
        assert _run(virtual=[circuit]) == []


class TestDemoVirtualCircuits:
    """The virtual circuits of data/demos/30_all, with the ports and physical circuits they derive from."""

    @pytest.mark.parametrize(
        "circuit",
        [
            # DF-DC10-EQXFR2 from the border leaf plus the cross-connect from the cage edge.
            _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING], owner="C005"),
            # Provider-managed, single-ended: the cage edge's cross-connect only.
            _virtual("VC-C001-EQXFR-AWS-EUC1", [C001_FR, EUC1], [EG_FR_RIDING], owner="C001"),
            _virtual("VC-SDWAN-FR2-AWS-EUC1", [FR2, EUC1], [GW, EG_FR_RIDING]),
            # The office's tunnel rides the internet, INET-C001-OFFICE up to the ISP.
            _virtual(
                "C001-PARTNER-ACME-FR2",
                [C001_OFFICE, FR2],
                [OFFICE_EDGE_RIDING, GW],
                owner="C001",
                transport_mode="internet_backed",
                link_type="vpn_ipsec",
            ),
            # The SD-WAN office tunnel add_sdwan_edge creates: sd_wan over the internet.
            _virtual(
                "C001-OFFICE-SDWAN-FR2",
                [C001_OFFICE, FR2],
                [OFFICE_EDGE_RIDING, GW],
                owner="C001",
                transport_mode="internet_backed",
                link_type="sd_wan",
            ),
        ],
        ids=[
            "dc-footprint-dark-fibre-and-cross-connect",
            "colo-footprint-cross-connect",
            "unowned",
            "office-access",
            "office-sdwan",
        ],
    )
    def test_demo_circuit_passes(self, circuit: dict[str, Any]) -> None:
        """Each demo virtual circuit's derived underlay is one contiguous path: no finding."""
        assert _run(virtual=[circuit]) == []


class TestEnvironmentSeparation:
    """Virtual circuits with a port on the same device are of one environment class: prod never meets non-prod."""

    PROD_DC10 = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10_RIDING, EG_FR_RIDING], owner="C005")
    PROD_DC11 = _virtual("VC-C006-DC11-AWS-EUC1", [C006_DC11, EUC1], [BL11_RIDING, EG_FR_RIDING], owner="C006")
    PROD_FR = _virtual("VC-C001-EQXFR-AWS-EUC1", [C001_FR, EUC1], [EG_FR_RIDING], owner="C001")
    DEV_FR = _virtual("VC-C006-D-EQXFR-AWS-EUC1", [C006_DEV_FR, EUC1], [EG_FR_RIDING], owner="C006")
    TEST_FR = _virtual("VC-C007-T-EQXFR-AWS-EUC1", [C007_TEST_FR, EUC1], [EG_FR_RIDING], owner="C007")
    UNOWNED = _virtual("VC-SDWAN-FR2-AWS-EUC1", [FR2, EUC1], [GW, EG_FR_RIDING])
    CLOUD = _virtual("VC-C008-CLOUD-FR2", [C008_CLOUD, FR2], [AWS, EG_FR_RIDING], owner="C008")
    OFFICE = _virtual(
        "C001-PARTNER-ACME-FR2",
        [C001_OFFICE, FR2],
        [OFFICE_EDGE_RIDING, GW],
        owner="C001",
        transport_mode="internet_backed",
        link_type="vpn_ipsec",
    )
    DEV_ON_GW = _virtual("VC-C006-D-GW-AWS-EUC1", [C006_DEV_FR, EUC1], [GW, EG_FR_RIDING], owner="C006")
    PROD_ON_GW = _virtual("VC-C001-GW-AWS-EUC1", [C001_FR, EUC1], [GW, EG_FR_RIDING], owner="C001")
    DEV_PA = _virtual("VC-C006-D-EQXPA-AWS-EUC1", [C006_DEV_PA, EUC1], [_riding(EG_PA, XC_PA4)], owner="C006")

    def test_tenants_of_one_environment_sharing_an_on_ramp_pass(self) -> None:
        """Three prod tenants and the unowned overlay all sit on eg-fr201 Ethernet1/13: owners need not match."""
        assert _run(virtual=[self.PROD_DC10, self.PROD_DC11, self.PROD_FR, self.UNOWNED]) == []

    def test_two_non_prod_environments_on_one_device_pass(self) -> None:
        """Dev and test are both non-prod: the invariant separates prod from non-prod, not d from t."""
        assert _run(virtual=[self.DEV_FR, self.TEST_FR]) == []

    def test_prod_and_non_prod_on_one_device_fail_once(self) -> None:
        """One finding for the device, naming it and both circuits with their environments, whatever the order."""
        for circuits in ([self.PROD_FR, self.DEV_FR], [self.DEV_FR, self.PROD_FR]):
            messages = _run(virtual=circuits)
            assert len(messages) == 1
            assert "Device 'eg-fr201' carries virtual circuits of different environments" in messages[0]
            assert "'VC-C001-EQXFR-AWS-EUC1' (environment 'p', prod)" in messages[0]
            assert "'VC-C006-D-EQXFR-AWS-EUC1' (environment 'd', non_prod)" in messages[0]

    def test_conflict_among_many_circuits_is_one_finding_per_device(self) -> None:
        """Three prod circuits and one dev one on eg-fr201: one finding, not one per pair."""
        messages = _run(virtual=[self.PROD_DC10, self.PROD_DC11, self.PROD_FR, self.DEV_FR])
        env_messages = [message for message in messages if "different environments" in message]
        assert len(env_messages) == 1
        assert all(name in env_messages[0] for name in ("VC-C005-DC10-AWS-EUC1", "VC-C006-D-EQXFR-AWS-EUC1"))

    def test_each_conflicting_device_is_reported(self) -> None:
        """Two circuits sharing two devices (gateway and edge) are two findings, one per device."""
        messages = _run(virtual=[self.PROD_ON_GW, self.DEV_ON_GW])
        assert sorted(message.split("'")[1] for message in messages) == ["EQX-FR2-SDWAN-GW1", "eg-fr201"]

    def test_unowned_circuit_is_skipped(self) -> None:
        """A platform overlay has no environment: a dev tenant sharing its devices does not conflict."""
        assert _run(virtual=[self.UNOWNED, self.DEV_ON_GW]) == []

    def test_cloud_footprint_environment_is_unknown_and_skipped(self) -> None:
        """A cloud footprint has no environment: it neither conflicts nor is named in a real conflict."""
        assert _run(virtual=[self.CLOUD, self.PROD_FR, self.PROD_DC10]) == []
        assert _run(virtual=[self.CLOUD, self.DEV_FR, self.TEST_FR]) == []
        messages = _run(virtual=[self.CLOUD, self.PROD_FR, self.DEV_FR])
        assert len(messages) == 1
        assert "VC-C008-CLOUD-FR2" not in messages[0]

    def test_circuit_alone_on_its_devices_passes(self) -> None:
        """A dev circuit on its own edge (eg-pa401) does not meet the prod circuit on eg-fr201."""
        assert _run(virtual=[self.PROD_FR, self.DEV_PA]) == []

    def test_footprints_disagreeing_on_class_make_the_circuit_prod(self) -> None:
        """C006 has a dev and a prod footprint on one circuit: any prod footprint makes it prod, whatever the
        location order, so it conflicts with a dev circuit on its device and not with a prod one."""
        c006_prod_pa = _place("C006-P-PA", "TopologyCustomerColocation", PA, owner="C006", environment="p")
        for locations in ([C006_DEV_FR, c006_prod_pa], [c006_prod_pa, C006_DEV_FR]):
            mixed = _virtual(
                "VC-C006-MIXED", locations, [EG_FR_RIDING], owner="C006", transport_mode="provider_virtual_only"
            )
            assert _run(virtual=[mixed, self.PROD_FR]) == []
            messages = _run(virtual=[mixed, self.TEST_FR])
            assert len(messages) == 1
            assert "'VC-C006-MIXED' (environment 'p', prod)" in messages[0]
            assert "'VC-C007-T-EQXFR-AWS-EUC1' (environment 't', non_prod)" in messages[0]

    def test_footprints_of_two_non_prod_environments_take_the_lowest(self) -> None:
        """Dev and test footprints of one owner on one circuit: non-prod, represented by the lowest value."""
        c006_test = _place("C006-T-PA", "TopologyCustomerColocation", PA, owner="C006", environment="t")
        for locations in ([C006_DEV_FR, c006_test], [c006_test, C006_DEV_FR]):
            mixed = _virtual(
                "VC-C006-MIXED", locations, [EG_FR_RIDING], owner="C006", transport_mode="provider_virtual_only"
            )
            assert _run(virtual=[mixed, self.TEST_FR]) == []
            messages = _run(virtual=[mixed, self.PROD_FR])
            assert len(messages) == 1
            assert "'VC-C006-MIXED' (environment 'd', non_prod)" in messages[0]

    @pytest.mark.parametrize("environment", [None, {"value": None}], ids=["attribute-absent", "value-null"])
    def test_footprint_without_an_environment_is_skipped(self, environment: dict[str, Any] | None) -> None:
        """A DC or colocation footprint with no environment value states none: the circuit is skipped, it neither
        conflicts nor is named."""
        bare = {**_place("C009-X-FR", "TopologyCustomerColocation", FR, owner="C009"), "environment": environment}
        circuit = _virtual("VC-C009-NOENV", [bare, EUC1], [_riding(EG_FR, XC)], owner="C009")
        assert _run(virtual=[circuit, self.PROD_FR]) == []
        assert _run(virtual=[circuit, self.DEV_FR]) == []

    @pytest.mark.parametrize(
        ("other", "expected"),
        [("PROD_ON_GW", 0), ("DEV_ON_GW", 1)],
        ids=["office-meets-prod", "office-meets-non-prod"],
    )
    def test_office_counts_as_prod(self, other: str, expected: int) -> None:
        """An office has no environment attribute and is PROD: it meets a prod circuit, conflicts with a dev one."""
        messages = _run(virtual=[self.OFFICE, getattr(self, other)])
        assert len(messages) == expected
        if expected:
            assert "Device 'EQX-FR2-SDWAN-GW1'" in messages[0]
            assert "'C001-PARTNER-ACME-FR2' (environment 'p', prod)" in messages[0]
