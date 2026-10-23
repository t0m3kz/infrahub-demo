"""Unit tests for CheckCircuitPlacement (where circuits terminate, what carries them, which environment they are in).

The payloads are raw GraphQL responses shaped like queries/validation/circuit_placement.gql (one root per circuit
kind); ``validate`` runs them through ``clean_data``. The places mirror data/demos/30_all: DC10/DC11 with their
border leaves, the Equinix FR cage FR2 (the edges eg-fr201/eg-fr202 are deployed to the ZONE FR2, not the metro),
the Equinix AM virtual colocation AM-NE, AWS eu-central-1, the ISPs Orange and Lumen, C001's office (owner of its
access circuit INET-C001-OFFICE).

A virtual circuit's carrier is derived, never listed: the physical circuits found in the ``capabilities`` of its own
ports, or of the parent interface of a sub-interface. ``_vport`` puts them there, nested under the port as the
query selects them. The ports mirror the demo data: DF-DC10-EQXFR2 ends on bl-dc101101 Ethernet1/35 and eg-fr201
Ethernet1/10, the cross-connect XC-EQXFR2-AWS-EUC1 on eg-fr201 Ethernet1/13 and the stub's et-0/0/0.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from checks.circuit_placement import CheckCircuitPlacement

KINDS = {"TopologyPhysicalCircuit", "TopologyTunnelCircuit", "TopologyInterconnectCircuit", "TopologyHandoffCircuit"}


def _place(name: str, kind: str, owner: str | None = None) -> dict[str, Any]:
    """A location or deployment node, as the query selects it (an office carries its owner)."""
    node: dict[str, Any] = {"id": f"id-{name}", "__typename": kind, "name": {"value": name}}
    if owner is not None:
        node["owner"] = {"node": {"id": f"id-{owner}", "org_id": {"value": owner}}}
    return node


DC10 = _place("DC10", "TopologyDataCenter")
DC11 = _place("DC11", "TopologyDataCenter")
FR = _place("FR", "TopologyColocationMetro")
PA = _place("PA", "TopologyColocationMetro")
AM = _place("AM", "TopologyColocationMetro")
FR2 = _place("FR2", "TopologyColocationZone")
FR6 = _place("FR6", "TopologyColocationZone")
PA4 = _place("PA4", "TopologyColocationZone")
AM_NE = _place("AM-NE", "TopologyVirtualColocation")
EUC1 = _place("eu-central-1", "TopologyCloudRegion")
ORANGE = _place("ISP-ORANGE", "TopologyExternalProviders")
LUMEN = _place("ISP-LUMEN", "TopologyExternalProviders")
C001_OFFICE = _place("C001-OFFICE", "TopologyCustomerOffice", owner="C001")
C002_OFFICE = _place("C002-OFFICE", "TopologyCustomerOffice", owner="C002")


def _edges(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """A GraphQL ``edges`` list over the given nodes."""
    return {"edges": [{"node": node} for node in nodes]}


def _owner(owner: str | None) -> dict[str, Any]:
    """A circuit's owner edge, as the query selects it (empty when unowned)."""
    return {"node": {"id": f"id-{owner}", "org_id": {"value": owner}} if owner else None}


def _end(device: str, name: str, deployment: dict[str, Any] | None) -> dict[str, Any]:
    """An end port of a physical circuit, with the device's deployment (id, kind and name only)."""
    deployment = {key: deployment[key] for key in ("id", "__typename", "name")} if deployment else None
    return {
        "id": f"id-{device}:{name}",
        "name": {"value": name},
        "device": {"node": {"id": f"id-{device}", "name": {"value": device}, "deployment": {"node": deployment}}},
    }


BL10_END = _end("bl-dc101101", "Ethernet1/35", DC10)
BL11_END = _end("bl-dc1111101", "Ethernet1/35", DC11)
EG_FR_END = _end("eg-fr201", "Ethernet1/13", FR2)  # the cross-connect's port
EG_FR_DF10_END = _end("eg-fr201", "Ethernet1/10", FR2)  # the dark fibre to DC10 ends on another port of the edge
EG_FR_DF11_END = _end("eg-fr201", "Ethernet1/11", FR2)
EG_FR_METRO_END = _end("eg-fr01", "Ethernet1/13", FR)  # legacy: no device is deployed to a metro any more
EG_PA_END = _end("eg-pa401", "Ethernet1/13", PA4)
EG_AM_NE_END = _end("eg-am-ne01", "Ethernet1/13", AM_NE)  # a virtual edge deployed to the virtual colocation
AWS_END = _end("EXT-AWS-DX-FR2", "et-0/0/0", EUC1)
GW_END = _end("EQX-FR2-SDWAN-GW1", "eth1", FR2)  # the gateway's public port
GW_ETH0_END = _end("EQX-FR2-SDWAN-GW1", "eth0", FR2)
OFFICE_EDGE_END = _end("C001-OFFICE-EDGE1", "GE1", C001_OFFICE)
ORANGE_PE_END = _end("EXT-ORANGE-1", "ge-0/0/1", ORANGE)
LUMEN_PE_END = _end("EXT-LUMEN-1", "ge-0/0/1", LUMEN)


