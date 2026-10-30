"""30_all interconnect expectations (test_62): physical and virtual circuits, their ends and locations, the colocation
serving zones (the zones of a footprint's metro that have a firewall pair)."""

from typing import TypedDict

from generators.helpers.naming import sdwan_edge_name

from .all_demo_constants import ALL_DEMO_DC_NAMES, _all_demo
from .demo_data import Declared, name, parent_name, render, schema

#: The `circuit_type` values of a TopologySessionCircuit (its role); the constants are used by every consumer.
TUNNEL_TYPE = "tunnel"
INTERCONNECT_TYPE = "dci"
HANDOFF_TYPE = "handoff"


def _session_circuits(circuit_type: str) -> list[Declared]:
    """The declared TopologySessionCircuit objects of one `circuit_type`."""
    return [c for c in _all_demo("TopologySessionCircuit") if c.value("circuit_type") == circuit_type]


# ---------------------------------------------------------------------------
# 30_all — interconnect and tenant-service expectations (test_62)
# ---------------------------------------------------------------------------

# The offices that opt into add_sdwan_edge (generators/topology/sdwan_edge.py)
# by naming a gateway in sdwan_gateway. Each gets an `INET-<office>` internet
# underlay and an `<office>-SDWAN-<gateway zone>` tunnel from the generator;
# none of that is declared.
_SDWAN_OFFICES: list[Declared] = [
    office for office in _all_demo("TopologyCustomerOffice") if office.item.get("sdwan_gateway")
]


# Carrier circuits are pure carriers, all declared in the data except the office underlays: the dark fibres and
# cross-connects of 08_interconnects/01_colo_onramp (each DC has one fibre to each of its metro's two border gateways:
# DC10/DC11 -> EQX FR2 (eg-fr201 and eg-fr202), DC12 -> EQX PA4 (eg-pa401 and eg-pa402); cross-connects: AWS
# eu-central-1, Azure westeurope, Megaport MCR), the shared transit circuits of 06_internet, the shared gateway's own
# access (08_gateway_access) and the partner's access (07_zone_policies), plus one generated underlay per SD-WAN office.
_DECLARED_CIRCUIT_TYPE_COUNTS: dict[str, int] = {
    circuit_type: len([c for c in _all_demo("TopologyCarrierCircuit") if c.value("circuit_type") == circuit_type])
    for circuit_type in ("dark_fiber", "cross_connect", "internet")
}

ALL_DEMO_CARRIER_CIRCUIT_TYPES: dict[str, int] = {
    **_DECLARED_CIRCUIT_TYPE_COUNTS,
    "internet": _DECLARED_CIRCUIT_TYPE_COUNTS["internet"] + len(_SDWAN_OFFICES),
}


# The DCI Interconnect circuits (08_interconnects/03_virtual_circuits/01_dci_interconnects.yml), one per dark fibre:
# circuit name -> (the DC end, the two (device, interface) ends). add_virtual_circuit builds two sessions on each,
# `<name>-underlay` (the directly connected eBGP session) and `<name>-evpn` (the loopback-to-loopback EVPN session), keyed
# with the per-circuit key `<name>-dci-key`. The DC end is the DC among the circuit's two facility locations.
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
# Unowned, unlike a customer's own internet underlay.
ALL_DEMO_INTERNET_TRANSIT_CIRCUITS: frozenset[str] = frozenset(
    {"INET-LUMEN-EQXFR2", "INET-COGENT-EQXFR2", "INET-LUMEN-EQXPA4", "INET-COGENT-EQXPA4"}
)


# The shared SD-WAN gateway's own internet access (08_interconnects/08_gateway_access): its eth1 to a free `customer`
# port of Lumen's Frankfurt provider edge. Unowned like the transit, but not transit: no peering, no address, and it
# ends on the gateway, not a cage edge. circuit_id -> its two (device, interface) ends.
ALL_DEMO_GATEWAY_ACCESS_CIRCUITS: dict[str, frozenset[tuple[str, str]]] = {
    "INET-LUMEN-EQXFR2-GW": frozenset({("EQX-FR2-SDWAN-GW1", "eth1"), ("EXT-LUMEN-FRA1", "et-0/0/1")}),
}

