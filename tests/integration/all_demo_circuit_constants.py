"""30_all interconnect expectations (test_62): physical and virtual circuits, their ends and locations, the colocation
serving zones (the zones of a footprint's metro that have a firewall pair)."""

from typing import TypedDict

from generators.helpers.naming import sdwan_edge_name

from .all_demo_constants import ALL_DEMO_DC_NAMES, _all_demo
from .demo_data import Declared, name, parent_name, render, schema

#: The `circuit_type` values of a TopologySessionCircuit (its role)
TUNNEL_TYPE = "tunnel"
INTERCONNECT_TYPE = "dci"
HANDOFF_TYPE = "handoff"


def _session_circuits(circuit_type: str) -> list[Declared]:
    """The declared TopologySessionCircuit objects of one `circuit_type`."""
    return [c for c in _all_demo("TopologySessionCircuit") if c.value("circuit_type") == circuit_type]


# ---------------------------------------------------------------------------
# 30_all — interconnect and tenant-service expectations (test_62)
# ---------------------------------------------------------------------------

# The offices that opt into add_sdwan_edge (generators/topology/sdwan_edge.py) by naming a gateway in sdwan_gateway.
_SDWAN_OFFICES: list[Declared] = [
    office for office in _all_demo("TopologyCustomerOffice") if office.item.get("sdwan_gateway")
]


# Carrier circuits are pure carriers, all declared in the data except the office underlays
_DECLARED_CIRCUIT_TYPE_COUNTS: dict[str, int] = {
    circuit_type: len([c for c in _all_demo("TopologyCarrierCircuit") if c.value("circuit_type") == circuit_type])
    for circuit_type in ("dark_fiber", "cross_connect", "virtual_connection", "internet")
}

ALL_DEMO_CARRIER_CIRCUIT_TYPES: dict[str, int] = {
    **_DECLARED_CIRCUIT_TYPE_COUNTS,
    "internet": _DECLARED_CIRCUIT_TYPE_COUNTS["internet"] + len(_SDWAN_OFFICES),
}


# The DCI Interconnect circuits (08_interconnects/03_virtual_circuits/01_dci_interconnects.yml)
ALL_DEMO_DCI_CIRCUITS: dict[str, tuple[str, tuple[tuple[str, str], tuple[str, str]]]] = {
    name(circuit): (
        next(location for location in circuit.item["locations"] if location in ALL_DEMO_DC_NAMES),
        (
            (str(circuit.item["capabilities"][0][0]), str(circuit.item["capabilities"][0][1])),
            (str(circuit.item["capabilities"][1][0]), str(circuit.item["capabilities"][1][1])),
        ),
    )
    for circuit in _session_circuits(INTERCONNECT_TYPE)
}


# Shared ISP transit (08_interconnects/06_internet): colo edges -> ISP PEs.
ALL_DEMO_INTERNET_TRANSIT_CIRCUITS: frozenset[str] = frozenset(
    {"INET-LUMEN-EQXFR2", "INET-COGENT-EQXFR2", "INET-LUMEN-EQXPA4", "INET-COGENT-EQXPA4"}
)


# The shared SD-WAN gateway's own internet access (08_interconnects/08_gateway_access)
ALL_DEMO_GATEWAY_ACCESS_CIRCUITS: dict[str, frozenset[tuple[str, str]]] = {
    "INET-LUMEN-EQXFR2-GW": frozenset({("EQX-FR2-SDWAN-GW1", "eth1"), ("EXT-LUMEN-FRA1", "et-0/0/1")}),
}

# The ISP router has no place: the circuit has the cage alone.
ALL_DEMO_GATEWAY_ACCESS_LOCATIONS: tuple[str, ...] = ("FR2",)


# The partner's own internet access (08_interconnects/07_zone_policies/01_partner_tunnel.yml)
ALL_DEMO_PARTNER_ACCESS_CIRCUITS: dict[str, frozenset[tuple[str, str]]] = {
    "INET-ACME-COGENT": frozenset({("EXT-ACME-EDGE1", "et-0/0/0"), ("EXT-COGENT-FRA1", "et-0/0/1")}),
}


# The uplink of every ISP router to THE INTERNET (data/bootstrap/03b_internet.yml, rule I3)
ALL_DEMO_ISP_UPLINK_CIRCUITS: dict[str, tuple[str, str]] = {
    "UPL-EXT-LUMEN-FRA1": ("EXT-LUMEN-FRA1", "xe-0/1/0"),
    "UPL-EXT-LUMEN-PAR1": ("EXT-LUMEN-PAR1", "xe-0/1/0"),
    "UPL-EXT-COGENT-FRA1": ("EXT-COGENT-FRA1", "xe-0/1/0"),
    "UPL-EXT-COGENT-PAR1": ("EXT-COGENT-PAR1", "xe-0/1/0"),
    "UPL-EXT-ORANGE": ("EXT-ORANGE", "et-0/0/0"),
}