def _physical(
    name: str,
    locations: list[dict[str, Any]],
    ends: list[dict[str, Any]],
    owner: str | None = None,
) -> dict[str, Any]:
    """A physical circuit as the query selects it: its locations and the end ports it terminates on."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "owner": _owner(owner),
        "locations": _edges(locations),
        "capabilities": _edges(ends),
    }


DF10 = _physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10_END, EG_FR_DF10_END])
DF11 = _physical("DF-DC11-EQXFR2", [DC11, FR2], [BL11_END, EG_FR_DF11_END])
XC = _physical("XC-EQXFR2-AWS-EUC1", [FR2, EUC1], [EG_FR_END, AWS_END])
XC_AM_NE = _physical("XC-EQXAMNE-AWS-EUC1", [AM_NE, EUC1], [EG_AM_NE_END, AWS_END])
INET_OFFICE = _physical("INET-C001-OFFICE", [C001_OFFICE, ORANGE], [OFFICE_EDGE_END, ORANGE_PE_END], owner="C001")
INET_GW = _physical("INET-LUMEN-EQXFR2-GW", [FR2, LUMEN], [GW_END, LUMEN_PE_END])


def _decommissioned(circuit: dict[str, Any]) -> dict[str, Any]:
    """The circuit with its status set to decommissioned (as ``status { value }`` selects it)."""
    return {**circuit, "status": {"value": "decommissioned"}}


def _carrier(circuit: dict[str, Any]) -> dict[str, Any]:
    """A physical circuit as the query selects it under a port: status, locations and its end ports."""
    ends = [edge["node"] for edge in circuit["capabilities"]["edges"]]
    return {
        "__typename": "TopologyPhysicalCircuit",
        "id": circuit["id"],
        "name": circuit["name"],
        "status": circuit.get("status", {"value": "active"}),
        "locations": _edges(
            [{key: edge["node"][key] for key in ("id", "__typename", "name")} for edge in circuit["locations"]["edges"]]
        ),
        "capabilities": _edges(
            [
                {"id": end["id"], "name": end["name"], "device": {"node": {"name": end["device"]["node"]["name"]}}}
                for end in ends
            ]
        ),
    }


def _deployment_of(device: str) -> dict[str, Any] | None:
    """Where a device of the virtual-circuit fixtures is deployed: the edges and gateways in their cage or DC, every
    party router (EXT-*) in the cloud region."""
    deployed = {
        "bl-dc101101": DC10,
        "bl-dc1111101": DC11,
        "eg-fr201": FR2,
        "EQX-FR2-SDWAN-GW1": FR2,
        "eg-pa401": PA4,
        "eg-am-ne01": AM_NE,
        "C001-OFFICE-EDGE1": C001_OFFICE,
    }
    place = deployed.get(device, EUC1 if device.startswith("EXT-") else None)
    return {key: place[key] for key in ("id", "__typename", "name")} if place else None


def _vport(
    device: str,
    name: str,
    carriers: tuple[dict[str, Any], ...] = (),
    parent: tuple[str, tuple[dict[str, Any], ...]] | None = None,
    device_kind: str = "DcimPhysicalDevice",
    bgp: tuple[str, ...] = ("overlay",),
    cabled: bool = False,
    parent_cabled: bool = False,
) -> dict[str, Any]:
    """A virtual circuit's end port: the physical circuits on it, the parent interface (name, circuits on it) of a
    sub-interface, and its device with its deployment and its BGP processes (one per role in ``bgp``).

    ``cabled`` puts a DcimCable on a physical port, ``parent_cabled`` on the parent of a sub-interface (a
    sub-interface itself has no ``cable``: the query selects it on the physical interface only)."""
    port: dict[str, Any] = {
        "id": f"id-{device}:{name}",
        "name": {"value": name},
        # a segment or another circuit on the port is no carrier
        "capabilities": _edges([*(_carrier(circuit) for circuit in carriers), {"__typename": "ManagedVxlanSegment"}]),
        "device": {
            "node": {
                "id": f"id-{device}",
                "__typename": device_kind,
                "name": {"value": device},
                "deployment": {"node": _deployment_of(device)},
                "capabilities": _edges(
                    [{"__typename": "ManagedBGP", "process_role": {"value": role}} for role in bgp]
                    + [{"__typename": "ManagedOSPF"}]
                ),
            }
        },
    }
    if parent is None:
        port["cable"] = {"node": {"id": f"cable-{device}:{name}"} if cabled else None}
    else:
        parent_name, parent_carriers = parent
        port["parent_interface"] = {
            "node": {
                "cable": {"node": {"id": f"cable-{device}:{parent_name}"} if parent_cabled else None},
                "capabilities": _edges([_carrier(circuit) for circuit in parent_carriers]),
            }
        }
    return port


# Ends of the virtual circuits, riding their carriers.
BL10 = _vport("bl-dc101101", "Ethernet1/35", (DF10,))
BL11 = _vport("bl-dc1111101", "Ethernet1/35", (DF11,))
EG_FR_DF10 = _vport("eg-fr201", "Ethernet1/10", (DF10,))
EG_FR_DF11 = _vport("eg-fr201", "Ethernet1/11", (DF11,))
OFFICE_EDGE = _vport("C001-OFFICE-EDGE1", "GE1", (INET_OFFICE,))
GW_PUBLIC = _vport("EQX-FR2-SDWAN-GW1", "eth1", (INET_GW,))
# A handoff VLAN rides the circuit that terminates on the parent port.
EG_FR_VLAN = _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
AWS_VLAN = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3101", parent=("et-0/0/0", (XC,)))
AWS_VLAN_NO_CARRIER = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3101", parent=("et-0/0/0", ()))


def _virtual(name: str, locations: list[dict[str, Any]], ends: list[dict[str, Any]]) -> dict[str, Any]:
    """The fields every virtual kind selects."""
    return {
        "id": f"id-{name}",
        "name": {"value": name},
        "locations": _edges(locations),
        "capabilities": _edges(ends),
    }


def _tunnel(
    name: str, locations: list[dict[str, Any]], ends: list[dict[str, Any]], owner: str | None = "C001"
) -> dict[str, Any]:
    """A tunnel circuit, owned by C001 (the owner of the C001 office) unless told otherwise."""
    return {**_virtual(name, locations, ends), "owner": _owner(owner)}


def _interconnect(
    name: str, locations: list[dict[str, Any]], ends: list[dict[str, Any]], owner: str | None = None
) -> dict[str, Any]:
    """An interconnect (DCI) circuit; unowned unless told otherwise, since a DCI is shared platform."""
    return {**_virtual(name, locations, ends), "owner": _owner(owner)}


def _handoff(
    name: str,
    locations: list[dict[str, Any]] | None,
    ends: list[dict[str, Any]],
    namespace: tuple[str, str | None] | None = ("PROD", "prod"),
    vif: tuple[str, int | None] | None = None,
) -> dict[str, Any]:
    """A handoff in ``namespace`` (name, namespace_type); ``vif`` is its cloud endpoint (name, vlan_id).

    ``locations=None`` places the handoff where its ends are deployed (what the check requires); a list states them.
    """
    if locations is None:
        deployments = [end["device"]["node"]["deployment"]["node"] for end in ends]
        locations = list({deployment["id"]: deployment for deployment in deployments if deployment}.values())
    node = _virtual(name, locations, ends)
    node["ip_namespace"] = {
        "node": {"name": {"value": namespace[0]}, "namespace_type": {"value": namespace[1]}} if namespace else None
    }
    endpoint: dict[str, Any] | None = None
    if vif is not None:
        endpoint = {"name": {"value": vif[0]}}
        if vif[1] is not None:
            endpoint["vlan_id"] = {"value": vif[1]}
    node["cloud_endpoint"] = {"node": endpoint}
    return node


def _run(
    physical: list[dict[str, Any]] | None = None,
    tunnels: list[dict[str, Any]] | None = None,
    interconnects: list[dict[str, Any]] | None = None,
    handoffs: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Validate the circuits and return the logged error messages."""
    check = cast(Any, CheckCircuitPlacement.__new__(CheckCircuitPlacement))
    findings: list[dict[str, Any]] = []
    check.log_error = lambda **kwargs: findings.append(kwargs)
    check.validate(
        {
            "TopologyPhysicalCircuit": _edges(physical or []),
            "TopologyTunnelCircuit": _edges(tunnels or []),
            "TopologyInterconnectCircuit": _edges(interconnects or []),
            "TopologyHandoffCircuit": _edges(handoffs or []),
        }
    )
    for finding in findings:
        assert finding["object_id"] and finding["object_type"] in KINDS
    return [finding["message"] for finding in findings]