ALL_DEMO_GATEWAY_ACCESS_LOCATIONS: tuple[str, ...] = ("FR2", "ISP-LUMEN")


# The partner's own internet access (08_interconnects/07_zone_policies/01_partner_tunnel.yml): the stub router
# EXT-ACME-EDGE1 (deployed to the partner topology PARTNER-ACME) to a free `customer` port of Cogent's Frankfurt edge.
# Unowned shared underlay: the carrier of the partner end of the tunnel C001-PARTNER-ACME-FR2. The data lists only
# ISP-COGENT; add_circuit derives PARTNER-ACME from the stub's deployment.
ALL_DEMO_PARTNER_ACCESS_CIRCUITS: dict[str, frozenset[tuple[str, str]]] = {
    "INET-ACME-COGENT": frozenset({("EXT-ACME-EDGE1", "et-0/0/0"), ("EXT-COGENT-FRA1", "et-0/0/1")}),
}

ALL_DEMO_PARTNER_ACCESS_LOCATIONS: tuple[str, ...] = ("ISP-COGENT", "PARTNER-ACME")


# The unowned internet circuits: only a customer's own underlay (INET-<office>) names an owner.
ALL_DEMO_SHARED_INTERNET_CIRCUITS: frozenset[str] = (
    ALL_DEMO_INTERNET_TRANSIT_CIRCUITS
    | frozenset(ALL_DEMO_GATEWAY_ACCESS_CIRCUITS)
    | frozenset(ALL_DEMO_PARTNER_ACCESS_CIRCUITS)
)


# The SD-WAN offices' internet access (add_sdwan_edge): INET-<office> runs from the office edge's GE1 to a free
# `customer` port of the one ISP access PE (30_all/04_internet), in the ISP's location. The offices board in one load and
# take the first free port in natural order under a lock, so together they hold xe-0/1/[0..n-1] exactly.
ALL_DEMO_SDWAN_UNDERLAY_CIRCUITS: dict[str, str] = {f"INET-{name(office)}": name(office) for office in _SDWAN_OFFICES}

ALL_DEMO_SDWAN_EDGE_UPLINK = "GE1"

ALL_DEMO_ISP_ACCESS_DEVICE = "EXT-ORANGE"

ALL_DEMO_ISP_ACCESS_LOCATION = "ISP-ORANGE"

ALL_DEMO_ISP_ACCESS_PORTS: frozenset[str] = frozenset(f"xe-0/1/{n}" for n in range(len(_SDWAN_OFFICES)))


# INTERNET upstream (PR 4c, extended by the zone edge pair): the colo edges are
# their metro's EVPN Multi-Site border gateways and export the ISP default route
# as an EVPN type-5 route (L3 VNI 60002, one global route-target for the
# INTERNET namespace). Both edges of each metro share the site VIP and have their
# own dark fibre to every DC the metro fronts (ALL_DEMO_DCI_CIRCUITS).
ALL_DEMO_INTERNET_BGWS: frozenset[str] = frozenset({"eg-fr201", "eg-fr202", "eg-pa401", "eg-pa402"})


# DC -> the BGWs its DCI sessions land on, i.e. where that DC learns 0/0 from:
# both edges of the metro it fronts.
ALL_DEMO_INTERNET_UPSTREAM_BY_DC: dict[str, frozenset[str]] = {
    "DC10": frozenset({"eg-fr201", "eg-fr202"}),
    "DC11": frozenset({"eg-fr201", "eg-fr202"}),
    "DC12": frozenset({"eg-pa401", "eg-pa402"}),
}


