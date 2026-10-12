"""Shared constants for integration tests."""

from typing import TypedDict

from utils.exchange_transit import EXCHANGE_PEERS, namespace_type_for_environment

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

# Generators the 30_all load must dispatch by itself, mapped to the minimum
# number of runs each must have. These are the event-driven runs Infrahub
# fires from data/events/99_actions.yml — not something the suite invokes —
# so a zero here means a trigger rule regressed, which is silent otherwise:
# the objects simply never get generated and no task fails.
ALL_DEMO_EXPECTED_GENERATORS: dict[str, int] = {
    "add_dc": 3,  # DC10, DC11, DC12
    "add_pod": 6,  # two pods per DC
    # 12 DC racks (four in each DC's POD-1) plus 13 colocation cage racks —
    # those are in topologies_rack too, so they dispatch add_rack and it
    # no-ops on them (LocationRack.pod is a colocation zone, not a pod).
    "add_rack": 25,
    # One run per declared metro (3 CoreSite + 3 Equinix + 3 Megaport). Only FR
    # and PA carry fabric_templates and actually build anything; the other 7 are
    # members of colocation_metros (required — a trigger firing for a non-member
    # node raises, see the endpoint note in data/events/99_actions.yml) and the
    # generator no-ops on them, same as add_rack does on the colocation cage
    # racks.
    "add_colocation_metro": 9,
    "add_endpoint": 12,  # 6 DC hosts + 6 colocation cage hosts
    "add_vxlan_segment": 5,  # C005's five application segments
    "add_app_application": 9,  # one per declared AppApplication
    # One per declared AppDependency: it loads after its application in the
    # same stage, so its own trigger is what puts its rule in place.
    "add_app_dependency": 14,
    # 9 distinct footprints: 03_dc/new_customers declares seven and
    # 06_customer_boarding re-declares three of them as supersets, which
    # upsert, and adds the C008/C010 dev footprints on DC12's shared hosts.
    # add_customer_deployment_cloud/office were removed: their only job was
    # hub-and-spoke exchange auto-provisioning, now replaced by the
    # per-context TopologyRoutedExchange objects the DC/colocation customer
    # generators create — see docs/exchange_gateway.md.
    "add_customer_deployment_dc": 9,
    "add_customer_deployment_colocation": 7,
    # trigger-customer-office-sdwan-on-created fires unconditionally for
    # every TopologyCustomerOffice (5: C001/C003/C015/C002/C005) — the
    # generator itself no-ops on those without sdwan_gateway set. C002 and
    # C005 additionally dispatch a run each via the updated-relationship
    # trigger when 08_interconnects/04_sdwan/01_gateway.yml sets their
    # sdwan_gateway; 5 is the safe floor.
    "add_sdwan_edge": 5,
    # Same AppApplication created event as add_app_application.
    "add_application_orchestrator_routing": 9,
    # One created-trigger run per declared AppComponent; add_endpoint's
    # fan-out only adds runs (components load after their servers are cabled,
    # so in 30_all it finds none).
    "add_app_component_segment": 22,
    # One created-trigger run per footprint kind, same floors as the
    # per-kind generators above (cloud/office: ALL_DEMO_EXPECTED_OBJECTS).
    "add_customer_dc_orchestrator_routing": 9,
    "add_customer_colocation_orchestrator_routing": 7,
    "add_customer_cloud_orchestrator_routing": 4,
    "add_customer_office_orchestrator_routing": 5,
}

# Declared-object inventory of data/demos/30_all, measured from the YAML.
# Counted with `>=` because generators legitimately add more of several of
# these kinds (racks stay declared-only, but devices, prefixes and segments
# do not) — the point is that every declared object landed.
ALL_DEMO_EXPECTED_OBJECTS: dict[str, int] = {
    "TopologyDataCenter": 3,
    "TopologyPod": 6,
    "LocationRack": 25,
    "TopologyCustomerDC": 9,
    "TopologyCustomerColocation": 7,
    "TopologyCustomerCloud": 4,
    "TopologyCustomerOffice": 5,
    "ManagedVxlanSegment": 5,
    "ManagedVlanSegment": 10,  # incl. the DC11 core-services segment (07_applications/infra)
    "AppApplication": 10,
    "AppComponent": 22,
    # One per tier-to-tier call, plus the access-profile grant that
    # publishes c001's checkout frontend; ports live on the components.
    "AppDependency": 14,
    "SecurityAccessProfile": 1,
    "DcimVirtualDevice": 34,
    "TopologyPhysicalCircuit": 8,
    "TopologyVirtualCircuit": 8,
    "CloudInstance": 8,
    "ManagedCloudProxy": 2,
}