# A tunnel, an interconnect and a handoff that pass; the tests below break one thing at a time.
TUNNEL = _tunnel("C001-OFFICE-SDWAN-FR2", [C001_OFFICE, FR2], [OFFICE_EDGE, GW_PUBLIC])
DCI = _interconnect("DCI-DC10-FR2", [DC10, FR2], [BL10, EG_FR_DF10])
HANDOFF = _handoff(
    "HO-C005-P-AWS-EUC1", None, [EG_FR_VLAN, AWS_VLAN], namespace=("PROD", "prod"), vif=("VIF-C005-P", 3101)
)


class TestPhysicalCircuitPlacement:
    @pytest.mark.parametrize(
        "circuit",
        [
            DF10,
            XC,
            INET_OFFICE,
            INET_GW,
            # A device racked in the cage itself places the cage end too.
            _physical("XC-GW-AWS", [FR2, EUC1], [GW_ETH0_END, AWS_END]),
            # A virtual colocation is a circuit end like a zone. Synthetic: no demo physical circuit ends on
            # AM-NE (eg-am-ne01 is a virtual edge); XC_AM_NE is the fixture, a cross-connect from AM-NE to AWS.
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
        assert _run(physical=[circuit]) == []

    @pytest.mark.parametrize("locations", [[FR2], []], ids=["only-the-authored-cage", "none"])
    def test_not_two_locations_fails(self, locations: list[dict[str, Any]]) -> None:
        """A physical circuit with fewer than two distinct locations (a derived end missing) is an error."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", locations, [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXFR2' terminates on" in messages[0]
        assert "exactly 2 distinct locations" in messages[0]

    def test_end_device_without_deployment_fails(self) -> None:
        """An end on a device with no deployment cannot place its end: error naming the port."""
        undeployed = _end("EXT-AWS-DX-FR2", "et-0/0/0", None)
        messages = _run(physical=[_physical("XC-EQXFR2-AWS-EUC1", [FR2, EUC1], [EG_FR_END, undeployed])])
        assert len(messages) == 1
        assert "end 'EXT-AWS-DX-FR2:et-0/0/0' is on a device with no deployment" in messages[0]

    def test_dc_end_other_than_the_device_deployment_fails(self) -> None:
        """Authored DC10 while the border leaf sits in DC11: the DC end is the device's deployment."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL11_END, EG_FR_END])])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXFR2' terminates on" in messages[0]
        assert "bl-dc1111101:Ethernet1/35 in TopologyDataCenter 'DC11'" in messages[0]

    def test_cage_of_another_metro_fails(self) -> None:
        """A colocation end must be the edge's zone itself: PA4 with an edge in FR2 fails."""
        messages = _run(physical=[_physical("DF-DC10-EQXPA4", [DC10, PA4], [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "not a sibling zone of its metro" in messages[0]

    def test_virtual_colocation_of_another_metro_fails(self) -> None:
        """AM-NE is not where an edge in zone FR2 is deployed."""
        messages = _run(physical=[_physical("DF-DC10-EQXAMNE", [DC10, AM_NE], [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXAMNE' terminates on" in messages[0]

    def test_sibling_cage_of_the_same_metro_fails(self) -> None:
        """Authored FR6 while the edge sits in FR2: add_circuit (plan_locations) refuses it as contradicting the
        endpoint devices' deployment, so the check does too."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR6", [DC10, FR6], [BL10_END, EG_FR_END])])
        assert len(messages) == 1
        assert "eg-fr201:Ethernet1/13 in TopologyColocationZone 'FR2'" in messages[0]
        assert "TopologyColocationZone 'FR6'" in messages[0]

    def test_device_deployed_to_the_metro_fails(self) -> None:
        """No device is deployed to a metro any more: a legacy metro deployment places no zone of it."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2-METRO", [DC10, FR2], [BL10_END, EG_FR_METRO_END])])
        assert len(messages) == 1
        assert "eg-fr01:Ethernet1/13 in TopologyColocationMetro 'FR'" in messages[0]

    def test_two_zones_of_different_metros_with_edges_in_one_fail(self) -> None:
        """FR2 and PA4 with both edges in FR2: one location has no end."""
        messages = _run(physical=[_physical("XC-FR2-PA4", [FR2, PA4], [EG_FR_END, _end("eg-fr202", "e1", FR2)])])
        assert len(messages) == 1

    def test_ends_must_match_locations_one_to_one(self) -> None:
        """Both ends in DC10 leave the FR2 location without an end."""
        messages = _run(
            physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10_END, _end("bl-dc101102", "e1", DC10)])]
        )
        assert len(messages) == 1

    def test_end_count_is_left_to_physical_circuit_ports(self) -> None:
        """A circuit with one end gets no placement finding: checks/physical_circuit_ports.py reports it."""
        assert _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10_END])]) == []


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
        """An office's access circuit that is unowned or owned by another customer is reported with its ownership."""
        circuit = _physical("INET-C001-OFFICE", [C001_OFFICE, ORANGE], [OFFICE_EDGE_END, ORANGE_PE_END], owner=owner)
        messages = _run(physical=[circuit])
        assert messages == [
            f"Physical circuit 'INET-C001-OFFICE' ends on TopologyCustomerOffice 'C001-OFFICE' of 'C001' but is "
            f"{ownership}: an office's access circuit is owned by the office's owner."
        ]

    def test_owned_shared_underlay_fails(self) -> None:
        """A dark fibre carries every tenant: owned, it would pin the shared underlay on one customer."""
        messages = _run(physical=[_physical("DF-DC10-EQXFR2", [DC10, FR2], [BL10_END, EG_FR_END], owner="C005")])
        assert len(messages) == 1
        assert "Physical circuit 'DF-DC10-EQXFR2' is owned by 'C005' but ends on no office" in messages[0]

    def test_ownership_is_checked_with_a_location_missing(self) -> None:
        """The owner rule does not wait for the derived end: one location is enough to see the office."""
        circuit = _physical("INET-C001-OFFICE", [C001_OFFICE], [OFFICE_EDGE_END, ORANGE_PE_END])
        messages = _run(physical=[circuit])
        assert len(messages) == 2
        assert "an office's access circuit is owned by the office's owner" in messages[0]
        assert "terminates on 1 location(s)" in messages[1]


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
        other_edge = _vport("C002-OFFICE-EDGE1", "GE1", (_physical("INET-C002-OFFICE", [C002_OFFICE, ORANGE], []),))
        other = _tunnel("C002-OFFICE-SDWAN-FR2", [C002_OFFICE, FR2], [other_edge, GW_PUBLIC], owner="C002")
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

    @pytest.mark.parametrize(
        ("owner", "locations"),
        [(None, [C001_OFFICE, FR2]), ("C002", [C001_OFFICE, FR2]), ("C001", [FR2, EUC1])],
        ids=["unowned", "other-customers-office", "no-office"],
    )
    def test_not_owned_by_the_offices_owner_fails(self, owner: str | None, locations: list[dict[str, Any]]) -> None:
        """Unowned, owned by another customer or with no office among its locations: all break the ownership rule."""
        messages = _run(tunnels=[_tunnel("T", locations, [OFFICE_EDGE, GW_PUBLIC], owner=owner)])
        assert len(messages) == 1
        assert "Tunnel circuit 'T' is " in messages[0]
        assert "a tunnel is owned by its office's owner" in messages[0]

    def test_end_on_no_physical_circuit_fails(self) -> None:
        """A tunnel end whose port rides no physical circuit is reported as riding no live internet circuit."""
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, _vport("EQX-FR2-SDWAN-GW1", "eth1")])])
        assert len(messages) == 1
        assert "end 'EQX-FR2-SDWAN-GW1:eth1' rides no live internet physical circuit" in messages[0]
        assert "it is on no live one" in messages[0]

    def test_end_on_a_circuit_that_is_not_internet_fails(self) -> None:
        """The gateway port is on a dark fibre, not on the internet."""
        messages = _run(
            tunnels=[_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, _vport("EQX-FR2-SDWAN-GW1", "eth1", (DF10,))])]
        )
        assert len(messages) == 1
        assert "its live physical circuits are DF-DC10-EQXFR2 (DC10 - FR2)" in messages[0]

    def test_end_on_a_decommissioned_internet_circuit_fails(self) -> None:
        """A decommissioned internet circuit is not a carrier, and is left out of the message."""
        port = _vport("C001-OFFICE-EDGE1", "GE1", (_decommissioned(INET_OFFICE),))
        messages = _run(tunnels=[_tunnel("T", [C001_OFFICE, FR2], [port, GW_PUBLIC])])
        assert len(messages) == 1
        assert "end 'C001-OFFICE-EDGE1:GE1' rides no live internet physical circuit" in messages[0]
        assert "INET-C001-OFFICE" not in messages[0]

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
    """DCI between two border gateways: unowned, each end on a live circuit, one contiguous path, pure fibre, one
    overlay process per end device."""

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

    def test_owned_fails(self) -> None:
        """A DCI is shared platform: an owner on it is reported."""
        messages = _run(interconnects=[_interconnect("DCI-DC10-FR2", [DC10, FR2], [BL10, EG_FR_DF10], owner="C005")])
        assert messages == [
            "Interconnect circuit 'DCI-DC10-FR2' is owned by 'C005': a DCI between two border gateways is shared "
            "platform, unowned."
        ]

    def test_not_two_locations_fails(self) -> None:
        """A DCI with one location is reported."""
        messages = _run(interconnects=[_interconnect("DCI", [DC10], [BL10, EG_FR_DF10])])
        assert len(messages) == 1
        assert "Interconnect circuit 'DCI' terminates on 1 location(s)" in messages[0]

    def test_not_two_end_ports_fails(self) -> None:
        """A DCI with a single end port is reported."""
        messages = _run(interconnects=[_interconnect("DCI", [DC10, FR2], [BL10])])
        assert any("has 1 end port(s)" in message for message in messages)

    def test_end_on_no_live_physical_circuit_fails(self) -> None:
        """A DCI end whose port is on no physical circuit is reported with the fix to apply."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, _vport("eg-fr201", "Ethernet1/10")])
        messages = _run(interconnects=[circuit])
        assert messages == [
            "Interconnect circuit 'DCI' end 'eg-fr201:Ethernet1/10' is on no live physical circuit: terminate it on "
            "a port cabled through a physical circuit."
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
        assert all("is on no live physical circuit" in message for message in messages)

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
                [FR2, EUC1], [EG_FR_VLAN, _vport("EQX-FR2-SDWAN-GW1", "eth0.3101", (XC,))], id="location-no-end-is-in"
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

    def test_unowned_and_owned_both_pass(self) -> None:
        """The owner is optional: nothing is checked on it."""
        owned = {**HANDOFF, "owner": _owner("C005")}
        assert _run(handoffs=[HANDOFF]) == []
        assert _run(handoffs=[owned]) == []

    def test_carrier_on_the_port_itself_passes(self) -> None:
        """A handoff on an unchannelled port: the port's own circuit is the carrier."""
        ends = [_vport("eg-fr201", "Ethernet1/13.3101", (XC,)), AWS_VLAN_NO_CARRIER]
        assert _run(handoffs=[_handoff("HO", None, ends)]) == []

    def test_one_physical_end_with_a_carrier_is_enough(self) -> None:
        """The stub has no circuit at its parent port, the edge has: the handoff rides it."""
        assert _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, AWS_VLAN_NO_CARRIER])]) == []

    def test_a_cable_on_the_parent_port_is_a_carrier(self) -> None:
        """Both ends in one cage: a hop inside one facility is a DcimCable, not a physical circuit."""
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
        assert "no live physical circuit and no cable" in messages[0]

    def test_no_physical_end_needs_no_carrier(self) -> None:
        """Both ends on Network Edge appliances or virtual devices: there is no physical circuit to require."""
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
        assert "has no live physical circuit and no cable at EXT-AWS-DX-FR2:et-0/0/0.3101" in messages[0]

    def test_no_carrier_on_physical_ends_fails(self) -> None:
        """Physical ends with no circuit and no cable are reported, naming the first such port."""
        ends = [_vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert len(messages) == 1
        assert (
            "Handoff circuit 'HO' has no live physical circuit and no cable at eg-fr201:Ethernet1/13.3101"
            in messages[0]
        )

    def test_decommissioned_carriers_do_not_count(self) -> None:
        """A decommissioned circuit on every end carries nothing: the handoff has no carrier."""
        ends = [
            _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (_decommissioned(XC),))),
            _vport("EXT-AWS-DX-FR2", "et-0/0/0.3101", parent=("et-0/0/0", (_decommissioned(XC),))),
        ]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert len(messages) == 1
        assert "has no live physical circuit and no cable" in messages[0]

    def test_end_that_is_not_a_vlan_sub_interface_fails(self) -> None:
        """A handoff end must be named <port>.<vlan>; a bare physical port is reported."""
        ends = [EG_FR_VLAN, _vport("EXT-AWS-DX-FR2", "et-0/0/0", (XC,))]
        messages = _run(handoffs=[_handoff("HO", None, ends)])
        assert messages == [
            "Handoff circuit 'HO' end 'EXT-AWS-DX-FR2:et-0/0/0' is not a VLAN sub-interface: a handoff end is named "
            "<port>.<vlan>."
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

    def test_cloud_endpoint_vlan_must_mirror_the_sub_interface(self) -> None:
        """The cloud virtual interface states VLAN 3102 while the sub-interfaces use 3101: reported."""
        messages = _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, AWS_VLAN], vif=("VIF-C005-P", 3102))])
        assert messages == [
            "Handoff circuit 'HO' ends are on VLAN 3101 but its cloud endpoint 'VIF-C005-P' states vlan_id 3102: "
            "the virtual interface mirrors the sub-interface VLAN."
        ]

    def test_cloud_endpoint_without_a_vlan_passes(self) -> None:
        """A VPN gateway endpoint has no VLAN to mirror."""
        assert _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, AWS_VLAN], vif=("VGW-C005-P", None))]) == []

    def test_no_cloud_endpoint_passes(self) -> None:
        """A handoff with no cloud endpoint has no VLAN to mirror."""
        assert _run(handoffs=[_handoff("HO", None, [EG_FR_VLAN, AWS_VLAN], vif=None)]) == []