def _zone_edge_pair(zone: Declared) -> tuple[str, str] | None:
    """The two edge names a zone's fabric_templates declare (``eg-<zone>01``, ``eg-<zone>02``), else None.

    A zone name is in the device name (the standard naming strategy with no location path), so the pair is
    read from the data. Zones hold physical templates only, so every edge row of a zone is a physical edge.
    """
    quantity = sum(int(row[0]) for row in zone.item.get("fabric_templates") or [] if row[1] == "edge")
    if quantity != 2:
        return None
    prefix = f"eg-{name(zone).lower()}"
    return f"{prefix}01", f"{prefix}02"


# Zones with exactly two PHYSICAL edges in 30_all (generators/topology/colocation_fabric.py
# _ensure_edge_pair; a virtual colocation's edges are never paired). Keyed by the lowercase zone name the
# generator uses for its pool and key names (`fr2-technical-ipv6-pool`, `fr2-fabric-key`). Derived from
# data/demos/30_all/02_colo/equinix/{fr,pa}/02_topology.yml: FR2 and PA4 declare
# `["2", "edge", "N9K-C9316D-GX_EDGE"]`; the other zones and the metros declare none. The virtual colocation AM-NE
# declares `["2", "edge", "C8000V_EDGE"]` but is never paired (virtual edges have no port to cable).
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


# The offices with a fabric (add_office_fabric): office -> role -> the switch names the generator derives (a flat office
# is named `<prefix>-<office>NN`: `ds-` for the distribution-switches, `as-` for the access-switches). Declared nowhere:
# a fabric_templates line is the whole input.
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


# The WAN exit of an office with both a fabric and an SD-WAN edge (add_office_fabric): office -> its
# distribution-switches, each peered to `<office>-EDGE1` by one `WAN-EXIT-<switch>-<edge>` eBGP session in the
# office's tenant VRF, over a dot1q sub-interface tagged utils.exchange_transit.wan_exit_vlan.
ALL_DEMO_OFFICE_WAN_EXITS: dict[str, tuple[str, ...]] = {
    name(office): ALL_DEMO_OFFICE_FABRICS[name(office)]["distribution-switch"]
    for office in _SDWAN_OFFICES
    if name(office) in ALL_DEMO_OFFICE_FABRICS
}


# The zone-owned slice of Technical-IPv6 the interlink /127 comes from: `{zone}-technical-ipv6-pool`.
ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE = "{zone}-technical-ipv6-pool"


# The shared Multi-Site VIP: both edges of a zone's pair carry ONE /128 on loopback100
# (data/demos/30_all/08_interconnects/01_colo_onramp/04_multisite_vips.yml).
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
    #: The owning customer's name; None for the shared on-ramp circuits that belong to no single customer.
    owner: str | None
    #: The circuit's locations, sorted. Authored for a declared circuit, `(office, the gateway's zone)` for the tunnel
    #: add_sdwan_edge builds.
    locations: tuple[str, ...]
    #: The (device, interface) ends in `capabilities`, sorted. A Handoff end is the `<port>.<vlan>` sub-interface, a
    #: Tunnel's the gateway's public port and the other device's uplink.
    ends: tuple[tuple[str, str], ...]
    #: circuit_ids of the carrier circuits derived from the ends (on the end port, or on its parent interface when it
    #: is a sub-interface), in end order, each once; never declared. Empty when the carrier is a cable or there is none.
    #: The derived list is compared sorted in test_02 (declared ones only: the generated INET-<office> is in no row).
    carrier: tuple[str, ...]
    #: The Handoff's environment namespace; None for the other kinds.
    ip_namespace: str | None
    #: The Handoff's cloud endpoint (a transit VIF, set from the VIF side); None for the other kinds and for a Handoff
    #: that ends on no cloud service.
    cloud_endpoint: str | None


_CUSTOMER_NAMES: dict[str, str] = {
    str(org.value("org_id")): str(org.value("name")) for org in _all_demo("OrganizationCustomer")
}

_DEVICE_DEPLOYMENTS: dict[str, str] = {
    name(device): str(device.value("deployment")) for device in _all_demo("DcimPhysicalDevice")
}


#: The public port of the shared SD-WAN gateway every tunnel ends on (its one `customer` physical port).
ALL_DEMO_SDWAN_GATEWAY_PUBLIC_PORT = "eth1"

