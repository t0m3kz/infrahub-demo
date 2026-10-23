"""Shared constants for integration tests."""

from typing import NamedTuple, TypedDict

from generators.fabric_sizing import (
    dc_loopback_bits,
    dc_management_bits,
    dc_technical_bits,
    fabric_loopback_bits,
    host_bits_to_prefix_length,
    pod_loopback_bits,
    pod_technical_bits,
)
from generators.helpers.lb_vip import cloud_lb_name, cloud_lb_scheme
from generators.helpers.naming import sdwan_edge_name
from generators.helpers.routing import underlay_pool_is_ipv6
from tests.unit.simulators.fabric_designs import dc_design, pod_design
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    PROD_TYPE,
    TENANT_NAMESPACE_TYPES,
    lb_leg_pool_name,
    lb_vip_pool_name,
    namespace_type_for_environment,
)
from utils.ports import PortProfileHelper

from .demo_data import (
    ROOT,
    Declared,
    created_dispatches,
    declared,
    distinct,
    name,
    object_templates,
    render,
    schema,
)

# ---------------------------------------------------------------------------
# Demo data paths  (single source of truth — the same files the demos ship)
# ---------------------------------------------------------------------------

DEMO_DC_DATA_ROOT = "data/demos/01_data_center"
DEMO_SWITCH_DATA = "data/demos/02_switch_dc6"
DEMO_RACK_DATA = "data/demos/03_rack_dc6"
DEMO_POD_DATA = "data/demos/04_pod_dc6"
DEMO_SERVERS_DATA = "data/demos/06_servers"

# ---------------------------------------------------------------------------
# DC6 expansion chain (test_12 - test_20) — grows the DC6 fabric deployed by
# test_10 with the DC6 expansion demos shipped in data/demos/.
# ---------------------------------------------------------------------------

SCENARIO_DC_NAME = "DC6"
SCENARIO_POD_1 = "DC6-1-POD-1"

# Test-local segment data: there is no standalone-loadable DC6 segment demo
# (data/demos/07_customers and 08_segments depend on the 30_all customer
# organizations and still use the pre-d4801531 [org_id, environment]
# customer-deployment HFID), so the scenario boards its own footprint.
SCENARIO_SEGMENT_DATA = "tests/integration/data/20_segments"
SCENARIO_SEGMENT_FOOTPRINT = "C001-P-DC6"

# Test-local application data for test_20: data/demos/06_servers declares no
# AppComponent, and a local segment's VLAN ID is realized only where a
# component's instance is cabled to a switch (add_app_component_segment), so
# the scenario declares one component on a SCENARIO_SEGMENT_DATA segment,
# instanced on one of the servers test_20 cables.
SCENARIO_ENDPOINT_APP_DATA = "tests/integration/data/20_endpoints"
SCENARIO_ENDPOINT_APPLICATION = "c001-dc6-endpoint-probe-p"
SCENARIO_ENDPOINT_COMPONENT_FQDN = "web.dc6-endpoint-probe.c001.test.local"
SCENARIO_ENDPOINT_SEGMENT = "c001-web-app-p"
SCENARIO_ENDPOINT_SERVER = "ktw-pod1-server-1"

# Every switching role a DC6 scenario can add. Counted per DC, so a role that
# never appears in DC6 just stays at 0 on both sides of the comparison.
SCENARIO_FABRIC_ROLES = ["super-spine", "spine", "leaf", "access-leaf", "l2-leaf", "tor", "border-leaf"]

# Roles whose existing devices must keep their underlay ASN while the chain
# grows DC6 (ebgp-ibgp). l2-leaf is L2-only (no BGP) so it is not listed;
# access-leaf only exists from test_14 on, so its baseline is empty before that.
SCENARIO_UNDERLAY_ROLES = ["super-spine", "spine", "leaf", "access-leaf", "tor"]

# ---------------------------------------------------------------------------
# 30_all — the everything demo (DC fabrics + colo + cloud + offices +
# customer deployments + applications + interconnects), shared by every
# test_59-test_63 module through the single branch below.
# ---------------------------------------------------------------------------

from tasks import (  # noqa: E402,F401 - re-exported for the test modules
    ALL_DEMO_BRANCH,
    ALL_DEMO_DATA,
    ALL_DEMO_LOAD_STAGES,
    ALL_DEMO_STAGE_MAX_ATTEMPTS,
    ALL_DEMO_STAGE_POLL_INTERVAL,
    ALL_DEMO_STAGE_STABLE_ZERO,
)

# What 30_all declares, read from its YAML (tests/integration/demo_data.py),
# so that boarding a customer needs no edit here.
_ALL_DEMO_ROOT = ROOT / ALL_DEMO_DATA


def _all_demo(kind: str) -> list[Declared]:
    """The distinct objects of ``kind`` 30_all declares (a re-declaration that updates one counts once)."""
    return distinct(declared(kind, _ALL_DEMO_ROOT))


# Generators the 30_all load must dispatch by itself, mapped to the minimum
# number of runs each must have. These are the event-driven runs Infrahub
# fires from data/events/99_actions.yml — not something the suite invokes —
# so a zero here means a trigger rule regressed, which is silent otherwise:
# the objects simply never get generated and no task fails. Each floor is one
# run per declared object its created-trigger watches that is in its target
# group (demo_data.created_dispatches): e.g. add_rack counts the colocation
# cage racks too (they are in topologies_rack and add_rack no-ops on them),
# add_colocation_metro every metro (its VNI and ASN pools, kit or no kit),
# add_colocation_zone every zone and add_virtual_colocation every virtual
# colocation, though only FR2, PA4 and AM-NE carry fabric_templates (the rest no-op), and
# add_sdwan_edge every office, though the generator
# no-ops on those without sdwan_gateway (add_office_fabric likewise: every office,
# though only those with fabric_templates generate); updated-triggers only add runs.
# add_customer_deployment_cloud/office were removed: their only job was
# hub-and-spoke exchange auto-provisioning, now replaced by the per-context
# TopologyRoutedExchange objects the DC/colocation customer generators
# create — see docs/exchange_gateway.md.
ALL_DEMO_EXPECTED_GENERATORS: dict[str, int] = {
    generator: created_dispatches(generator, _ALL_DEMO_ROOT)
    for generator in (
        "add_dc",
        "add_pod",
        "add_rack",
        "add_colocation_metro",
        "add_colocation_zone",
        "add_virtual_colocation",
        "add_endpoint",
        "add_vxlan_segment",
        "add_app_application",
        "add_app_dependency",
        "add_customer_deployment_dc",
        "add_customer_deployment_colocation",
        "add_sdwan_edge",
        "add_office_fabric",
        "add_application_orchestrator_routing",
        "add_app_component_segment",
        "add_customer_dc_orchestrator_routing",
        "add_customer_colocation_orchestrator_routing",
        "add_customer_cloud_orchestrator_routing",
        "add_customer_office_orchestrator_routing",
        "add_virtual_colocation_orchestrator_routing",
    )
}