# ---------------------------------------------------------------------------
# 30_all — compute layer expectations (test_61)
# ---------------------------------------------------------------------------

ALL_DEMO_DC_NAMES = ("DC10", "DC11", "DC12")

# The three fabrics are declared with identical shapes (size L, ebgp-ebgp,
# ipv6 underlay, pbr), so one expected role map covers all of them:
#   DC level : 2 super-spine + 2 border-leaf, plus the firewall and
#              load-balancer HA pairs and the dedicated virtual firewall and
#              load-balancer pairs of the DC's one customer with both
#              dedicated (see ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
#   POD level: 2 spines each, two pods
#   Racks    : two network racks x (2 leaf + 2 access-leaf)
#   Compute  : one application host per row (see ALL_DEMO_DC_HOST_ROWS)
ALL_DEMO_DC_ROLE_COUNTS: dict[str, int] = {
    "super-spine": 2,
    "border-leaf": 2,
    "spine": 4,
    "leaf": 4,
    "access-leaf": 4,
    "firewall": 4,
    "load-balancer": 4,
    "endpoint": 2,
}

# Every fabric device routes the underlay; only the tiers above the VTEP layer
# carry an overlay process in this design. access-leaf/leaf deliberately have
# no overlay process — they are the L2 access side of the pbr fabric.
ALL_DEMO_DC_UNDERLAY_ROLES = ("super-spine", "spine", "leaf", "access-leaf", "border-leaf")
ALL_DEMO_DC_OVERLAY_ROLES = ("super-spine", "spine", "border-leaf")