#: The circuit that carries the gateway's public port.
ALL_DEMO_SDWAN_GATEWAY_ACCESS = "INET-LUMEN-EQXFR2-GW"


def _sdwan_tunnel(office: Declared) -> tuple[str, VirtualCircuitExpectation]:
    """The tunnel add_sdwan_edge builds for one opted-in office, named as the generator names it.

    `<office>-SDWAN-<the gateway's zone>`, a tunnel TopologySessionCircuit owned by the office's customer, located at the
    office and the gateway's zone, ending on the gateway's public port and the edge uplink. Each end rides its own
    internet access: the edge uplink `INET-<office>`, the gateway's port the shared `INET-LUMEN-EQXFR2-GW`.
    """
    office_name = name(office)
    gateway = str(office.item["sdwan_gateway"])
    zone = _DEVICE_DEPLOYMENTS[gateway]
    return f"{office_name}-SDWAN-{zone}", {
        "circuit_type": TUNNEL_TYPE,
        "owner": _CUSTOMER_NAMES[render(office, "{{ owner__org_id__value }}")],
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
        "ip_namespace": None,
        "cloud_endpoint": None,
    }


# The full virtual-circuit fabric: the generated tunnels (one per SD-WAN office), the partner tunnel, the six DCI
# Interconnects (one per dark fibre, unowned, carrier = the fibre) and the five Handoffs (unowned shared on-ramps, all
# PROD). `carrier` is the expected derived underlay: the carrier circuits on the circuit's own ports, or on the parent
# of a sub-interface; the data declares none.
ALL_DEMO_VIRTUAL_CIRCUITS: dict[str, VirtualCircuitExpectation] = {
    # One generated tunnel per SD-WAN office (_sdwan_tunnel).
    **dict(_sdwan_tunnel(office) for office in _SDWAN_OFFICES),
    # C001's partner extranet (08_interconnects/07_zone_policies/01_partner_tunnel.yml): the gateway's public port to
    # the partner stub router, each end on its own internet access; located at C001's office and the gateway's zone.
    "C001-PARTNER-ACME-FR2": {
        "circuit_type": TUNNEL_TYPE,
        "owner": "Nordix Ltd.",
        "locations": ("C001-OFFICE", "FR2"),
        "ends": (("EQX-FR2-SDWAN-GW1", "eth1"), ("EXT-ACME-EDGE1", "et-0/0/0")),
        "carrier": ("INET-LUMEN-EQXFR2-GW", "INET-ACME-COGENT"),
        "ip_namespace": None,
        "cloud_endpoint": None,
    },
    # The DCI: ends = the fibre's two ports, locations = the fibre's two facilities.
    **{
        circuit_name: {
            "circuit_type": INTERCONNECT_TYPE,
            "owner": None,
            "locations": tuple(sorted(locations)),
            "ends": ends,
            "carrier": (fibre,),
            "ip_namespace": None,
            "cloud_endpoint": None,
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
    # The AWS transit VIF of the FR2 cage (05_cloud_endpoints/00_vif_handoff.yml): the party sub-interface rides the
    # parent et-0/0/0 of the cross-connect.
    "HANDOFF-FR2-AWS-EUC1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "owner": None,
        "locations": ("FR2", "eu-central-1"),
        "ends": (("EXT-AWS-DX-FR2", "et-0/0/0.1000"), ("eg-fr201", "Ethernet1/13.1000")),
        "carrier": ("XC-EQXFR2-AWS-EUC1",),
        "ip_namespace": "PROD",
        "cloud_endpoint": "vif-transit-hub-euc1",
    },
    # The SD-WAN gateway's LAN handoff (04_sdwan/06_vrf_handoff.yml): both ends in one cage, carried by a cable.
    "HANDOFF-FR2-SDWAN-GW1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "owner": None,
        "locations": ("FR2",),
        "ends": (("EQX-FR2-SDWAN-GW1", "eth0.1900"), ("eg-fr201", "Ethernet1/12.1900")),
        "carrier": (),
        "ip_namespace": "PROD",
        "cloud_endpoint": None,
    },
    # The AM-NE chain (03_virtual_circuits/02_am_ne_handoff.yml, 05_cloud_endpoints/00_vif_handoff_am_ne.yml): the
    # Equinix Fabric hop cage -> NE (carried by the cable to the Fabric port, the NE end needs none) and the NE ->
    # AWS hop whose party end rides the same cross-connect as the FR2 VIF.
    "HANDOFF-FR2-AMNE-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "owner": None,
        "locations": ("AM-NE", "FR2"),
        "ends": (("eg-am-ne01", "Ethernet1/1.1200"), ("eg-fr201", "Ethernet1/1.1200")),
        "carrier": (),
        "ip_namespace": "PROD",
        "cloud_endpoint": None,
    },
    "HANDOFF-AMNE-AWS-EUC1-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "owner": None,
        "locations": ("AM-NE", "eu-central-1"),
        "ends": (("EXT-AWS-DX-FR2", "et-0/0/0.1100"), ("eg-am-ne02", "Ethernet1/1.1100")),
        "carrier": ("XC-EQXFR2-AWS-EUC1",),
        "ip_namespace": "PROD",
        "cloud_endpoint": "vif-transit-am-euc1",
    },
    # The Megaport Cloud Router (05_cloud_endpoints/03_megaport_handoff.yml): the cloud-neutral example, carried by the
    # cross-connect to the MCR; no cloud endpoint. The Azure cross-connect has no handoff (idle carrier).
    "HANDOFF-PA4-MEGAPORT-PROD": {
        "circuit_type": HANDOFF_TYPE,
        "owner": None,
        "locations": ("MCR-PAR1", "PA4"),
        "ends": (("EXT-MEGAPORT-MCR1", "et-0/0/0.1300"), ("eg-pa401", "Ethernet1/1.1300")),
        "carrier": ("XC-EQXPA4-MEGAPORT",),
        "ip_namespace": "PROD",
        "cloud_endpoint": None,
    },
}