# Declared-object inventory of data/demos/30_all, counted from the YAML.
# Compared with `>=` because generators legitimately add more of several of
# these kinds (racks stay declared-only, but devices, prefixes and segments
# do not) — the point is that every declared object landed.
ALL_DEMO_EXPECTED_OBJECTS: dict[str, int] = {
    kind: len(_all_demo(kind))
    for kind in (
        "TopologyDataCenter",
        "TopologyPod",
        "LocationRack",
        "TopologyCustomerDC",
        "TopologyCustomerColocation",
        "TopologyCustomerCloud",
        "TopologyCustomerOffice",
        "ManagedVxlanSegment",
        "ManagedVlanSegment",
        "AppApplication",
        "AppComponent",
        "AppDependency",
        "SecurityAccessProfile",
        "DcimVirtualDevice",
        "TopologyPhysicalCircuit",
        "TopologyTunnelCircuit",
        "TopologyInterconnectCircuit",
        "TopologyHandoffCircuit",
        "CloudInstance",
        "ManagedCloudProxy",
    )
}


# ---------------------------------------------------------------------------
# 30_all — compute layer expectations (test_61)
# ---------------------------------------------------------------------------

ALL_DEMO_DC_NAMES: tuple[str, ...] = tuple(sorted(name(dc) for dc in _all_demo("TopologyDataCenter")))

# The three fabrics are declared with identical shapes (size L, ebgp-ebgp,
# ipv6 underlay, pbr), so one expected role map covers all of them:
#   DC level : 2 super-spine + 2 border-leaf, plus the firewall and
#              load-balancer HA pairs and the dedicated virtual firewall and
#              load-balancer pairs of the DC's one customer with both
#              dedicated (see ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
#   POD level: 2 spines each, two pods
#   Racks    : two network racks x (2 leaf + 2 access-leaf)
#   Compute  : the DC's application hosts (ALL_DEMO_DC_ENDPOINTS), the one
#              role whose count differs between the DCs
ALL_DEMO_DC_ROLE_COUNTS: dict[str, int] = {
    "super-spine": 2,
    "border-leaf": 2,
    "spine": 4,
    "leaf": 4,
    "access-leaf": 4,
    "firewall": 4,
    "load-balancer": 4,
}

# Every fabric device routes the underlay; only the tiers above the VTEP layer
# carry an overlay process in this design. access-leaf/leaf deliberately have
# no overlay process — they are the L2 access side of the pbr fabric.
ALL_DEMO_DC_UNDERLAY_ROLES = ("super-spine", "spine", "leaf", "access-leaf", "border-leaf")
ALL_DEMO_DC_OVERLAY_ROLES = ("super-spine", "spine", "border-leaf")

# Application hosts in row pairs, one host of a pair per compute-rack row
# (DC10 has three pairs: C005's payment-core, its session store, C008; DC12
# two: the shared P027 non-prod pair and C005's own prod pair). The
# row matters: add_endpoint
# cables a host to the access-leaf pair of the network rack sharing its row,
# so row-1 and row-2 hosts must land on *disjoint* access-leaf pairs. That
# disjointness is the whole point of putting them in different rows, and it is
# what keeps the blast radius of one access-leaf pair bounded.
ALL_DEMO_DC_HOST_ROWS: dict[str, int] = {
    "dc10-pod1-server-1": 1,
    "dc10-pod1-server-2": 2,
    "dc10-pod1-server-3": 1,
    "dc10-pod1-server-4": 2,
    "dc10-pod1-server-5": 1,
    "dc10-pod1-server-6": 2,
    "dc11-pod1-server-1": 1,
    "dc11-pod1-server-2": 2,
    "dc12-pod1-server-1": 1,
    "dc12-pod1-server-2": 2,
    "dc12-pod1-server-3": 1,
    "dc12-pod1-server-4": 2,
}

# DC name -> how many application hosts (role endpoint) it has, from ALL_DEMO_DC_HOST_ROWS.
ALL_DEMO_DC_ENDPOINTS: dict[str, int] = {
    dc: sum(host.startswith(f"{dc.lower()}-") for host in ALL_DEMO_DC_HOST_ROWS) for dc in ALL_DEMO_DC_NAMES
}

# The colocation cages are the deliberate counter-example: both NICs of each
# host land on the *single* cage switch. NIC redundancy without switch
# redundancy — the honest small-colocation shape, and a genuine shared-fate
# case to contrast with the DC pods.
ALL_DEMO_COLO_HOST_SWITCHES: dict[str, str] = {
    "cs-ny1-server-1": "CS-NY1-SW1",
    "cs-ny1-server-2": "CS-NY1-SW1",
    "cs-va1-server-1": "CS-VA1-SW1",
    "cs-va1-server-2": "CS-VA1-SW1",
    "mcr-ams1-server-1": "MCR-AMS1-SW1",
    "mcr-ams1-server-2": "MCR-AMS1-SW1",
}

# Every host is dual-homed, DC or cage.
ALL_DEMO_HOST_LINK_COUNT = 2


def _components(app: Declared) -> list[Declared]:
    """The AppComponents declared under an application."""
    return [obj for obj in declared("AppComponent", _ALL_DEMO_ROOT) if obj.enclosing is app]


def _instances(app: Declared) -> set[str]:
    """The names of the instances an application's components run on."""
    return {instance for component in _components(app) for instance in component.item.get("instances") or []}


_ALL_DEMO_APPLICATIONS = _all_demo("AppApplication")
_ALL_DEMO_CLOUD_INSTANCES = {name(instance) for instance in _all_demo("CloudInstance")}

# Applications with no compute footprint: no component has an instance (e.g.
# c001-checkout-p, a private-access-only frontend published through an
# access-profile dependency). test_61's switch-port-to-application chain does
# not cover them, so they are not in ALL_DEMO_EXPECTED_APPLICATIONS.
ALL_DEMO_NO_COMPUTE_APPLICATIONS: tuple[str, ...] = tuple(
    sorted(name(app) for app in _ALL_DEMO_APPLICATIONS if not _instances(app))
)