class TestSessionPorts:
    """A port is an end of at most one Interconnect or Handoff circuit; a tunnel is exempt."""

    def test_two_handoffs_on_the_same_pair_of_ports_are_one_finding(self) -> None:
        """Two handoffs on the same two ports give one finding naming both circuits, not one per port."""
        other = _handoff("HO-B", None, [EG_FR_VLAN, AWS_VLAN])
        messages = _run(handoffs=[HANDOFF, other])
        assert messages == [
            "Circuits 'HO-B', 'HO-C005-P-AWS-EUC1' all terminate on 'EXT-AWS-DX-FR2:et-0/0/0.3101', "
            "'eg-fr201:Ethernet1/13.3101': a port is an end of at most one Interconnect or Handoff circuit, one "
            "session per address."
        ]

    def test_two_handoffs_sharing_one_port_fail(self) -> None:
        """Sharing a single port is enough: one port carries one session."""
        other_stub = _vport("EXT-AWS-DX-FR2", "et-0/0/1.3101", parent=("et-0/0/1", (XC,)))
        messages = _run(handoffs=[HANDOFF, _handoff("HO-B", None, [EG_FR_VLAN, other_stub])])
        assert len(messages) == 1
        assert "all terminate on 'eg-fr201:Ethernet1/13.3101'" in messages[0]

    def test_an_interconnect_and_a_handoff_sharing_a_port_fail(self) -> None:
        """The one-session-per-port rule spans both kinds: a DCI and a handoff on one port conflict."""
        handoff = _handoff("HO-B", None, [_vport("eg-fr201", "Ethernet1/10", (DF10,)), AWS_VLAN])
        messages = [m for m in _run(interconnects=[DCI], handoffs=[handoff]) if "all terminate on" in m]
        assert len(messages) == 1
        assert "'DCI-DC10-FR2', 'HO-B' all terminate on 'eg-fr201:Ethernet1/10'" in messages[0]

    def test_handoffs_on_different_vlans_of_one_port_pass(self) -> None:
        """Sub-interfaces are ports of their own: a cage port carries one handoff per VLAN."""
        edge = _vport("eg-fr201", "Ethernet1/13.3102", parent=("Ethernet1/13", (XC,)))
        stub = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        assert _run(handoffs=[HANDOFF, _handoff("HO-B", None, [edge, stub])]) == []