#: The one end every uplink shares
ALL_DEMO_INTERNET_BUS: tuple[str, str] = ("INTERNET", "bus")


# The unowned internet circuits: only a customer's own underlay (INET-<office>) names an owner.
ALL_DEMO_SHARED_INTERNET_CIRCUITS: frozenset[str] = (
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
    | frozenset(ALL_DEMO_ISP_UPLINK_CIRCUITS)
    | frozenset(ALL_DEMO_GATEWAY_ACCESS_CIRCUITS)
    | frozenset(ALL_DEMO_PARTNER_ACCESS_CIRCUITS)
)


# The SD-WAN offices' internet access (add_sdwan_edge)
ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS: dict[str, str] = {f"INET-{name(office)}": name(office) for office in _SDWAN_OFFICES}

ALL_DEMO_SDWAN_EDGE_UPLINK = "GE1"

ALL_DEMO_ISP_ACCESS_DEVICE = "EXT-ORANGE"


ALL_DEMO_ISP_ACCESS_PORTS: frozenset[str] = frozenset(f"xe-0/1/{n}" for n in range(len(_SDWAN_OFFICES)))


# INTERNET upstream (PR 4c, extended by the zone edge pair)
ALL_DEMO_INTERNET_BGWS: frozenset[str] = frozenset({"eg-fr201", "eg-fr202", "eg-pa401", "eg-pa402"})


# DC -> the BGWs its DCI sessions land on, i.e. where that DC learns 0/0 from
ALL_DEMO_INTERNET_UPSTREAM_BY_DC: dict[str, frozenset[str]] = {
    "DC10": frozenset({"eg-fr201", "eg-fr202"}),
    "DC11": frozenset({"eg-fr201", "eg-fr202"}),
    "DC12": frozenset({"eg-pa401", "eg-pa402"}),
}


def _zone_edge_pair(zone: Declared) -> tuple[str, str] | None:
    """The two edge names a zone's fabric_templates declare (``eg-<zone>01``, ``eg-<zone>02``), else None."""
    quantity = sum(int(row[0]) for row in zone.item.get("fabric_templates") or [] if row[1] == "edge")
    if quantity != 2:
        return None
    prefix = f"eg-{name(zone).lower()}"
    return f"{prefix}01", f"{prefix}02"


# Zones with exactly two PHYSICAL edges in 30_all (generators/topology/colocation_fabric.py _ensure_edge_pair; a virtual
# colocation's edges are never paired).
ALL_DEMO_EDGE_PAIR_ZONES: dict[str, tuple[str, str]] = {
    name(zone).lower(): pair for zone in _all_demo("TopologyColocationZone") if (pair := _zone_edge_pair(zone))
}


# The interlink port (N9K-C9316D-GX_EDGE Ethernet1/8, role `interlink`) cabled between the pair.
ALL_DEMO_EDGE_INTERLINK_INTERFACE = "Ethernet1/8"


# zone -> ((device, interface), (device, interface)) of the one DcimCable per pair.
ALL_DEMO_EDGE_INTERLINKS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    zone: ((a, ALL_DEMO_EDGE_INTERLINK_INTERFACE), (b, ALL_DEMO_EDGE_INTERLINK_INTERFACE))
    for zone, (a, b) in ALL_DEMO_EDGE_PAIR_ZONES.items()
}


# zone -> the one FABRIC-<a>-<b> iBGP session (peering_role fabric) between the pair's overlay processes.
ALL_DEMO_FABRIC_PEERINGS: dict[str, str] = {
    zone: f"FABRIC-{a}-{b}" for zone, (a, b) in ALL_DEMO_EDGE_PAIR_ZONES.items()
}


# The offices with a fabric (add_office_fabric)
_OFFICE_SWITCH_PREFIXES = {"distribution-switch": "ds", "access-switch": "as"}

ALL_DEMO_OFFICE_FABRICS: dict[str, dict[str, tuple[str, ...]]] = {
    name(office): {
        role: tuple(
            f"{prefix}-{name(office).lower()}{index:02d}"
            for index in range(
                1,
                1
                + sum(
                    int(quantity)
                    for quantity, template_role, _ in office.item["fabric_templates"]
                    if template_role == role
                ),
            )
        )
        for role, prefix in _OFFICE_SWITCH_PREFIXES.items()
    }
    for office in _all_demo("TopologyCustomerOffice")
    if office.item.get("fabric_templates")
}