# name -> (criticality, component count), criticality defaulting like the schema.
ALL_DEMO_EXPECTED_APPLICATIONS: dict[str, tuple[str, int]] = {
    name(app): (str(app.value("criticality")), len(_components(app)))
    for app in _ALL_DEMO_APPLICATIONS
    if _instances(app)
}

# Cloud-native applications: their instances are CloudInstance nodes with no
# hosting_device. Every other application's instances are DcimVirtualDevice
# nodes that MUST name the physical host they run on — that edge is what ties
# a switch port to a customer application.
ALL_DEMO_CLOUD_APPLICATIONS: tuple[str, ...] = tuple(
    sorted(
        name(app) for app in _ALL_DEMO_APPLICATIONS if _instances(app) and _instances(app) <= _ALL_DEMO_CLOUD_INSTANCES
    )
)

# Applications on the branch that test_61's application-graph check does not
# cover: the no-compute ones above, and test_20's DC6 probe application, which
# a full suite run merges to main before test_59 branches the 30_all scenario
# off it (a single DC6 server instance, no HA pair, no hosting_device).
ALL_DEMO_OUT_OF_SCOPE_APPLICATIONS = (*ALL_DEMO_NO_COMPUTE_APPLICATIONS, SCENARIO_ENDPOINT_APPLICATION)

# Each component is deployed as an HA pair.
ALL_DEMO_COMPONENT_INSTANCE_COUNT = 2

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

