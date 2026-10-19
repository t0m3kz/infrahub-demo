"""Unit tests for CheckCircuitPlacement (where physical and virtual circuits terminate).

The payloads are raw GraphQL responses shaped like queries/validation/circuit_placement.gql; ``validate`` runs
them through ``clean_data``. The places mirror data/demos/30_all: DC10/DC11 with their border leaves, the
Equinix FR metro (edges eg-fr01/eg-fr02 deployed to it) with cages FR2/FR6, AWS eu-central-1, the ISP Orange.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.circuit_placement import CheckCircuitPlacement


def _place(name: str, kind: str, parent: dict[str, Any] | None = None, owner: str | None = None) -> dict[str, Any]:
    """A location or deployment node, as the query selects it."""
    node: dict[str, Any] = {"id": f"id-{name}", "__typename": kind, "name": {"value": name}}
    if parent is not None:
        node["parent"] = {"node": {key: parent[key] for key in ("id", "__typename", "name")}}
    if owner is not None:
        node["owner"] = {"node": {"id": f"id-{owner}", "org_id": {"value": owner}}}
    return node


DC10 = _place("DC10", "TopologyDataCenter")
DC11 = _place("DC11", "TopologyDataCenter")
FR = _place("FR", "TopologyColocationMetro")
PA = _place("PA", "TopologyColocationMetro")
FR2 = _place("FR2", "TopologyColocationZone", FR)
FR6 = _place("FR6", "TopologyColocationZone", FR)
PA4 = _place("PA4", "TopologyColocationZone", PA)
EUC1 = _place("eu-central-1", "TopologyCloudRegion")
ORANGE = _place("ISP-ORANGE", "TopologyExternalProviders")
C005_DC10 = _place("C005-P-DC10", "TopologyCustomerDC", DC10, owner="C005")
C001_FR = _place("C001-P-FR", "TopologyCustomerColocation", FR, owner="C001")
C001_OFFICE = _place("C001-OFFICE", "TopologyCustomerOffice", owner="C001")


def _port(device: str, name: str, deployment: dict[str, Any] | None) -> dict[str, Any]:
    """An end port on its device, with the device's deployment."""
    return {
        "id": f"id-{device}:{name}",
        "name": {"value": name},
        "device": {"node": {"id": f"id-{device}", "name": {"value": device}, "deployment": {"node": deployment}}},
    }


BL10 = _port("bl-dc101101", "Ethernet1/35", DC10)
BL11 = _port("bl-dc1111101", "Ethernet1/35", DC11)
EG_FR = _port("eg-fr01", "Ethernet1/13", FR)
EG_PA = _port("eg-pa01", "Ethernet1/13", PA)
AWS = _port("EXT-AWS-DX-FR2", "et-0/0/0", EUC1)
GW = _port("EQX-FR2-SDWAN-GW1", "eth0", FR2)
OFFICE_EDGE = _port("C001-OFFICE-EDGE1", "GE1", C001_OFFICE)
ORANGE_PE = _port("EXT-ORANGE-1", "ge-0/0/1", ORANGE)


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {"edges": [{"node": node} for node in nodes]}


def _physical(name: str, locations: list[dict[str, Any]], ends: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "locations": _edges(locations),
        "capabilities": _edges(ends),
    }


DF10 = _physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10, EG_FR])
DF11 = _physical("DF-DC11-EQXFR2", [DC11, FR2], [BL11, EG_FR])
XC = _physical("XC-EQXFR2-AWS-EUC1", [FR2, EUC1], [EG_FR, AWS])
INET_OFFICE = _physical("INET-C001-OFFICE", [C001_OFFICE, ORANGE], [OFFICE_EDGE, ORANGE_PE])