# The WAN exit of an office with both a fabric and an SD-WAN edge (add_office_fabric)
ALL_DEMO_OFFICE_WAN_EXITS: dict[str, tuple[str, ...]] = {
    name(office): ALL_DEMO_OFFICE_FABRICS[name(office)]["distribution-switch"]
    for office in _SDWAN_OFFICES
    if name(office) in ALL_DEMO_OFFICE_FABRICS
}


# The zone-owned slice of Technical-IPv6 the interlink /127 comes from
ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE = "{zone}-technical-ipv6-pool"


# The shared Multi-Site VIP: both edges of a zone's pair carry ONE /128 on loopback100
# (data/demos/30_all/08_interconnects/01_colo_onramp/09_multisite_vips.yml).
ALL_DEMO_ZONE_SITE_VIPS: dict[str, str] = {"fr2": "fd00:2500:ff::f1/128", "pa4": "fd00:2500:ff::f2/128"}


# The four eBGP transit sessions in VRF INTERNET (06_internet/05_bgp.yml) -> the colo edge each runs on.
ALL_DEMO_INTERNET_TRANSIT_PEERINGS: dict[str, str] = {
    "TRANSIT-LUMEN-EQXFR2": "eg-fr201",
    "TRANSIT-COGENT-EQXFR2": "eg-fr202",
    "TRANSIT-LUMEN-EQXPA4": "eg-pa401",
    "TRANSIT-COGENT-EQXPA4": "eg-pa402",
}


# The L3 VNI of the INTERNET namespace (data/bootstrap) and its global route-target
# `{l3_vni}:{l3_vni}`, imported and exported by every VTEP carrying the VRF.
INTERNET_L3_VNI = 60002

INTERNET_ROUTE_TARGET = "60002:60002"


class VirtualCircuitExpectation(TypedDict):
    """What one session circuit (a tunnel, dci or handoff `circuit_type`) in 30_all must look like."""

    #: The `circuit_type` of the TopologySessionCircuit: tunnel, dci or handoff.
    circuit_type: str
    #: The circuit's locations, sorted.
    locations: tuple[str, ...]
    #: The (device, interface) ends in `capabilities`, sorted.
    ends: tuple[tuple[str, str], ...]
    #: circuit_ids of the carrier circuits derived from the ends (on the end port, or on its parent interface when it is
    #: a sub-interface), in end order, each once; never declared.
    carrier: tuple[str, ...]
    #: The routing zone of the circuit's `vrf`: a Handoff's
    security_zone: str | None


_DEVICE_DEPLOYMENTS: dict[str, str] = {
    name(device): str(device.value("deployment")) for device in _all_demo("DcimPhysicalDevice")
}


#: The public port of the shared SD-WAN gateway every tunnel ends on (its one `customer` physical port).
ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT = "eth1"

#: The circuit that carries the gateway's public port.
ALL_DEMO_SDWAN_GATEWAY_ACCESS = "INET-LUMEN-EQXFR2-GW"


def _sdwan_tunnel(office: Declared) -> tuple[str, VirtualCircuitExpectation]:
    """The tunnel add_sdwan_edge builds for one opted-in office, named as the generator names it."""
    office_name = name(office)
    gateway = str(office.item["sdwan_gateway"])
    zone = _DEVICE_DEPLOYMENTS[gateway]
    return f"{office_name}-SDWAN-{zone}", {
        "circuit_type": TUNNEL_TYPE,
        "locations": tuple(sorted((office_name, zone))),
        "ends": tuple(
            sorted(
                (
                    (gateway, ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT),
                    (sdwan_edge_name(office_name), ALL_DEMO_SDWAN_EDGE_UPLINK),
                )
            )
        ),
        "carrier": (f"INET-{office_name}", ALL_DEMO_SDWAN_GATEWAY_ACCESS),
        "security_zone": None,
    }