# Physical circuits are pure carriers, all declared in the data except the office underlays: the dark fibres and
# cross-connects of 08_interconnects/01_colo_onramp (each DC has one fibre to each of its metro's two border gateways:
# DC10/DC11 -> EQX FR2 (eg-fr201 and eg-fr202), DC12 -> EQX PA4 (eg-pa401 and eg-pa402); cross-connects: AWS
# eu-central-1, Azure westeurope, Megaport MCR), the shared transit circuits of 06_internet, the shared gateway's own
# access (08_gateway_access) and the partner's access (07_zone_policies), plus one generated underlay per SD-WAN office.
_DECLARED_CIRCUIT_TYPE_COUNTS: dict[str, int] = {
    circuit_type: len([c for c in _all_demo("TopologyPhysicalCircuit") if c.value("circuit_type") == circuit_type])
    for circuit_type in ("dark_fiber", "cross_connect", "internet")
}
ALL_DEMO_PHYSICAL_CIRCUIT_TYPES: dict[str, int] = {
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
    for circuit in _all_demo("TopologyInterconnectCircuit")
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
    """What one virtual circuit (a Tunnel, Interconnect or Handoff kind) in 30_all must look like."""

    #: TopologyTunnelCircuit, TopologyInterconnectCircuit or TopologyHandoffCircuit: the role IS the kind.
    kind: str
    #: The owning customer's name; None for the shared on-ramp circuits that belong to no single customer.
    owner: str | None
    #: The circuit's locations, sorted. Authored for a declared circuit, `(office, the gateway's zone)` for the tunnel
    #: add_sdwan_edge builds.
    locations: tuple[str, ...]
    #: The (device, interface) ends in `capabilities`, sorted. A Handoff end is the `<port>.<vlan>` sub-interface, a
    #: Tunnel's the gateway's public port and the other device's uplink.
    ends: tuple[tuple[str, str], ...]
    #: circuit_ids of the physical circuits derived from the ends (on the end port, or on its parent interface when it
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
TUNNEL_KIND = "TopologyTunnelCircuit"
INTERCONNECT_KIND = "TopologyInterconnectCircuit"
HANDOFF_KIND = "TopologyHandoffCircuit"


def _sdwan_tunnel(office: Declared) -> tuple[str, VirtualCircuitExpectation]:
    """The tunnel add_sdwan_edge builds for one opted-in office, named as the generator names it.

    `<office>-SDWAN-<the gateway's zone>`, a TopologyTunnelCircuit owned by the office's customer, located at the
    office and the gateway's zone, ending on the gateway's public port and the edge uplink. Each end rides its own
    internet access: the edge uplink `INET-<office>`, the gateway's port the shared `INET-LUMEN-EQXFR2-GW`.
    """
    office_name = name(office)
    gateway = str(office.item["sdwan_gateway"])
    zone = _DEVICE_DEPLOYMENTS[gateway]
    return f"{office_name}-SDWAN-{zone}", {
        "kind": TUNNEL_KIND,
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
# PROD). `carrier` is the expected derived underlay: the physical circuits on the circuit's own ports, or on the parent
# of a sub-interface; the data declares none.
ALL_DEMO_VIRTUAL_CIRCUITS: dict[str, VirtualCircuitExpectation] = {
    # One generated tunnel per SD-WAN office (_sdwan_tunnel).
    **dict(_sdwan_tunnel(office) for office in _SDWAN_OFFICES),
    # C001's partner extranet (08_interconnects/07_zone_policies/01_partner_tunnel.yml): the gateway's public port to
    # the partner stub router, each end on its own internet access; located at C001's office and the gateway's zone.
    "C001-PARTNER-ACME-FR2": {
        "kind": TUNNEL_KIND,
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
            "kind": INTERCONNECT_KIND,
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
        "kind": HANDOFF_KIND,
        "owner": None,
        "locations": ("FR2", "eu-central-1"),
        "ends": (("EXT-AWS-DX-FR2", "et-0/0/0.1000"), ("eg-fr201", "Ethernet1/13.1000")),
        "carrier": ("XC-EQXFR2-AWS-EUC1",),
        "ip_namespace": "PROD",
        "cloud_endpoint": "vif-transit-hub-euc1",
    },
    # The SD-WAN gateway's LAN handoff (04_sdwan/06_vrf_handoff.yml): both ends in one cage, carried by a cable.
    "HANDOFF-FR2-SDWAN-GW1-PROD": {
        "kind": HANDOFF_KIND,
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
        "kind": HANDOFF_KIND,
        "owner": None,
        "locations": ("AM-NE", "FR2"),
        "ends": (("eg-am-ne01", "Ethernet1/1.1200"), ("eg-fr201", "Ethernet1/1.1200")),
        "carrier": (),
        "ip_namespace": "PROD",
        "cloud_endpoint": None,
    },
    "HANDOFF-AMNE-AWS-EUC1-PROD": {
        "kind": HANDOFF_KIND,
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
        "kind": HANDOFF_KIND,
        "owner": None,
        "locations": ("MCR-PAR1", "PA4"),
        "ends": (("EXT-MEGAPORT-MCR1", "et-0/0/0.1300"), ("eg-pa401", "Ethernet1/1.1300")),
        "carrier": ("XC-EQXPA4-MEGAPORT",),
        "ip_namespace": "PROD",
        "cloud_endpoint": None,
    },
}

# Circuit placement (test_62 test_01/test_02). A physical circuit terminates on two facilities: the kinds that inherit
# TopologyFacility (read from schemas/); a virtual circuit's locations are facilities too.
ALL_DEMO_FACILITY_KINDS: frozenset[str] = frozenset(
    kind for kind, definition in schema().items() if "TopologyFacility" in definition.inherit_from
)

_DECLARED_PHYSICAL_CIRCUITS = {str(c.value("circuit_id")): c for c in _all_demo("TopologyPhysicalCircuit")}
_DECLARED_VIRTUAL_CIRCUITS = {
    name(c): c for kind in (TUNNEL_KIND, INTERCONNECT_KIND, HANDOFF_KIND) for c in _all_demo(kind)
}

# circuit_id / name -> the locations the 30_all data authors. A physical circuit's other end is derived by add_circuit
# from its end devices' deployments, so the data lists only one place: the cage for the on-ramp circuits (add_circuit also
# derives the zone from the edge's deployment, so an authored cage must be that zone), the ISP for the partner's access.
ALL_DEMO_DECLARED_PHYSICAL_LOCATIONS: dict[str, tuple[str, ...]] = {
    circuit_id: tuple(circuit.item.get("locations") or ())
    for circuit_id, circuit in _DECLARED_PHYSICAL_CIRCUITS.items()
}
ALL_DEMO_DECLARED_VIRTUAL_LOCATIONS: dict[str, tuple[str, ...]] = {
    circuit_name: tuple(circuit.item.get("locations") or ())
    for circuit_name, circuit in _DECLARED_VIRTUAL_CIRCUITS.items()
}

# circuit_id / name -> the (device, interface) ports the data declares in `capabilities`. A virtual circuit's underlay is
# derived from them: the physical circuits that terminate on its own ports.
ALL_DEMO_DECLARED_PHYSICAL_PORTS: dict[str, frozenset[tuple[str, str]]] = {
    circuit_id: frozenset((str(device), str(port)) for device, port in circuit.item.get("capabilities") or ())
    for circuit_id, circuit in _DECLARED_PHYSICAL_CIRCUITS.items()
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
        for circuit in _all_demo(HANDOFF_KIND)
    )
    if len(party) == 1
}


def _site(deployment: Declared) -> str:
    """The DC or colocation metro a customer footprint sits in (its parent): `C001-P-FR` -> FR."""
    return str(deployment.value("parent"))


def _zone_has_firewalls(zone: Declared) -> bool:
    """Whether a colocation zone declares a firewall pair in its fabric_templates."""
    return any(template[1] == "firewall" for template in zone.item.get("fabric_templates") or [])


_FIREWALL_ZONES = {name(zone) for zone in _all_demo("TopologyColocationZone") if _zone_has_firewalls(zone)}

# Every 30_all colocation deployment -> the zone whose firewall pair serves it (its serving_zone), None when unset
# (the footprint then has no context: an info log, not an error). A footprint is named after its metro
# (`C001-P-FR`), the zone is a child of that metro.
ALL_DEMO_COLOCATION_SERVING_ZONE: dict[str, str | None] = {
    name(deployment): (str(deployment.item["serving_zone"]) if deployment.item.get("serving_zone") else None)
    for deployment in _all_demo("TopologyCustomerColocation")
}

# Every 30_all colocation deployment -> whether it has a serving context: its serving_zone declares a firewall
# pair (FR2, 02_colo/equinix/fr). Footprints with no serving_zone, or a zone without firewalls, must stay unserved.
ALL_DEMO_COLOCATION_SERVED: dict[str, bool] = {
    deployment: zone in _FIREWALL_ZONES for deployment, zone in ALL_DEMO_COLOCATION_SERVING_ZONE.items()
}
# Serving zone -> the deployments its shared context serves, keyed by zone name (`FR2`): the zone is the site of
# a colocation firewall context (its pair, its VLAN pool `fr2-fw-context-vlan-pool`).
ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    zone: tuple(
        deployment
        for deployment, serving in ALL_DEMO_COLOCATION_SERVING_ZONE.items()
        if serving == zone and ALL_DEMO_COLOCATION_SERVED[deployment]
    )
    for zone in sorted({z for z in ALL_DEMO_COLOCATION_SERVING_ZONE.values() if z in _FIREWALL_ZONES})
}
# One shared (tenant-less) firewall context per firewall cluster: one per DC
# and one per serving colocation zone, whose pair every footprint naming that
# serving_zone shares (a colocation has no design, so never a dedicated
# context). The colocation context carries transit legs like a DC one (its
# legs face the zone's two edges, whose firewall ports are tagged with the
# context) ...
ALL_DEMO_SHARED_FIREWALL_CONTEXTS = len(ALL_DEMO_DC_NAMES) + len(ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS)

_BOOTSTRAP_ROOT = ROOT / "data" / "bootstrap"


def _dedicated_tenants(flag: str) -> dict[str, str]:
    """Tenant deployment -> design, for every DC footprint whose design blueprint sets ``flag``."""
    designs = {name(design) for design in declared("TopologyCustomerTemplateDC", _BOOTSTRAP_ROOT) if design.value(flag)}
    return {
        name(deployment): str(deployment.item["design"])
        for deployment in _all_demo("TopologyCustomerDC")
        if deployment.item.get("design") in designs
    }


# ... plus one dedicated context per customer whose design blueprint sets
# dedicated_firewall (data/bootstrap/21_customer_templates.yml: L_DC and
# XL_DC do, S_DC and M_DC do not). Maps tenant deployment -> design.
ALL_DEMO_DEDICATED_FIREWALL_TENANTS: dict[str, str] = _dedicated_tenants("dedicated_firewall")

# ---------------------------------------------------------------------------
# 30_all — internet egress rules (07_zone_policies/05_security_policy.yml)
#
# A hand-written permit from a tenant segment to the Internet segment
# (data/bootstrap/27_external_segments.yml: no prefix, INTERNET-ZONE). The
# firewall renders it to INTERNET-ZONE with destination any; the source leaf's
# ACL denies every NON_INTERNET_PREFIXES range before the permit to any.
# ---------------------------------------------------------------------------