# One application host per compute-rack row. The row matters: add_endpoint
# cables a host to the access-leaf pair of the network rack sharing its row,
# so row-1 and row-2 hosts must land on *disjoint* access-leaf pairs. That
# disjointness is the whole point of putting them in different rows, and it is
# what keeps the blast radius of one access-leaf pair bounded.
ALL_DEMO_DC_HOST_ROWS: dict[str, int] = {
    "dc10-pod1-server-1": 1,
    "dc10-pod1-server-2": 2,
    "dc11-pod1-server-1": 1,
    "dc11-pod1-server-2": 2,
    "dc12-pod1-server-1": 1,
    "dc12-pod1-server-2": 2,
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

# name -> (criticality, component count).
ALL_DEMO_EXPECTED_APPLICATIONS: dict[str, tuple[str, int]] = {
    "c003-custody-api-p": ("high", 2),
    "c005-payment-core-p": ("critical", 4),
    "c006-erp-core-p": ("critical", 3),
    "c011-edge-analytics-p": ("medium", 2),
    "c012-payment-edge-p": ("high", 2),
    "c013-fraud-detection-p": ("critical", 2),
    "c016-billing-cloud-p": ("medium", 2),
    "c008-inventory-sync-d": ("medium", 2),
    "c010-reporting-portal-d": ("low", 2),
}

# Cloud-native applications: their instances are CloudInstance nodes with no
# hosting_device. Every other application's instances are DcimVirtualDevice
# nodes that MUST name the physical host they run on — that edge is what ties
# a switch port to a customer application.
ALL_DEMO_CLOUD_APPLICATIONS = ("c003-custody-api-p", "c016-billing-cloud-p")

# c001-checkout-p is a private-access-only frontend (published through an
# access-profile dependency, no instances or network_segment declared) — it
# has no compute footprint, so it is out of scope for test_61's switch-port-to-application chain and is not
# listed in ALL_DEMO_EXPECTED_APPLICATIONS.
ALL_DEMO_NO_COMPUTE_APPLICATIONS = ("c001-checkout-p",)

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

ALL_DEMO_PHYSICAL_CIRCUIT_TYPES: dict[str, int] = {
    # All hand-authored in 08_interconnects/ — the opt-in
    # TopologyInterconnectRequest that used to scaffold 2 extra DC11<->DC12
    # stub circuits was removed along with its generator.
    # Each DC has one fibre to each of its metro's two border gateways:
    # DC10/DC11 -> EQX FR2 (01 and 02 edge), DC12 -> EQX PA4 (01 and 02 edge).
    "dark_fiber": 6,
    "cross_connect": 2,  # EQX FR2 -> AWS eu-central-1, EQX PA4 -> Azure westeurope
    # 5 office underlays (C001/C003/C015 hand-authored + C002/C005 from
    # add_sdwan_edge) + 4 shared transit circuits from 06_internet.
    "internet": 9,
}

# The peering_role=dci dark fibres (08_interconnects/01_colo_onramp/
# 03_physical_circuits.yml): circuit_id -> (DC whose fabric overlay key keys
# the session, the two (device, interface) ends). add_circuit builds one
# DCI-<circuit_id> session per entry.
ALL_DEMO_DCI_CIRCUITS: dict[str, tuple[str, tuple[tuple[str, str], tuple[str, str]]]] = {
    "DF-DC10-EQXFR2": ("DC10", (("bl-dc101101", "Ethernet1/35"), ("eg-fr01", "Ethernet1/10"))),
    "DF-DC11-EQXFR2": ("DC11", (("bl-dc1111101", "Ethernet1/35"), ("eg-fr01", "Ethernet1/11"))),
    "DF-DC12-EQXPA4": ("DC12", (("bl-dc1212101", "Ethernet1/35"), ("eg-pa01", "Ethernet1/10"))),
    "DF-DC10-EQXFR2-B": ("DC10", (("bl-dc101101", "Ethernet1/36"), ("eg-fr02", "Ethernet1/10"))),
    "DF-DC11-EQXFR2-B": ("DC11", (("bl-dc1111101", "Ethernet1/36"), ("eg-fr02", "Ethernet1/11"))),
    "DF-DC12-EQXPA4-B": ("DC12", (("bl-dc1212101", "Ethernet1/36"), ("eg-pa02", "Ethernet1/10"))),
}

# Shared ISP transit (08_interconnects/06_internet): colo edges -> ISP PEs.
# Unowned, unlike a customer's own internet underlay.
ALL_DEMO_INTERNET_TRANSIT_CIRCUITS: frozenset[str] = frozenset(
    {"INET-LUMEN-EQXFR2", "INET-COGENT-EQXFR2", "INET-LUMEN-EQXPA4", "INET-COGENT-EQXPA4"}
)

# INTERNET upstream (PR 4c, extended by the metro edge pair): the colo edges are
# their metro's EVPN Multi-Site border gateways and export the ISP default route
# as an EVPN type-5 route (L3 VNI 60002, one global route-target for the
# INTERNET namespace). Both edges of each metro share the site VIP and have their
# own dark fibre to every DC the metro fronts (ALL_DEMO_DCI_CIRCUITS).
ALL_DEMO_INTERNET_BGWS: frozenset[str] = frozenset({"eg-fr01", "eg-fr02", "eg-pa01", "eg-pa02"})

# DC -> the BGWs its DCI sessions land on, i.e. where that DC learns 0/0 from:
# both edges of the metro it fronts.
ALL_DEMO_INTERNET_UPSTREAM_BY_DC: dict[str, frozenset[str]] = {
    "DC10": frozenset({"eg-fr01", "eg-fr02"}),
    "DC11": frozenset({"eg-fr01", "eg-fr02"}),
    "DC12": frozenset({"eg-pa01", "eg-pa02"}),
}

# Metros with exactly two PHYSICAL edges in 30_all (generators/topology/colocation.py
# _ensure_edge_pair; virtual edges are ignored). Derived from
# data/demos/30_all/02_colo/equinix/{fr,pa}/02_topology.yml: FR and PA declare
# `["2", "edge", "N9K-C9316D-GX_EDGE"]`; AM is virtual, the other Equinix/Megaport/
# CoreSite metros declare no fabric_templates.
ALL_DEMO_EDGE_PAIR_METROS: dict[str, tuple[str, str]] = {
    "fr": ("eg-fr01", "eg-fr02"),
    "pa": ("eg-pa01", "eg-pa02"),
}

# The interlink port (N9K-C9316D-GX_EDGE Ethernet1/8, role `interlink`) cabled between the pair.
ALL_DEMO_EDGE_INTERLINK_INTERFACE = "Ethernet1/8"

# Metro keys are the lowercase metro names the generator uses for its pool and key names.
# metro -> ((device, interface), (device, interface)) of the one DcimCable per pair.
ALL_DEMO_EDGE_INTERLINKS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    metro: ((a, ALL_DEMO_EDGE_INTERLINK_INTERFACE), (b, ALL_DEMO_EDGE_INTERLINK_INTERFACE))
    for metro, (a, b) in ALL_DEMO_EDGE_PAIR_METROS.items()
}