# The virtual-circuit fabric test_62 compares with the inventory
ALL_DEMO_VIRTUAL_CIRCUITS: dict[str, VirtualCircuitExpectation] = {
    # One generated tunnel per SD-WAN office (_sdwan_tunnel).
    **dict(_sdwan_tunnel(office) for office in _SDWAN_OFFICES),
    # C001's partner extranet (08_interconnects/07_zone_policies/01_partner_tunnel.yml)
    "C001-PARTNER-ACME-FR2": {
        "circuit_type": TUNNEL_TYPE,
        "locations": ("C001-OFFICE", "FR2"),
        "ends": (("EQX-FR2-SDWAN-GW1", "eth1"), ("EXT-ACME-EDGE1", "et-0/0/0")),
        "carrier": ("INET-LUMEN-EQXFR2-GW", "INET-ACME-COGENT"),
        "security_zone": "PROD",
    },
    # The DCI: ends = the fibre's two ports, locations = the fibre's two facilities.
    **{
        circuit_name: {
            "circuit_type": INTERCONNECT_TYPE,
            "locations": tuple(sorted(locations)),
            "ends": ends,
            "carrier": (fibre,),
            "security_zone": None,
        }
        for circuit_name, locations, ends, fibre in (
            (
                "ICX-DC10-EQXFR2",
                ("DC10", "FR2"),
                (("bl-dc101101", "Ethernet1/35"), ("eg-fr201", "Ethernet1/10")),
                "DF-DC10-EQXFR2",
            ),
            (
                "ICX-DC11-EQXFR2",
                ("DC11", "FR2"),
                (("bl-dc1111101", "Ethernet1/35"), ("eg-fr201", "Ethernet1/11")),
                "DF-DC11-EQXFR2",
            ),
            (
                "ICX-DC12-EQXPA4",
                ("DC12", "PA4"),
                (("bl-dc1212101", "Ethernet1/35"), ("eg-pa401", "Ethernet1/10")),
                "DF-DC12-EQXPA4",
            ),
            (
                "ICX-DC10-EQXFR2-B",
                ("DC10", "FR2"),
                (("bl-dc101101", "Ethernet1/36"), ("eg-fr202", "Ethernet1/10")),
                "DF-DC10-EQXFR2-B",
            ),
            (
                "ICX-DC11-EQXFR2-B",
                ("DC11", "FR2"),
                (("bl-dc1111101", "Ethernet1/36"), ("eg-fr202", "Ethernet1/11")),
                "DF-DC11-EQXFR2-B",
            ),
            (
                "ICX-DC12-EQXPA4-B",
                ("DC12", "PA4"),
                (("bl-dc1212101", "Ethernet1/36"), ("eg-pa402", "Ethernet1/10")),
                "DF-DC12-EQXPA4-B",
            ),
        )
    },
    # The SD-WAN gateway's LAN handoff (04_sdwan/06_vrf_handoff.yml): both ends in one cage
    "HANDOFF-FR2-SDWAN-GW1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "locations": ("FR2",),
        "ends": (("EQX-FR2-SDWAN-GW1", "eth0.1900"), ("eg-fr201", "Ethernet1/12.1900")),
        "carrier": (),
        "security_zone": "PROD",
    },
    # C001's AWS handoff (08_interconnects/03_virtual_circuits/02_aws_prod_handoff.yml)
    "HANDOFF-FR2-AWS-EUC1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "locations": ("FR2", "eu-central-1"),
        "ends": (("dx-30all-euc1-fr2", "dxcon-fr2.1000"), ("eg-fr201", "Ethernet1/13.1000")),
        "carrier": (),
        "security_zone": "PROD",
    },
}


# A circuit has no owner of its own
ALL_DEMO_TUNNEL_OWNERS: dict[str, str] = {
    **{_sdwan_tunnel(office)[0]: render(office, "{{ owner__org_id__value }}") for office in _SDWAN_OFFICES},
    "C001-PARTNER-ACME-FR2": "C001",
}


# Circuit placement (test_62 test_01/test_02).
ALL_DEMO_FACILITY_KINDS: frozenset[str] = frozenset(
    kind for kind, definition in schema().items() if "TopologyFacility" in definition.inherit_from
)


_DECLARED_CARRIER_CIRCUITS = {str(c.value("circuit_id")): c for c in _all_demo("TopologyCarrierCircuit")}

_DECLARED_VIRTUAL_CIRCUITS = {
    name(c): c
    for circuit_type in (TUNNEL_TYPE, INTERCONNECT_TYPE, HANDOFF_TYPE)
    for c in _session_circuits(circuit_type)
}


# circuit_id / name -> the locations the 30_all data authors.
ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS: dict[str, tuple[str, ...]] = {
    circuit_id: tuple(circuit.item.get("locations") or ()) for circuit_id, circuit in _DECLARED_CARRIER_CIRCUITS.items()
}

ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS: dict[str, tuple[str, ...]] = {
    circuit_name: tuple(circuit.item.get("locations") or ())
    for circuit_name, circuit in _DECLARED_VIRTUAL_CIRCUITS.items()
}