class InternetEgressRule(TypedDict):
    """One permit to the Internet segment."""

    #: DC whose shared firewall context serves `source_segment`.
    dc: str
    source_segment: str
    destination_segment: str
    destination_zone: str
    protocol: str
    port: int


ALL_DEMO_INTERNET_EGRESS_RULES: dict[str, InternetEgressRule] = {
    "nordix-prod-to-internet-https": {
        "dc": "DC11",
        "source_segment": "c006-nordix-prod-p",
        "destination_segment": "internet",
        "destination_zone": "INTERNET-ZONE",
        "protocol": "tcp",
        "port": 443,
    },
}

# ---------------------------------------------------------------------------
# 30_all — inter-VRF exchange legs through the firewall contexts (PR 3)
#
# Everything below is DERIVED, not hand-counted. A context's legs are its
# tenant VRFs plus INTERNET and MANAGEMENT (EXCHANGE_PEERS): a dedicated context serves its one
# tenant, a shared context serves every customer deployment of its DC that has
# no dedicated context. A serving colocation zone
# (ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS, FR) has one shared context serving
# all its deployments, on the same rules. A deployment's VRF follows its
# environment (`p` is PROD, every other value NON-PROD); an office has no
# environment and is always PROD (OFFICE_NAMESPACE). The deployments are
# the TopologyCustomerDC footprints 30_all declares (06_customer_boarding,
# <customer>/dc/01_deployments.yml).
# ---------------------------------------------------------------------------

ALL_DEMO_TRANSIT_NAMESPACES = ("PROD", "NON-PROD", "INTERNET", "MANAGEMENT")
_TRANSIT_NAMESPACE_BY_TYPE: dict[str, str] = dict(
    zip(("prod", "non_prod", "internet", "management"), ALL_DEMO_TRANSIT_NAMESPACES)
)

# Deployment name ({org}-{environment}-{DC}) of every TopologyCustomerDC, per DC.
ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    dc: tuple(sorted(name(d) for d in _all_demo("TopologyCustomerDC") if _site(d) == dc)) for dc in ALL_DEMO_DC_NAMES
}

# The bootstrap exchanges of the removed data/bootstrap/23_exchanges.yml. The
# gateway is mandatory now, so every exchange is created per context.
ALL_DEMO_LEGACY_EXCHANGES = ("PROD-INTERNET", "NON-PROD-INTERNET", "PROD-MANAGEMENT", "NON-PROD-MANAGEMENT")

# Resource prefixes of the FW-Transit-<VRF>-IPv4 pools (data/bootstrap/20_dci_pools.yml).
ALL_DEMO_TRANSIT_RESOURCES: dict[str, str] = {
    "PROD": "100.66.0.0/20",
    "NON-PROD": "100.66.16.0/20",
    "INTERNET": "100.66.32.0/20",
    "MANAGEMENT": "100.66.48.0/20",
}

MANAGEMENT_NAMESPACE = "MANAGEMENT"
# DC11's core-services segment (07_applications/infra/00_segment.yml): INFRA-owned, in VRF MANAGEMENT,
# pinned to al-dc1111111501 Ethernet1/2, reached by the c006 udp/53 and udp/123 rules (05_security_policy.yml).
MANAGEMENT_SERVICES_PREFIX = "10.250.0.0/24"
MANAGEMENT_SERVICES_GATEWAY = "10.250.0.1/24"
MANAGEMENT_SERVICES_VLAN = 250
MANAGEMENT_SERVICES_LEAF = "al-dc1111111501"
MANAGEMENT_SERVICES_DC = "DC11"
# rule name -> (protocol, port), the DC11 permits PROD-ZONE -> MANAGEMENT-ZONE.
ALL_DEMO_MANAGEMENT_RULES: dict[str, tuple[str, int]] = {
    "nordix-prod-to-core-dns": ("udp", 53),
    "nordix-prod-to-core-ntp": ("udp", 123),
}
# The tenant segment those rules start from, read from 05_security_policy.yml.
# Its prefix and its local VLAN on each switch are derived (add_vxlan_segment:
# a /24 from its environment's tenant pool, one VLAN per VLAN domain), so
# test_64 reads both from the graph.
# Exactly one (the unpacking raises otherwise).
(MANAGEMENT_CLIENT_SEGMENT,) = sorted(
    {
        str(rule.item["source_segment"])
        for rule in declared("SecurityPolicyRule", _ALL_DEMO_ROOT)
        if rule.item.get("name") in ALL_DEMO_MANAGEMENT_RULES
    }
)

# A firewall cluster is an HA pair: one sub-interface per member per leg.
ALL_DEMO_FIREWALL_MEMBERS = 2


class ExpectedTransitContext(TypedDict):
    """What one firewall context of a 30_all site (a DC, or a serving colocation zone) must carry."""

    #: The DC (`DC10`) or the serving colocation zone (`FR2`) the context's firewalls sit in.
    site: str
    #: Tenant deployment of a dedicated context, None for the DC's shared one.
    tenant: str | None
    #: Deployments the context serves.
    deployments: tuple[str, ...]
    #: Namespace names with a leg, in ALL_DEMO_TRANSIT_NAMESPACES order.
    namespaces: tuple[str, ...]
    #: Exchange name suffixes `{A}-{Z}` (the exchange is `{context}-{A}-{Z}`).
    exchanges: tuple[str, ...]


OFFICE_NAMESPACE = _TRANSIT_NAMESPACE_BY_TYPE[PROD_TYPE]
"""The tenant VRF of every office: it has no environment, an office is always PROD."""


def deployment_environment(deployment: str) -> str:
    """`C008-D-DC12` -> `d`: the environment segment of a DC or colocation deployment name (not an office's)."""
    return deployment.split("-")[1].lower()


def deployment_namespace(deployment: str) -> str:
    """The tenant VRF namespace name of a deployment (`p` -> PROD, anything else NON-PROD)."""
    return _TRANSIT_NAMESPACE_BY_TYPE[namespace_type_for_environment(deployment_environment(deployment))]


def transit_namespace_type(namespace_name: str) -> str:
    """`NON-PROD` -> `non_prod`."""
    return next(ns_type for ns_type, name in _TRANSIT_NAMESPACE_BY_TYPE.items() if name == namespace_name)