# Circuit placement (test_62 test_01/test_02). A carrier circuit terminates on two facilities: the kinds that inherit
# TopologyFacility (read from schemas/); a virtual circuit's locations are facilities too.
ALL_DEMO_FACILITY_KINDS: frozenset[str] = frozenset(
    kind for kind, definition in schema().items() if "TopologyFacility" in definition.inherit_from
)


_DECLARED_CARRIER_CIRCUITS = {str(c.value("circuit_id")): c for c in _all_demo("TopologyCarrierCircuit")}

_DECLARED_VIRTUAL_CIRCUITS = {
    name(c): c
    for circuit_type in (TUNNEL_TYPE, INTERCONNECT_TYPE, HANDOFF_TYPE)
    for c in _session_circuits(circuit_type)
}


# circuit_id / name -> the locations the 30_all data authors. A carrier circuit's other end is derived by add_circuit
# from its end devices' deployments, so the data lists only one place: the cage for the on-ramp circuits (add_circuit also
# derives the zone from the edge's deployment, so an authored cage must be that zone), the ISP for the partner's access.
ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS: dict[str, tuple[str, ...]] = {
    circuit_id: tuple(circuit.item.get("locations") or ()) for circuit_id, circuit in _DECLARED_CARRIER_CIRCUITS.items()
}

ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS: dict[str, tuple[str, ...]] = {
    circuit_name: tuple(circuit.item.get("locations") or ())
    for circuit_name, circuit in _DECLARED_VIRTUAL_CIRCUITS.items()
}


# circuit_id / name -> the (device, interface) ports the data declares in `capabilities`. A virtual circuit's underlay is
# derived from them: the carrier circuits that terminate on its own ports.
ALL_DEMO_DECLARED_PHYSICAL_PORTS: dict[str, frozenset[tuple[str, str]]] = {
    circuit_id: frozenset((str(device), str(port)) for device, port in circuit.item.get("capabilities") or ())
    for circuit_id, circuit in _DECLARED_CARRIER_CIRCUITS.items()
}