def _virtual(
    name: str,
    locations: list[dict[str, Any]],
    ends: list[dict[str, Any]],
    hops: list[dict[str, Any]],
    owner: str | None = None,
    transport_mode: str = "physical_backed",
) -> dict[str, Any]:
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "transport_mode": {"value": transport_mode},
        "owner": {"node": {"id": f"id-{owner}", "org_id": {"value": owner}} if owner else None},
        "locations": _edges(locations),
        "capabilities": _edges(ends),
        "physical_circuits": _edges(
            [{"id": hop["id"], "name": hop["name"], "locations": hop["locations"]} for hop in hops]
        ),
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
        ],
        ids=["dc-to-cage", "cage-to-region", "office-to-isp", "device-in-the-cage"],
    )
    def test_placed_circuit_passes(self, circuit: dict[str, Any]) -> None:
        """Each location is where one end's device is deployed: no finding."""
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
        """A cage end must be a zone of the metro the edge is deployed to: PA4 with an FR edge fails."""
        messages = _run(physical=[_physical("DF-DC10-EQXPA4", [DC10, PA4], [BL10, EG_FR])])
        assert len(messages) == 1
        assert "a cage end a zone of the metro the device is deployed to" in messages[0]

    def test_ends_must_match_locations_one_to_one(self) -> None:
        """Both ends in DC10 leave the FR2 location without an end."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10, _port("bl-dc101102", "e1", DC10)])])
        assert len(messages) == 1

    def test_end_count_is_left_to_physical_circuit_ports(self) -> None:
        """A circuit with one end gets no placement finding: checks/physical_circuit_ports.py reports it."""
        assert _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10])]) == []


class TestVirtualCircuitTermination:
    def test_not_two_locations_fails(self) -> None:
        """A virtual circuit has exactly two distinct locations."""
        messages = _run(virtual=[_virtual("VC-X", [C005_DC10], [BL10, EG_FR], [DF10, XC], owner="C005")])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-X' terminates on 1 location(s)" in messages[0]

    @pytest.mark.parametrize(
        "circuit",
        [
            _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, EG_FR], [DF10, XC], owner="C005"),
            # A colocation footprint is parented by the metro: the FR edge places it, cage FR2 starts the chain.
            _virtual("VC-C001-EQXFR-AWS-EUC1", [C001_FR, EUC1], [EG_FR], [XC], owner="C001"),
            # Far end another footprint (of the same owner here).
            _virtual(
                "VC-C005-DC10-FR",
                [C005_DC10, _place("C005-P-FR", "TopologyCustomerColocation", FR, "C005")],
                [BL10, EG_FR],
                [DF10],
                owner="C005",
            ),
        ],
        ids=["dc-footprint-to-region", "colo-footprint-via-metro", "footprint-to-footprint"],
    )
    def test_owned_circuit_passes(self, circuit: dict[str, Any]) -> None:
        """Tenant end a footprint of the owner with a device in its facility, far end a provider place."""
        assert _run(virtual=[circuit]) == []

    def test_owned_without_own_footprint_fails(self) -> None:
        """Owned by C006 while the only footprint is C005's: the tenant end is a footprint of the owner."""
        messages = _run(virtual=[_virtual("VC-C006", [C005_DC10, EUC1], [BL10, EG_FR], [DF10, XC], owner="C006")])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-C006' is owned by 'C006' but none of its locations" in messages[0]

    def test_tenant_facility_without_end_device_fails(self) -> None:
        """C005-P-DC10 while the circuit ends on DC11's border leaf: no end device is in DC10."""
        messages = _run(virtual=[_virtual("VC-C005", [C005_DC10, EUC1], [BL11, EG_PA], [DF10, XC], owner="C005")])
        assert len(messages) == 1
        assert "tenant end TopologyCustomerDC 'C005-P-DC10' is in TopologyDataCenter 'DC10'" in messages[0]

    def test_owned_far_end_not_a_provider_facility_fails(self) -> None:
        """An owned circuit runs to a provider facility or a footprint, not a bare DC."""
        messages = _run(
            virtual=[
                _virtual(
                    "VC-C005-DC11",
                    [C005_DC10, DC11],
                    [BL10, BL11],
                    [],
                    owner="C005",
                    transport_mode="provider_virtual_only",
                )
            ]
        )
        assert len(messages) == 1
        assert "far end is a provider facility" in messages[0]

    def test_unowned_between_facilities_passes(self) -> None:
        """A platform overlay (no owner) between two facilities."""
        assert _run(virtual=[_virtual("VC-SDWAN-FR2-AWS-EUC1", [FR2, EUC1], [GW, EG_FR], [XC])]) == []

    def test_unowned_on_a_footprint_fails(self) -> None:
        """No owner but a customer footprint end: a platform overlay terminates on facilities."""
        messages = _run(virtual=[_virtual("VC-PLATFORM", [C005_DC10, EUC1], [BL10, EG_FR], [DF10, XC])])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-PLATFORM' has no owner but terminates on TopologyCustomerDC" in messages[0]


class TestVirtualCircuitUnderlay:
    def test_hops_in_any_order_pass(self) -> None:
        """Relationship peers carry no order: the reversed hop list is still one path."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, EG_FR], [XC, DF10], owner="C005")
        assert _run(virtual=[circuit]) == []

    def test_chain_not_starting_at_the_tenant_facility_fails(self) -> None:
        """C005-P-DC10 riding DC11's fibre: the chain does not start at DC10."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, EG_FR], [DF11, XC], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "Virtual circuit 'VC-C005-DC10-AWS-EUC1' underlay" in messages[0]
        assert "is not one contiguous path from TopologyDataCenter 'DC10' to TopologyCloudRegion" in messages[0]

    def test_chain_not_reaching_the_far_end_fails(self) -> None:
        """The dark fibre alone stops at FR2, short of eu-central-1."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, EG_FR], [DF10], owner="C005")
        assert len(_run(virtual=[circuit])) == 1

    def test_physical_backed_without_underlay_fails(self) -> None:
        """physical_backed riding no physical circuit."""
        circuit = _virtual("VC-C005-DC10-AWS-EUC1", [C005_DC10, EUC1], [BL10, EG_FR], [], owner="C005")
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "is physical_backed but rides no physical circuit" in messages[0]

    def test_internet_backed_reaching_the_internet_passes(self) -> None:
        """An internet-backed overlay's underlay runs from one end to the internet, not to the far end."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE, GW],
            [INET_OFFICE],
            owner="C001",
            transport_mode="internet_backed",
        )
        assert _run(virtual=[circuit]) == []

    def test_internet_backed_not_reaching_the_internet_fails(self) -> None:
        """Riding a dark fibre instead of an internet access circuit."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE, GW],
            [DF10],
            owner="C001",
            transport_mode="internet_backed",
        )
        messages = _run(virtual=[circuit])
        assert len(messages) == 1
        assert "to the internet (a TopologyExternalProviders)" in messages[0]

    def test_internet_backed_without_underlay_passes(self) -> None:
        """No physical circuit is required for an internet-backed overlay."""
        circuit = _virtual(
            "C001-PARTNER-ACME-FR2",
            [C001_OFFICE, FR2],
            [OFFICE_EDGE, GW],
            [],
            owner="C001",
            transport_mode="internet_backed",
        )
        assert _run(virtual=[circuit]) == []