# metro -> the one FABRIC-<a>-<b> iBGP session (peering_role fabric) between the pair's overlay processes.
ALL_DEMO_FABRIC_PEERINGS: dict[str, str] = {
    metro: f"FABRIC-{a}-{b}" for metro, (a, b) in ALL_DEMO_EDGE_PAIR_METROS.items()
}

# The metro-owned slice of Technical-IPv6 the interlink /127 comes from: `{metro}-technical-ipv6-pool`.
ALL_DEMO_EDGE_PAIR_POOL_TEMPLATE = "{metro}-technical-ipv6-pool"

# The shared Multi-Site VIP: both edges of a metro carry ONE /128 on loopback100
# (data/demos/30_all/08_interconnects/01_colo_onramp/04_multisite_vips.yml).
ALL_DEMO_METRO_SITE_VIPS: dict[str, str] = {"fr": "fd00:2500:ff::f1/128", "pa": "fd00:2500:ff::f2/128"}

# The four eBGP transit sessions in VRF INTERNET (06_internet/05_bgp.yml) -> the colo edge each runs on.
ALL_DEMO_INTERNET_TRANSIT_PEERINGS: dict[str, str] = {
    "TRANSIT-LUMEN-EQXFR2": "eg-fr01",
    "TRANSIT-COGENT-EQXFR2": "eg-fr02",
    "TRANSIT-LUMEN-EQXPA4": "eg-pa01",
    "TRANSIT-COGENT-EQXPA4": "eg-pa02",
}

# The L3 VNI of the INTERNET namespace (data/bootstrap) and its global route-target
# `{l3_vni}:{l3_vni}`, imported and exported by every VTEP carrying the VRF.
INTERNET_L3_VNI = 60002
INTERNET_ROUTE_TARGET = "60002:60002"


class VirtualCircuitExpectation(TypedDict):
    """What one TopologyVirtualCircuit in 30_all must look like."""

    link_type: str
    transport_mode: str
    #: None for the shared on-ramp circuits that belong to no single customer.
    owner: str | None
    physical_circuits: tuple[str, ...]
    interfaces: int
    cloud_endpoints: tuple[str, ...]


