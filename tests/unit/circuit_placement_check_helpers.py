"""Shared payload builders and the check runner for the CheckCircuitPlacement unit tests.

See tests/unit/test_circuit_placement_check.py for how the payloads mirror queries/validation/circuit_placement.gql
and data/demos/30_all.
"""

from __future__ import annotations

from typing import Any, cast

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