# circuit_id / name -> the (device, interface) ports the data declares in `capabilities`.
ALL_DEMO_DECLARED_PHYSICAL_PORTS: dict[str, frozenset[tuple[str, str]]] = {
    circuit_id: frozenset((str(device), str(port)) for device, port in circuit.item.get("capabilities") or ())
    for circuit_id, circuit in _DECLARED_CARRIER_CIRCUITS.items()
}

ALL_DEMO_DECLARED_VIRTUAL_PORTS: dict[str, frozenset[tuple[str, str]]] = {
    circuit_name: frozenset((str(device), str(port)) for device, port in circuit.item.get("capabilities") or ())
    for circuit_name, circuit in _DECLARED_VIRTUAL_CIRCUITS.items()
}


# Handoff circuit -> (the party device, its ASN)
_DECLARED_PROCESS_ASNS: dict[str, int] = {
    str(device): int(str(process.item["local_as"]).removeprefix("AS"))
    for process in _all_demo("ManagedBGP")
    for device in process.item.get("capabilities") or []
}

ALL_DEMO_HANDOFF_PARTY_ASNS: dict[str, tuple[str, int]] = {
    circuit_name: (party[0], _DECLARED_PROCESS_ASNS[party[0]])
    for circuit_name, party in (
        (
            circuit_name,
            [device for device, _ in expected["ends"] if device in _DECLARED_PROCESS_ASNS],
        )
        for circuit_name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items()
        if expected["circuit_type"] == HANDOFF_TYPE
    )
    if len(party) == 1
}


# The handoffs whose far end is a cloud peering interface (a CloudPeeringInterface the data declares):
# add_virtual_circuit builds no session for them, the data authors the sub-interface address and the ManagedBGPPeering.
_CLOUD_PEERING_ENDS: frozenset[tuple[str, str]] = frozenset(
    (str(peering.item["device"]), name(peering)) for peering in _all_demo("CloudPeeringInterface")
)

ALL_DEMO_CLOUD_HANDOFF_PEERINGS: dict[str, str] = {
    circuit_name: str(peering.value("name"))
    for circuit_name, expected in ALL_DEMO_VIRTUAL_CIRCUITS.items()
    if expected["circuit_type"] == HANDOFF_TYPE and any(end in _CLOUD_PEERING_ENDS for end in expected["ends"])
    for peering in _all_demo("ManagedBGPPeering")
    if [list(end) for end in expected["ends"] if end not in _CLOUD_PEERING_ENDS] == peering.item["capabilities"]
}


def _site(deployment: Declared) -> str:
    """The DC or colocation metro a customer footprint sits in (its parent): `C001-P-FR` -> FR."""
    return str(deployment.value("parent"))


def _zone_has_firewalls(zone: Declared) -> bool:
    """Whether a colocation zone declares a firewall pair in its fabric_templates."""
    return any(template[1] == "internal-firewall" for template in zone.item.get("fabric_templates") or [])


# Metro -> its zones that declare a firewall pair (a virtual colocation is never one).
def _firewall_zones_by_metro() -> dict[str, tuple[str, ...]]:
    by_metro: dict[str, list[str]] = {}
    for zone in _all_demo("TopologyColocationZone"):
        if _zone_has_firewalls(zone):
            by_metro.setdefault(str(parent_name(zone)), []).append(name(zone))
    return {metro: tuple(sorted(zones)) for metro, zones in by_metro.items()}


_FIREWALL_ZONES_BY_METRO = _firewall_zones_by_metro()


# Every 30_all colocation deployment -> the zones whose firewall pair serve it
ALL_DEMO_COLOCATION_SERVING_ZONES: dict[str, tuple[str, ...]] = {
    name(deployment): _FIREWALL_ZONES_BY_METRO.get(_site(deployment), ())
    for deployment in _all_demo("TopologyCustomerColocation")
}


# Every 30_all colocation deployment -> whether it has a serving context
ALL_DEMO_COLOCATION_SERVED: dict[str, bool] = {
    deployment: bool(zones) for deployment, zones in ALL_DEMO_COLOCATION_SERVING_ZONES.items()
}

# Serving zone -> the deployments its shared context serves, keyed by zone name (`FR2`)
ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    zone: tuple(deployment for deployment, serving in ALL_DEMO_COLOCATION_SERVING_ZONES.items() if zone in serving)
    for zone in sorted({z for zones in _FIREWALL_ZONES_BY_METRO.values() for z in zones})
}