ALL_DEMO_DECLARED_VIRTUAL_PORTS: dict[str, frozenset[tuple[str, str]]] = {
    circuit_name: frozenset((str(device), str(port)) for device, port in circuit.item.get("capabilities") or ())
    for circuit_name, circuit in _DECLARED_VIRTUAL_CIRCUITS.items()
}


# Handoff circuit -> (the party device, its ASN): the ASN is the `local_as` of the ManagedBGP process the data declares on
# the party stub (EXT-AWS-DX-FR2 AS64512, EQX-FR2-SDWAN-GW1 AS65028, EXT-MEGAPORT-MCR1 AS133937), which add_virtual_circuit
# makes the session's remote process. A handoff whose both ends are devices whose processes are generated (the Network
# Edge pair, HANDOFF-FR2-AMNE-PROD) has no declared party and is absent.
_DECLARED_PROCESS_ASNS: dict[str, int] = {
    str(device): int(str(process.item["local_as"]).removeprefix("AS"))
    for process in _all_demo("ManagedBGP")
    for device in process.item.get("capabilities") or []
}

ALL_DEMO_HANDOFF_PARTY_ASNS: dict[str, tuple[str, int]] = {
    circuit_name: (party[0], _DECLARED_PROCESS_ASNS[party[0]])
    for circuit_name, party in (
        (
            name(circuit),
            [str(device) for device, _ in circuit.item["capabilities"] if str(device) in _DECLARED_PROCESS_ASNS],
        )
        for circuit in _session_circuits(HANDOFF_TYPE)
    )
    if len(party) == 1
}


def _site(deployment: Declared) -> str:
    """The DC or colocation metro a customer footprint sits in (its parent): `C001-P-FR` -> FR."""
    return str(deployment.value("parent"))


def _zone_has_firewalls(zone: Declared) -> bool:
    """Whether a colocation zone declares a firewall pair in its fabric_templates."""
    return any(template[1] == "firewall" for template in zone.item.get("fabric_templates") or [])


# Metro -> its zones that declare a firewall pair (a virtual colocation is never one). A colocation footprint is named
# after its metro (`C001-P-FR`), names no zone, and is served by every such zone of its metro: one shared context each.
def _firewall_zones_by_metro() -> dict[str, tuple[str, ...]]:
    by_metro: dict[str, list[str]] = {}
    for zone in _all_demo("TopologyColocationZone"):
        if _zone_has_firewalls(zone):
            by_metro.setdefault(str(parent_name(zone)), []).append(name(zone))
    return {metro: tuple(sorted(zones)) for metro, zones in by_metro.items()}


_FIREWALL_ZONES_BY_METRO = _firewall_zones_by_metro()


# Every 30_all colocation deployment -> the zones whose firewall pair serve it, none when its metro has no firewall
# zone (the footprint then has no context: an info log, not an error).
ALL_DEMO_COLOCATION_SERVING_ZONES: dict[str, tuple[str, ...]] = {
    name(deployment): _FIREWALL_ZONES_BY_METRO.get(_site(deployment), ())
    for deployment in _all_demo("TopologyCustomerColocation")
}


# Every 30_all colocation deployment -> whether it has a serving context: its metro has a zone with a firewall pair
# (FR2, 02_colo/equinix/fr). Footprints of a metro whose zones have no firewalls must stay unserved.
ALL_DEMO_COLOCATION_SERVED: dict[str, bool] = {
    deployment: bool(zones) for deployment, zones in ALL_DEMO_COLOCATION_SERVING_ZONES.items()
}

# Serving zone -> the deployments its shared context serves, keyed by zone name (`FR2`): the zone is the site of
# a colocation firewall context (its pair, its VLAN pool `fr2-fw-context-vlan-pool`).
ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    zone: tuple(deployment for deployment, serving in ALL_DEMO_COLOCATION_SERVING_ZONES.items() if zone in serving)
    for zone in sorted({z for zones in _FIREWALL_ZONES_BY_METRO.values() for z in zones})
}