# The full virtual-circuit fabric. `physical_circuits` is the underlay mapping
# and `interfaces` the minimum number of terminating DcimInterfaces reached
# through interface_capabilities — the one uniform "what is this port doing"
# edge in the schema.
ALL_DEMO_VIRTUAL_CIRCUITS: dict[str, VirtualCircuitExpectation] = {
    "VC-C005-DC10-AWS-EUC1": {
        "link_type": "direct_connect_aws",
        "transport_mode": "physical_backed",
        "owner": "Drentec BV",
        "physical_circuits": ("DF-DC10-EQXFR2", "XC-EQXFR2-AWS-EUC1"),
        "interfaces": 2,
        "cloud_endpoints": ("vif-transit-hub-euc1",),
    },
    "VC-C006-DC11-AWS-EUC1": {
        "link_type": "direct_connect_aws",
        "transport_mode": "physical_backed",
        "owner": "Lumivex SA",
        "physical_circuits": ("DF-DC11-EQXFR2", "XC-EQXFR2-AWS-EUC1"),
        "interfaces": 2,
        "cloud_endpoints": ("vif-transit-hub-euc1",),
    },
    # Azure westeurope: only the AWS hub is modelled, so this one deliberately
    # has no cloud endpoint.
    "VC-C008-DC12-AZURE-WEU": {
        "link_type": "express_route_azure",
        "transport_mode": "physical_backed",
        "owner": "Krafven GmbH",
        "physical_circuits": ("DF-DC12-EQXPA4", "XC-EQXPA4-AZURE-WEU"),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
    # Colocation-only leg: the customer end is in the cage, so a single
    # terminating interface is correct.
    "VC-C001-EQXFR-AWS-EUC1": {
        "link_type": "equinix_fabric",
        "transport_mode": "physical_backed",
        "owner": "Nordix Ltd.",
        "physical_circuits": ("XC-EQXFR2-AWS-EUC1",),
        "interfaces": 1,
        "cloud_endpoints": ("vif-transit-hub-euc1",),
    },
    "VC-SDWAN-FR2-AWS-EUC1": {
        "link_type": "direct_connect_aws",
        "transport_mode": "physical_backed",
        "owner": None,  # shared SD-WAN on-ramp, not a customer circuit
        "physical_circuits": ("XC-EQXFR2-AWS-EUC1",),
        "interfaces": 2,
        "cloud_endpoints": ("vif-transit-hub-euc1",),
    },
    "C001-SDWAN-FR2": {
        "link_type": "sd_wan",
        "transport_mode": "internet_backed",
        "owner": "Nordix Ltd.",
        "physical_circuits": ("INET-C001-WAW-FR2",),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
    "C003-SDWAN-FR2": {
        "link_type": "sd_wan",
        "transport_mode": "internet_backed",
        "owner": "Vaultex Inc.",
        "physical_circuits": ("INET-C003-MUC-FR2",),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
    "C015-SDWAN-FR2": {
        "link_type": "sd_wan",
        "transport_mode": "internet_backed",
        "owner": "Novantis GmbH",
        "physical_circuits": ("INET-C015-PAR-FR2",),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
    # Entirely generator-produced (add_sdwan_edge), unlike its hand-authored
    # siblings above — proves the automated per-office SD-WAN path end-to-end.
    "C002-P-SDWAN-FR2": {
        "link_type": "sd_wan",
        "transport_mode": "internet_backed",
        "owner": "SwiftGo GmbH",
        "physical_circuits": ("INET-C002-P",),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
    # Same generator path as C002; C005's office additionally routes into
    # colo-services-stretch over 08_interconnects/04_sdwan/06_vrf_handoff.yml.
    "C005-P-SDWAN-FR2": {
        "link_type": "sd_wan",
        "transport_mode": "internet_backed",
        "owner": "Drentec BV",
        "physical_circuits": ("INET-C005-P",),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
    # Second tunnel riding the same internet underlay as C001-SDWAN-FR2, on a
    # different gateway sub-interface — see 08_interconnects/07_zone_policies/
    # 01_partner_virtual_circuit.yml for why this is an overlay, not a new
    # dedicated physical cross-connect.
    "C001-PARTNER-ACME-FR2": {
        "link_type": "vpn_ipsec",
        "transport_mode": "internet_backed",
        "owner": "Nordix Ltd.",
        "physical_circuits": ("INET-C001-WAW-FR2",),
        "interfaces": 2,
        "cloud_endpoints": (),
    },
}

# One shared (tenant-less) firewall context per firewall cluster: the three
# DC clusters and the FR colocation metro's pair, which every colocation
# deployment in the metro shares (a colocation has no design, so never a
# dedicated context). The colocation context carries transit legs like a DC
# one (its legs face the metro's two edges, whose firewall ports are tagged
# with the context) ...
ALL_DEMO_SHARED_FIREWALL_CONTEXTS = 4
# Every 30_all colocation deployment -> whether its metro has firewalls, and so
# a serving context. Only FR declares a firewall pair (02_colo/equinix/fr); the
# rest are edge-only metros whose deployments must stay unserved.
ALL_DEMO_COLOCATION_SERVED = {
    "C001-P-FR": True,
    "C005-P-FR": True,
    "C002-P-PA": False,
    "C011-P-NY": False,
    "C012-P-VA": False,
    "C013-P-AMS": False,
    "C014-P-PAR": False,
}
# Served colocation metro -> the deployments its shared context serves, derived from
# ALL_DEMO_COLOCATION_SERVED (a deployment name ends in its metro: `C001-P-FR` -> FR).
ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    site: tuple(name for name, served in ALL_DEMO_COLOCATION_SERVED.items() if served and name.endswith(f"-{site}"))
    for site in sorted({name.rsplit("-", 1)[-1] for name, served in ALL_DEMO_COLOCATION_SERVED.items() if served})
}
# ... plus one dedicated context per customer whose design blueprint sets
# dedicated_firewall (data/bootstrap/21_customer_templates.yml: L_DC and
# XL_DC do, S_DC and M_DC do not). Maps tenant deployment -> design.
ALL_DEMO_DEDICATED_FIREWALL_TENANTS: dict[str, str] = {
    "C007-P-DC10": "L_DC",
    "C009-P-DC11": "XL_DC",
    "C005-P-DC12": "L_DC",
}

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
        "destination_segment": "internet-internet-p",
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
# no dedicated context. A served colocation metro (ALL_DEMO_COLOCATION_SITE_DEPLOYMENTS,
# FR) has one shared context serving all its deployments, on the same rules. A deployment's VRF follows its environment (`p` is
# PROD, every other value NON-PROD). The deployments are the nine
# TopologyCustomerDC footprints of 03_dc/new_customers plus the C008/C010 dev
# footprints that 06_customer_boarding adds on DC12 (the c005/c006 boardings
# there re-declare footprints that already exist, so they add none).
# ---------------------------------------------------------------------------

ALL_DEMO_TRANSIT_NAMESPACES = ("PROD", "NON-PROD", "INTERNET", "MANAGEMENT")
_TRANSIT_NAMESPACE_BY_TYPE: dict[str, str] = dict(
    zip(("prod", "non_prod", "internet", "management"), ALL_DEMO_TRANSIT_NAMESPACES)
)

# Deployment name ({org}-{environment}-{DC}) of every TopologyCustomerDC, per DC.
ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS: dict[str, tuple[str, ...]] = {
    "DC10": ("C005-P-DC10", "C007-P-DC10"),
    "DC11": ("C006-P-DC11", "C009-P-DC11"),
    "DC12": ("C005-P-DC12", "C008-P-DC12", "C008-D-DC12", "C010-P-DC12", "C010-D-DC12"),
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
# The c006 segment (VLAN 103, PROD) whose rules reach the services segment.
MANAGEMENT_CLIENT_SEGMENT_VLAN = 103
MANAGEMENT_CLIENT_SEGMENT_PREFIX = "10.6.103.0/24"
# rule name -> (protocol, port), the DC11 permits PROD-ZONE -> MANAGEMENT-ZONE.
ALL_DEMO_MANAGEMENT_RULES: dict[str, tuple[str, int]] = {
    "nordix-prod-to-core-dns": ("udp", 53),
    "nordix-prod-to-core-ntp": ("udp", 123),
}

# A firewall cluster is an HA pair: one sub-interface per member per leg.
ALL_DEMO_FIREWALL_MEMBERS = 2


class ExpectedTransitContext(TypedDict):
    """What one firewall context of a 30_all site (a DC, or a served colocation metro) must carry."""

    #: The DC (`DC10`) or the colocation metro (`FR`) the context's firewalls sit in.
    site: str
    #: Tenant deployment of a dedicated context, None for the DC's shared one.
    tenant: str | None
    #: Deployments the context serves.
    deployments: tuple[str, ...]
    #: Namespace names with a leg, in ALL_DEMO_TRANSIT_NAMESPACES order.
    namespaces: tuple[str, ...]
    #: Exchange name suffixes `{A}-{Z}` (the exchange is `{context}-{A}-{Z}`).
    exchanges: tuple[str, ...]


def deployment_environment(deployment: str) -> str:
    """`C008-D-DC12` -> `d`: the environment segment of a deployment name."""
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
    then the shared context of every served colocation metro (never a dedicated one: no design)."""
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
# leg, so that is every context, the colocation metro's included.
ALL_DEMO_SNAT_POOL_PREFIX = "192.0.2.0/24"
ALL_DEMO_SNAT_ADDRESSES = sum("INTERNET" in c["namespaces"] for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
# DC10 3+3, DC11 3+3, DC12 4+3, FR 3 legs.
ALL_DEMO_TRANSIT_LEGS = sum(len(c["namespaces"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
ALL_DEMO_TRANSIT_EXCHANGES = sum(len(c["exchanges"]) for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
# One /29 per leg, so the /29 count per VRF is the leg count per VRF.
ALL_DEMO_TRANSIT_PREFIXES: dict[str, int] = {
    name: sum(name in c["namespaces"] for c in ALL_DEMO_EXPECTED_TRANSIT_CONTEXTS)
    for name in ALL_DEMO_TRANSIT_NAMESPACES
}
ALL_DEMO_TRANSIT_SUBINTERFACES = ALL_DEMO_TRANSIT_LEGS * ALL_DEMO_FIREWALL_MEMBERS
# Each site's member uplinks reach 2 service ports (dedicated virtual members follow
# their hosting firewall): a DC's border leafs, a colocation metro's two edges
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

# C005's five application segments and the deployments each is activated in.
# The two dc_pair "stretch" segments carry two DC legs each, and
# colo-services-stretch one DC leg plus one colocation-metro leg (FR, reached
# over DF-DC10-EQXFR2 through the EVPN Multi-Site border gateways) — eight
# ManagedSegmentDeployment records in total.
ALL_DEMO_SEGMENT_LEGS: dict[str, tuple[str, ...]] = {
    "c005-web-frontend-local-dc10-p": ("DC10",),
    "c005-app-backend-stretch-p": ("DC10", "DC12"),
    "c005-database-local-dc12-p": ("DC12",),
    "c005-message-queue-stretch-p": ("DC10", "DC12"),
    "c005-colo-services-stretch-p": ("DC10", "FR"),
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