class TestEnvironmentSeparation:
    """Circuits with a port on the same device are of one environment class: prod never meets non-prod."""

    @staticmethod
    def _on_edge(name: str, namespace: tuple[str, str | None] | None, vlan: int = 3101) -> dict[str, Any]:
        """A handoff on the cage edge eg-fr201 (VLAN ``vlan``) towards its own stub."""
        edge = _vport("eg-fr201", f"Ethernet1/13.{vlan}", parent=("Ethernet1/13", (XC,)))
        stub = _vport(f"EXT-{name}", f"et-0/0/0.{vlan}", parent=("et-0/0/0", (XC,)))
        return _handoff(name, None, [edge, stub], namespace=namespace)

    def test_handoffs_of_one_class_sharing_an_edge_pass(self) -> None:
        """Two prod handoffs (VLANs 3101 and 3102) on one edge are the same environment class."""
        circuits = [self._on_edge("HO-A", ("PROD", "prod"), 3101), self._on_edge("HO-B", ("PROD", "prod"), 3102)]
        assert _run(handoffs=circuits) == []

    def test_prod_and_non_prod_on_one_device_fail_once(self) -> None:
        """One finding for the device, naming it and both circuits with their namespaces, whatever the order."""
        prod = self._on_edge("HO-P", ("PROD", "prod"), 3101)
        non_prod = self._on_edge("HO-N", ("NON-PROD", "non_prod"), 3102)
        for circuits in ([prod, non_prod], [non_prod, prod]):
            messages = _run(handoffs=circuits)
            assert len(messages) == 1
            assert "Device 'eg-fr201' carries virtual circuits of different environments" in messages[0]
            assert "'HO-P' (namespace 'PROD', prod)" in messages[0]
            assert "'HO-N' (namespace 'NON-PROD', non_prod)" in messages[0]

    def test_conflict_among_many_circuits_is_one_finding_per_device(self) -> None:
        """Three circuits on one device with prod and non-prod among them give a single finding for the device."""
        circuits = [
            self._on_edge("HO-P1", ("PROD", "prod"), 3101),
            self._on_edge("HO-P2", ("PROD", "prod"), 3102),
            self._on_edge("HO-N", ("NON-PROD", "non_prod"), 3103),
        ]
        assert len(_run(handoffs=circuits)) == 1

    def test_each_conflicting_device_is_reported(self) -> None:
        """A prod and a non-prod handoff sharing both of their devices: one finding for each device."""
        gateway = _vport("EQX-FR2-SDWAN-GW1", "eth2.3101", parent=("eth2", (XC,)))
        edge = _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
        prod = _handoff("HO-P", None, [edge, gateway], namespace=("PROD", "prod"))
        non_prod = _handoff("HO-N", None, [edge, gateway], namespace=("NON-PROD", "non_prod"))
        messages = _run(handoffs=[prod, non_prod])
        env_messages = [message for message in messages if "different environments" in message]
        assert sorted(message.split("'")[1] for message in env_messages) == ["EQX-FR2-SDWAN-GW1", "eg-fr201"]

    @pytest.mark.parametrize(
        ("namespace", "expected"),
        [(("PROD", "prod"), 0), (("NON-PROD", "non_prod"), 1)],
        ids=["tunnel-meets-prod", "tunnel-meets-non-prod"],
    )
    def test_tunnel_counts_as_prod(self, namespace: tuple[str, str], expected: int) -> None:
        """An office is always PROD: its tunnel meets a prod handoff on the gateway, conflicts with a non-prod one."""
        edge = _vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
        gateway = _vport("EQX-FR2-SDWAN-GW1", "eth2.3101", parent=("eth2", (XC,)))
        handoff = _handoff("HO", None, [edge, gateway], namespace=namespace)
        messages = [message for message in _run(tunnels=[TUNNEL], handoffs=[handoff]) if "environments" in message]
        assert len(messages) == expected
        if expected:
            assert "Device 'EQX-FR2-SDWAN-GW1'" in messages[0]
            assert "'C001-OFFICE-SDWAN-FR2' (office 'C001-OFFICE', prod)" in messages[0]

    @pytest.mark.parametrize(
        "namespace",
        [("INTERNET", "internet"), ("MANAGEMENT", "management"), ("X", None), None],
        ids=["internet", "management", "untyped", "no-namespace"],
    )
    def test_a_handoff_in_a_namespace_with_no_tenant_class_is_skipped(
        self, namespace: tuple[str, str | None] | None
    ) -> None:
        """Internet, management, untyped or absent namespaces state no environment class, so they never conflict."""
        prod = self._on_edge("HO-P", ("PROD", "prod"), 3101)
        other = self._on_edge("HO-X", namespace, 3102)
        assert _run(handoffs=[prod, other]) == []

    def test_circuits_alone_on_their_devices_pass(self) -> None:
        """A non-prod handoff on its own edge does not meet the prod one on eg-fr201."""
        edge = _vport("eg-pa401", "Ethernet1/13.3101", parent=("Ethernet1/13", (XC,)))
        stub = _vport("EXT-NONPROD-PA", "et-0/0/0.3101", parent=("et-0/0/0", (XC,)))
        non_prod = _handoff("HO-N", None, [edge, stub], namespace=("NON-PROD", "non_prod"))
        assert _run(handoffs=[self._on_edge("HO-P", ("PROD", "prod")), non_prod]) == []

    def test_interconnect_states_no_class(self) -> None:
        """A DCI runs in the underlay: a non-prod handoff on the same edge does not conflict with it."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, EG_FR_DF10])
        edge = _vport("eg-fr201", "Ethernet1/10.3101", parent=("Ethernet1/10", (DF10,)))
        stub = _vport("EXT-NONPROD", "et-0/0/0.3101", parent=("et-0/0/0", (XC,)))
        handoff = _handoff("HO-N", None, [edge, stub], namespace=("NON-PROD", "non_prod"))
        messages = _run(interconnects=[circuit], handoffs=[handoff])
        assert not any("environments" in message for message in messages)


class TestDecommissionedSessionCircuits:
    """add_virtual_circuit builds nothing for a decommissioned session circuit, so the check does not enforce the
    session rules on it; the same defect on an active circuit is still reported."""

    @staticmethod
    def _uncarried_dci(status: str) -> dict[str, Any]:
        """A DCI whose far end is on no physical circuit."""
        circuit = _interconnect("DCI", [DC10, FR2], [BL10, _vport("eg-fr201", "Ethernet1/10")])
        return {**circuit, "status": {"value": status}}

    @staticmethod
    def _broken_chain_dci(status: str) -> dict[str, Any]:
        """A DCI whose carrier stops at FR2, short of the region."""
        circuit = _interconnect("DCI", [DC10, EUC1], [BL10, EG_FR_DF10])
        return {**circuit, "status": {"value": status}}

    @staticmethod
    def _bad_handoff(name: str, status: str, ends: list[dict[str, Any]]) -> dict[str, Any]:
        """A handoff in FR2 with the given ends and status, for defects the check must or must not report."""
        return {**_handoff(name, None, ends), "status": {"value": status}}

    def test_decommissioned_interconnect_with_an_uncarried_end_is_not_reported(self) -> None:
        """A decommissioned DCI is not built, so an uncarried end on it is not a finding."""
        assert _run(interconnects=[self._uncarried_dci("decommissioned")]) == []

    def test_decommissioned_interconnect_with_a_non_contiguous_carrier_is_not_reported(self) -> None:
        """A decommissioned DCI is not built, so a broken carrier chain on it is not a finding."""
        assert _run(interconnects=[self._broken_chain_dci("decommissioned")]) == []

    def test_decommissioned_interconnect_with_other_defects_is_not_reported(self) -> None:
        """Owned, one end port and no overlay process: nothing is reported for a circuit nothing builds."""
        circuit = _interconnect("DCI", [DC10], [_vport("bl-dc101101", "Ethernet1/35", (DF10,), bgp=())], owner="C001")
        assert _run(interconnects=[{**circuit, "status": {"value": "decommissioned"}}]) == []

    def test_active_interconnect_with_the_same_defects_is_reported(self) -> None:
        """The same two defects on an active DCI are findings, so the exemption is by status only."""
        uncarried = _run(interconnects=[self._uncarried_dci("active")])
        assert len(uncarried) == 1
        assert "end 'eg-fr201:Ethernet1/10' is on no live physical circuit" in uncarried[0]
        chain = _run(interconnects=[self._broken_chain_dci("active")])
        assert len(chain) == 1
        assert "is not one contiguous path" in chain[0]

    def test_decommissioned_handoff_with_no_carrier_is_not_reported(self) -> None:
        """A decommissioned handoff is not built, so a missing carrier is not a finding."""
        ends = [_vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        assert _run(handoffs=[self._bad_handoff("HO", "decommissioned", ends)]) == []

    def test_decommissioned_handoff_with_mismatched_vlans_is_not_reported(self) -> None:
        """A decommissioned handoff is not built, so differing VLANs (3101, 3102) are not a finding."""
        other = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        assert _run(handoffs=[self._bad_handoff("HO", "decommissioned", [EG_FR_VLAN, other])]) == []

    def test_active_handoff_with_the_same_defects_is_reported(self) -> None:
        """The same two defects on an active handoff are findings, so the exemption is by status only."""
        no_carrier = [_vport("eg-fr201", "Ethernet1/13.3101", parent=("Ethernet1/13", ())), AWS_VLAN_NO_CARRIER]
        messages = _run(handoffs=[self._bad_handoff("HO", "active", no_carrier)])
        assert len(messages) == 1
        assert "has no live physical circuit and no cable" in messages[0]
        other = _vport("EXT-AWS-DX-FR2", "et-0/0/0.3102", parent=("et-0/0/0", (XC,)))
        messages = _run(handoffs=[self._bad_handoff("HO", "active", [EG_FR_VLAN, other])])
        assert len(messages) == 1
        assert "ends carry different VLANs" in messages[0]

    @pytest.mark.parametrize("live_first", [True, False], ids=["live-first", "dead-first"])
    def test_decommissioned_circuit_sharing_ports_with_a_live_one_is_not_reported(self, live_first: bool) -> None:
        """The dead circuit holds no port: neither it nor the live circuit gets the one-session-per-port finding."""
        dead = self._bad_handoff("HO-OLD", "decommissioned", [EG_FR_VLAN, AWS_VLAN])
        circuits = [HANDOFF, dead] if live_first else [dead, HANDOFF]
        assert _run(handoffs=circuits) == []

    def test_two_active_circuits_sharing_ports_are_still_reported(self) -> None:
        """Two live circuits on the same ports still conflict."""
        live = self._bad_handoff("HO-OLD", "active", [EG_FR_VLAN, AWS_VLAN])
        messages = _run(handoffs=[HANDOFF, live])
        assert len(messages) == 1
        assert "all terminate on" in messages[0]

    def test_decommissioned_interconnect_sharing_a_port_with_a_live_handoff_is_not_reported(self) -> None:
        """A dead DCI holds no port, so the live handoff on the same port is not reported."""
        handoff = _handoff("HO-B", None, [_vport("eg-fr201", "Ethernet1/10", (DF10,)), AWS_VLAN])
        dead = {**DCI, "status": {"value": "decommissioned"}}
        assert not any("all terminate on" in m for m in _run(interconnects=[dead], handoffs=[handoff]))

    def test_decommissioned_prod_handoff_meeting_a_live_non_prod_one_is_not_reported(self) -> None:
        """A dead prod handoff meets nothing, so the live non-prod one on the same device is not a conflict."""
        prod = TestEnvironmentSeparation._on_edge("HO-P", ("PROD", "prod"), 3101)
        non_prod = TestEnvironmentSeparation._on_edge("HO-N", ("NON-PROD", "non_prod"), 3102)
        dead = {**prod, "status": {"value": "decommissioned"}}
        assert _run(handoffs=[dead, non_prod]) == []

    def test_active_prod_handoff_meeting_a_non_prod_one_is_still_reported(self) -> None:
        """With an explicit active status the prod and non-prod handoffs on one device still conflict."""
        prod = {**TestEnvironmentSeparation._on_edge("HO-P", ("PROD", "prod"), 3101), "status": {"value": "active"}}
        non_prod = TestEnvironmentSeparation._on_edge("HO-N", ("NON-PROD", "non_prod"), 3102)
        messages = _run(handoffs=[prod, non_prod])
        assert len(messages) == 1
        assert "different environments" in messages[0]

    def test_decommissioned_tunnel_is_still_validated(self) -> None:
        """A tunnel is an inventory record no generator reads the status of: not exempt."""
        tunnel = {
            **_tunnel("T", [C001_OFFICE, FR2], [OFFICE_EDGE, GW_PUBLIC], owner=None),
            "status": {"value": "decommissioned"},
        }
        messages = _run(tunnels=[tunnel])
        assert len(messages) == 1
        assert "is unowned" in messages[0]


def test_all_circuits_together_pass() -> None:
    """The demo shapes side by side: physical carriers, a tunnel, a DCI and a handoff raise nothing."""
    assert (
        _run(physical=[DF10, XC, INET_OFFICE, INET_GW], tunnels=[TUNNEL], interconnects=[DCI], handoffs=[HANDOFF]) == []
    )