def _expected_transit_context(site: str, tenant: str | None, deployments: tuple[str, ...]) -> ExpectedTransitContext:
    tenant_types = {namespace_type_for_environment(deployment_environment(d)) for d in deployments}
    exchanges = [
        f"{_TRANSIT_NAMESPACE_BY_TYPE[tenant_type]}-{_TRANSIT_NAMESPACE_BY_TYPE[peer]}"
        for tenant_type in _TRANSIT_NAMESPACE_BY_TYPE
        if tenant_type in tenant_types
        for peer in EXCHANGE_PEERS[tenant_type]
    ]
    peer_types = {peer for tenant_type in tenant_types for peer in EXCHANGE_PEERS[tenant_type]}
    namespaces = tuple(
        name for ns_type, name in _TRANSIT_NAMESPACE_BY_TYPE.items() if ns_type in tenant_types | peer_types
    )
    return {
        "site": site,
        "tenant": tenant,
        "deployments": deployments,
        "namespaces": namespaces,
        "exchanges": tuple(exchanges),
    }


def expected_transit_contexts() -> list[ExpectedTransitContext]:
    """The shared context of every 30_all DC, then its dedicated ones (ALL_DEMO_DEDICATED_FIREWALL_TENANTS),
    then the shared context of every serving colocation zone (never a dedicated one: no design)."""
    contexts: list[ExpectedTransitContext] = []
    for dc, deployments in ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS.items():
        dedicated = tuple(d for d in deployments if d in ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
        shared = tuple(d for d in deployments if d not in ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
        contexts.append(_expected_transit_context(dc, None, shared))
        contexts.extend(_expected_transit_context(dc, tenant, (tenant,)) for tenant in dedicated)
    for site, deployments in ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS.items():
        contexts.append(_expected_transit_context(site, None, deployments))
    return contexts


ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS = expected_transit_contexts()
# PR 4d: every context with an INTERNET leg owns one SNAT /32 (FW-SNAT-INTERNET-IPv4,
# data/bootstrap/23_transit_pools.yml). EXCHANGE_PEERS gives every tenant VRF an INTERNET
# leg, so that is every context, the serving colocation zone's included.
ALL_DEMO_SNAT_POOL_PREFIX = "192.0.2.0/24"
ALL_DEMO_SNAT_ADDRESSES = sum("INTERNET" in c["namespaces"] for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
# DC10 3+3, DC11 3+3, DC12 4+3, FR2 3 legs.
ALL_DEMO_TRANSIT_LEGS = sum(len(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
ALL_DEMO_TRANSIT_EXCHANGES = sum(len(c["exchanges"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
# One /29 per leg, so the /29 count per VRF is the leg count per VRF.
ALL_DEMO_TRANSIT_PREFIXES: dict[str, int] = {
    name: sum(name in c["namespaces"] for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
    for name in ALL_DEMO_TRANSIT_NAMESPACES
}
ALL_DEMO_TRANSIT_SUBINTERFACES = ALL_DEMO_TRANSIT_LEGS * ALL_DEMO_FIREWALL_MEMBERS
# Each site's member uplinks reach 2 service ports (dedicated virtual members follow
# their hosting firewall): a DC's border leafs, a serving colocation zone's two edges
# (firewall-role ports Ethernet1/15-16). Every context of the site tags both.
ALL_DEMO_BORDER_FIREWALL_PORTS = (
    len(ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS) + len(ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS)
) * ALL_DEMO_FIREWALL_MEMBERS

# Declared nowhere: the customer boarding generators create them, so the floors
# join ALL_DEMO_EXPECTED_OBJECTS here. Contexts = the shared ones + one per
# dedicated tenant; `>=` because the DC6 chain's own boarding merges into main.
ALL_DEMO_EXPECTED_OBJECTS["ManagedFirewallContext"] = ALL_DEMO_SHARED_FIREWALL_CONTEXTS + len(
    ALL_DEMO_DEDICATED_FIREWALL_TENANTS
)
ALL_DEMO_EXPECTED_OBJECTS["TopologyRoutedExchange"] = ALL_DEMO_TRANSIT_EXCHANGES

# ---------------------------------------------------------------------------
# 30_all — load-balancer contexts and VIPs (generators/firewall_context.py
# LOADBALANCER_CONTEXT, generators/lb_vip.py)
#
# A DC customer footprint boards onto a load-balancer context too: its design's
# dedicated one (dedicated_loadbalancer, on its own virtual pair, named
# `{cluster}-context`), else the shared one of its tenant namespace on the DC's
# cluster (`{cluster}-{NAMESPACE}`), created by the first footprint of that
# namespace. One-arm: one /29 leg in the tenant VRF (LB-Leg-<NS>-IPv4), a
# routed VIP /26 (LB-VIP-<NS>-IPv4). A colocation zone has none. A
# load_balanced AppComponent gets one VIP per port on the context serving the
# deployment of its instances; a dependency into it is ruled to the VIP
# (R_client), the context's leg to the component's segment (R_server).
# Everything is derived from the 30_all and bootstrap YAML.
# ---------------------------------------------------------------------------

ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS: dict[str, str] = _dedicated_tenants("dedicated_loadbalancer")

#: (DC, tenant namespace name, dedicated tenant deployment or None for the shared context).
LoadbalancerContextKey = tuple[str, str, str | None]

_DC_FOOTPRINT_SITES = {
    deployment: dc for dc, deployments in ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS.items() for deployment in deployments
}


def loadbalancer_context_key(deployment: str) -> LoadbalancerContextKey:
    """The key of the load-balancer context serving a DC footprint (`C010-D-DC12` -> (DC12, NON-PROD, None))."""
    tenant = deployment if deployment in ALL_DEMO_DEDICATED_LOADBALANCER_TENANTS else None
    return _DC_FOOTPRINT_SITES[deployment], deployment_namespace(deployment), tenant


def _loadbalancer_contexts() -> dict[LoadbalancerContextKey, tuple[str, ...]]:
    served: dict[LoadbalancerContextKey, list[str]] = {}
    for deployment in _DC_FOOTPRINT_SITES:
        served.setdefault(loadbalancer_context_key(deployment), []).append(deployment)
    return {key: tuple(sorted(deployments)) for key, deployments in served.items()}


# Every load-balancer context 30_all must hold -> the footprints it serves.
ALL_DEMO_LOADBALANCER_CONTEXTS: dict[LoadbalancerContextKey, tuple[str, ...]] = _loadbalancer_contexts()
ALL_DEMO_SHARED_LOADBALANCER_CONTEXTS = sum(tenant is None for _, _, tenant in ALL_DEMO_LOADBALANCER_CONTEXTS)
ALL_DEMO_EXPECTED_OBJECTS["ManagedLoadbalancerContext"] = len(ALL_DEMO_LOADBALANCER_CONTEXTS)

# A load-balancer cluster is an HA pair: one leg sub-interface and one tagged border port per member.
ALL_DEMO_LOADBALANCER_MEMBERS = 2

_TENANT_NAMESPACES = tuple(
    ns for ns_type, ns in _TRANSIT_NAMESPACE_BY_TYPE.items() if ns_type in TENANT_NAMESPACE_TYPES
)
_BOOTSTRAP_PREFIX_POOLS = {name(pool): pool for pool in declared("CoreIPPrefixPool", _BOOTSTRAP_ROOT)}


def _pool_resource(pool_name: str) -> str:
    """The one resource prefix of a bootstrap prefix pool (data/bootstrap/23_transit_pools.yml)."""
    (resource,) = _BOOTSTRAP_PREFIX_POOLS[pool_name].item["resources"]["data"]
    return str(resource["prefix"])


# Tenant namespace -> the resource of its leg /29 pool and of its VIP /26 pool.
ALL_DEMO_LB_LEG_RESOURCES: dict[str, str] = {ns: _pool_resource(lb_leg_pool_name(ns)) for ns in _TENANT_NAMESPACES}
ALL_DEMO_LB_VIP_RESOURCES: dict[str, str] = {ns: _pool_resource(lb_vip_pool_name(ns)) for ns in _TENANT_NAMESPACES}

# DC -> the platform of its load-balancer pair (its fabric_templates' load-balancer template).
ALL_DEMO_DC_LOADBALANCER_PLATFORMS: dict[str, str] = {
    name(dc): str(object_templates()[template]["platform"])
    for dc in _all_demo("TopologyDataCenter")
    for _quantity, role, template in dc.item.get("fabric_templates") or []
    if role == "load-balancer"
}

# Namespace name -> its L3 VNI (data/bootstrap): the F5 route-domain id and the Linux VRF table of a context.
ALL_DEMO_NAMESPACE_L3_VNI: dict[str, int] = {
    name(namespace): int(namespace.item["l3_vni"]) for namespace in declared("IpamNamespace", _BOOTSTRAP_ROOT)
}


class LoadBalancedComponent(TypedDict):
    """What one load_balanced AppComponent of 30_all must be fronted by."""

    #: Its network segment: R_server's destination, the pool members' prefix.
    segment: str
    #: (protocol, port) of every port it lists: one VIP each.
    ports: tuple[tuple[str, int], ...]
    #: The contexts serving the deployments of its instances: one VIP per port on each.
    contexts: tuple[LoadbalancerContextKey, ...]


_INSTANCE_DEPLOYMENTS = {
    name(device): str(device.item["deployment"])
    for device in _all_demo("DcimVirtualDevice")
    if device.item.get("deployment")
}


def _single_ports(specs: list[str]) -> tuple[tuple[str, int], ...]:
    """`["tcp/443"]` -> `(("tcp", 443),)`; a range has no VIP (the generator refuses it)."""
    return tuple(
        (protocol, start) for protocol, start, end in map(PortProfileHelper.parse_port_spec, specs) if end is None
    )


_CLOUD_INSTANCES = {name(instance): instance.item for instance in _all_demo("CloudInstance")}


def _on_cloud(component: Declared) -> bool:
    """Every instance of the component is a CloudInstance (generators.helpers.lb_vip.is_cloud_component)."""
    instances = component.item.get("instances") or []
    return bool(instances) and all(instance in _CLOUD_INSTANCES for instance in instances)


_LOAD_BALANCED = [component for component in _all_demo("AppComponent") if component.item.get("load_balanced")]

# fqdn -> expectation, for every on-prem component 30_all declares
# load_balanced (c005 web-frontend on DC10's shared PROD context, c010
# portal-ui on DC12's shared NON-PROD one).
ALL_DEMO_LOAD_BALANCED_COMPONENTS: dict[str, LoadBalancedComponent] = {
    str(component.item["fqdn"]): {
        "segment": str(component.item["network_segment"]),
        "ports": _single_ports(component.item["ports"]),
        "contexts": tuple(
            sorted(
                {loadbalancer_context_key(_INSTANCE_DEPLOYMENTS[i]) for i in component.item.get("instances") or []},
                key=repr,
            )
        ),
    }
    for component in _LOAD_BALANCED
    if not _on_cloud(component)
}


class CloudLoadBalancedComponent(TypedDict):
    """What one load_balanced AppComponent of 30_all on cloud instances must be fronted by."""

    #: Its CloudLoadBalancer, the one load balancer of all its VIPs.
    load_balancer: str
    #: The virtual network of its instances' subnets.
    virtual_network: str
    #: Its instances' subnets: the load balancer's network_segments.
    segments: tuple[str, ...]
    #: internet_facing for an internet_exposed application, else internal.
    scheme: str
    #: (protocol, port) of every port it lists: one VIP each, without an address.
    ports: tuple[tuple[str, int], ...]
    #: The instances' private_ip: the pool members of every VIP.
    members: tuple[str, ...]


_CLOUD_SEGMENT_NETWORKS = {
    name(segment): str(segment.item["virtual_network"]) for segment in _all_demo("CloudNetworkSegment")
}


def _cloud_load_balanced(component: Declared) -> CloudLoadBalancedComponent:
    instances = [_CLOUD_INSTANCES[instance] for instance in component.item["instances"]]
    segments = tuple(sorted({str(instance["network_segment"]) for instance in instances}))
    application = component.enclosing.item if component.enclosing else {}
    return {
        "load_balancer": cloud_lb_name({"fqdn": component.item["fqdn"]}),
        "virtual_network": _CLOUD_SEGMENT_NETWORKS[segments[0]],
        "segments": segments,
        "scheme": cloud_lb_scheme(application.get("security_profile")),
        "ports": _single_ports(component.item["ports"]),
        "members": tuple(sorted(str(instance["private_ip"]) for instance in instances)),
    }


# fqdn -> expectation, for every component on cloud instances 30_all declares
# load_balanced (c003 api-frontend, on AWS).
ALL_DEMO_CLOUD_LOAD_BALANCED_COMPONENTS: dict[str, CloudLoadBalancedComponent] = {
    str(component.item["fqdn"]): _cloud_load_balanced(component) for component in _LOAD_BALANCED if _on_cloud(component)
}


class LoadBalancedClient(TypedDict):
    """A dependency into a load_balanced component: ruled to its VIPs (R_client), never to its segment."""

    target: str
    #: The source component's segment, whose policy holds R_client.
    source_segment: str
    #: (protocol, port) the dependency opens: its own ports, else every port of the target.
    ports: tuple[tuple[str, int], ...]


_COMPONENTS_BY_FQDN = {str(component.item.get("fqdn")): component for component in _all_demo("AppComponent")}

# dependency name -> its R_client expectation (c008's trade portal -> C005's payment API).
ALL_DEMO_LB_CLIENT_DEPENDENCIES: dict[str, LoadBalancedClient] = {
    str(dependency.item["name"]): {
        "target": str(dependency.item["target"]),
        "source_segment": str(_COMPONENTS_BY_FQDN[str(dependency.item["source"])].item["network_segment"]),
        "ports": _single_ports(
            dependency.item.get("ports") or _COMPONENTS_BY_FQDN[str(dependency.item["target"])].item["ports"]
        ),
    }
    for dependency in _all_demo("AppDependency")
    if str(dependency.item.get("target")) in ALL_DEMO_LOAD_BALANCED_COMPONENTS
}

# Every VXLAN segment -> the sites (DC, colocation metro or office) of the
# deployments it is activated in: one ManagedSegmentDeployment record each
# (a dc_pair "stretch" segment has two DC legs; C005's colo-services-stretch
# one DC leg plus one colocation-metro leg, FR, reached over DF-DC10-EQXFR2
# through the EVPN Multi-Site border gateways; C005's office-lan one leg, the
# office itself: an office hosts its own segments, it has no site above it).
_FOOTPRINT_SITES = {
    **{
        name(deployment): _site(deployment)
        for kind in ("TopologyCustomerDC", "TopologyCustomerColocation")
        for deployment in _all_demo(kind)
    },
    **{name(office): name(office) for office in _all_demo("TopologyCustomerOffice")},
}
ALL_DEMO_SEGMENT_LEGS: dict[str, tuple[str, ...]] = {
    name(segment): tuple(_FOOTPRINT_SITES[deployment] for deployment in segment.item.get("customer_deployments") or [])
    for segment in _all_demo("ManagedVxlanSegment")
}

# ---------------------------------------------------------------------------
# Fabric designs — the slice every fabric pool must have
#
# A DC's and a pod's pools are sized from their designs' envelope
# (generators/fabric_sizing.py) in the address family the DC's underlay_protocol
# gives each pool (generators/helpers/routing.underlay_pool_is_ipv6). The design
# rows are data/bootstrap/14b_fabric_templates.yml, read through the unit-test
# simulator, so a design change needs no edit here.
# ---------------------------------------------------------------------------

# A DC with no underlay_protocol is IPv6 (the schema default, and the generators' fallback).
DEFAULT_UNDERLAY_PROTOCOL = "ipv6"


class FabricSlice(NamedTuple):
    """The address family and prefix length one fabric pool's slice must have."""

    version: int
    prefix_length: int

    def __str__(self) -> str:
        return f"IPv{self.version} /{self.prefix_length}"


def _fabric_slice(host_bits: int, underlay_protocol: str, pool: str) -> FabricSlice:
    """The slice of a ``pool`` ("technical", "loopback" or "management") ``host_bits`` wide."""
    ipv6 = underlay_pool_is_ipv6(underlay_protocol, pool)
    return FabricSlice(6 if ipv6 else 4, host_bits_to_prefix_length(host_bits, ipv6=ipv6))


def expected_dc_pool_slices(design: str, underlay_protocol: str) -> dict[str, FabricSlice | None]:
    """A DC's pools for its bootstrap ``design``: technical, loopback, management and the fabric
    loopback (the DC-tier pool inside the loopback slice; None when the design has no DC tier).

    Raises KeyError for a design the bootstrap does not declare."""
    dc = dc_design(design)
    tier_bits = fabric_loopback_bits(dc)
    return {
        "technical": _fabric_slice(dc_technical_bits(dc), underlay_protocol, "technical"),
        "loopback": _fabric_slice(dc_loopback_bits(dc), underlay_protocol, "loopback"),
        "management": _fabric_slice(dc_management_bits(dc), underlay_protocol, "management"),
        "fabric_loopback": None if tier_bits is None else _fabric_slice(tier_bits, underlay_protocol, "loopback"),
    }


def expected_pod_pool_slices(
    dc_design_name: str, pod_design_name: str, underlay_protocol: str
) -> dict[str, FabricSlice]:
    """A pod's technical and loopback pools for its bootstrap design in its DC's bootstrap design.

    Raises KeyError for a design the bootstrap does not declare."""
    dc, pod = dc_design(dc_design_name), pod_design(pod_design_name)
    return {
        "technical": _fabric_slice(pod_technical_bits(pod, dc), underlay_protocol, "technical"),
        "loopback": _fabric_slice(pod_loopback_bits(pod), underlay_protocol, "loopback"),
    }


# Timeout and polling constants
REPO_SYNC_MAX_ATTEMPTS = 60
REPO_SYNC_POLL_INTERVAL = 5  # seconds
GENERATOR_TASK_TIMEOUT = 1800  # 30 minutes
DIFF_TASK_TIMEOUT = 600  # 10 minutes
MERGE_TASK_TIMEOUT = 600  # 10 minutes
# Consecutive empty polls (5s apart) of main's task queue before a merge's
# post-merge work counts as done — triggers schedule it a few seconds late.
POST_MERGE_STABLE_ZERO = 3
VALIDATION_MAX_ATTEMPTS = 30
VALIDATION_POLL_INTERVAL = 10  # seconds
DATA_PROPAGATION_DELAY = 3  # seconds

# CoreNodeTriggerRule nodes (loaded by test_03_load_events) exist in the graph
# as soon as `infrahubctl object load` returns, but the underlying Prefect
# automations that actually listen for created/updated events are registered
# separately and asynchronously by a background worker — confirmed live at a
# consistent 12-20s lag behind the object-load command completing. A DC bulk
# load that starts before that registration finishes has its pods'/racks'
# created events fired into a system with nothing listening yet, silently
# dropping them (no error — the objects just never get generated). This has
# only ever bitten the very first DC in the suite; by the time later DCs run,
# the gap has long closed on its own.
TRIGGER_ACTIVATION_DELAY = 30  # seconds
