"""Shared constants for integration tests."""

from typing import TypedDict

# ---------------------------------------------------------------------------
# Demo data paths  (single source of truth — same files as invoke demo.run-demo)
# ---------------------------------------------------------------------------

DEMO_DC_DATA_ROOT = "data/demos/01_data_center"
DEMO_SWITCH_DATA = "data/demos/02_switch"
DEMO_RACK_DATA = "data/demos/03_rack"
DEMO_POD_DATA = "data/demos/04_pod"
DEMO_SERVERS_DATA = "data/demos/06_servers"

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
    "add_app_application": 7,  # one per declared AppApplication
    # 7 distinct footprints: 03_dc/new_customers declares all of them and
    # 06_customer_boarding re-declares three as supersets, which upsert.
    # add_customer_deployment_cloud/office were removed: their only job was
    # hub-and-spoke exchange auto-provisioning, now replaced by the 4 fixed
    # bootstrap TopologyRoutedExchange objects (data/bootstrap/23_exchanges.yml)
    # — see docs/exchange_gateway.md.
    "add_customer_deployment_dc": 7,
    "add_customer_deployment_colocation": 7,
    # trigger-customer-office-sdwan-on-created fires unconditionally for
    # every TopologyCustomerOffice (5: C001/C003/C015/C002/C005) — the
    # generator itself no-ops on those without sdwan_gateway set. C002 and
    # C005 additionally dispatch a run each via the updated-relationship
    # trigger when 08_interconnects/04_sdwan/01_gateway.yml sets their
    # sdwan_gateway; 5 is the safe floor.
    "add_sdwan_edge": 5,
}

# Declared-object inventory of data/demos/30_all, measured from the YAML.
# Counted with `>=` because generators legitimately add more of several of
# these kinds (racks stay declared-only, but devices, prefixes and segments
# do not) — the point is that every declared object landed.
ALL_DEMO_EXPECTED_OBJECTS: dict[str, int] = {
    "TopologyDataCenter": 3,
    "TopologyPod": 6,
    "LocationRack": 25,
    "TopologyCustomerDC": 7,
    "TopologyCustomerColocation": 7,
    "TopologyCustomerCloud": 4,
    "TopologyCustomerOffice": 5,
    "ManagedVxlanSegment": 5,
    "ManagedVlanSegment": 7,
    "AppApplication": 7,
    # 18 AppComponent blocks are declared, but c005/04_component_updates.yml
    # re-declares web-frontend to attach its depends_on — an upsert, not a
    # 18th component.
    "AppComponent": 17,
    "DcimVirtualDevice": 26,
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
#              load-balancer HA pairs and their shared/dedicated virtual
#              instances (each DC hosts exactly one dedicated-firewall
#              customer — see ALL_DEMO_DEDICATED_FIREWALL_TENANTS)
#   POD level: 2 spines each, two pods
#   Racks    : two network racks x (2 leaf + 2 access-leaf)
#   Compute  : one application host per row (see ALL_DEMO_DC_HOST_ROWS)
ALL_DEMO_DC_ROLE_COUNTS: dict[str, int] = {
    "super-spine": 2,
    "border-leaf": 2,
    "spine": 4,
    "leaf": 4,
    "access-leaf": 4,
    "firewall": 8,
    "load-balancer": 6,
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
# what makes the change-risk blast radius of one access-leaf pair bounded.
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
# case for the change-risk check to contrast with the DC pods.
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

# name -> (criticality, component count). c005/04_component_updates.yml re-declares
# web-frontend to attach its depends_on, so c005 has 4 components, not 5.
ALL_DEMO_EXPECTED_APPLICATIONS: dict[str, tuple[str, int]] = {
    "c003-custody-api-p": ("high", 2),
    "c005-payment-core-p": ("critical", 4),
    "c006-erp-core-p": ("critical", 3),
    "c011-edge-analytics-p": ("medium", 2),
    "c012-payment-edge-p": ("high", 2),
    "c013-fraud-detection-p": ("critical", 2),
    "c016-billing-cloud-p": ("medium", 2),
}

# Cloud-native applications: their instances are CloudInstance nodes with no
# hosting_device. Every other application's instances are DcimVirtualDevice
# nodes that MUST name the physical host they run on — that edge is what lets
# the change-risk traversal walk from a switch port to a customer application.
ALL_DEMO_CLOUD_APPLICATIONS = ("c003-custody-api-p", "c016-billing-cloud-p")

# c001-checkout-p is a private-access-only frontend (access_profile-gated, no
# instances or network_segment declared) — it has no compute footprint, so it
# is out of scope for test_61's switch-port-to-application chain and is not
# listed in ALL_DEMO_EXPECTED_APPLICATIONS.
ALL_DEMO_NO_COMPUTE_APPLICATIONS = ("c001-checkout-p",)

# Each component is deployed as an HA pair.
ALL_DEMO_COMPONENT_INSTANCE_COUNT = 2

# ---------------------------------------------------------------------------
# 30_all — interconnect and tenant-service expectations (test_62)
# ---------------------------------------------------------------------------

ALL_DEMO_PHYSICAL_CIRCUIT_TYPES: dict[str, int] = {
    # All hand-authored in 08_interconnects/ — the opt-in
    # TopologyInterconnectRequest that used to scaffold 2 extra DC11<->DC12
    # stub circuits was removed along with its generator.
    "dark_fiber": 3,  # DC10/DC11 -> EQX FR2, DC12 -> EQX PA4
    "cross_connect": 2,  # EQX FR2 -> AWS eu-central-1, EQX PA4 -> Azure westeurope
    "internet": 5,  # C001/C003/C015 hand-authored + C002/C005 generator-produced (add_sdwan_edge)
}


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
# edge in the schema, and the edge the change-risk traversal walks.
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

# One shared (tenant-less) firewall context per DC cluster ...
ALL_DEMO_SHARED_FIREWALL_CONTEXTS = 3
# ... plus one dedicated context per customer whose design blueprint sets
# dedicated_firewall (data/bootstrap/21_customer_templates.yml: L_DC and
# XL_DC do, S_DC and M_DC do not). Maps tenant deployment -> design.
ALL_DEMO_DEDICATED_FIREWALL_TENANTS: dict[str, str] = {
    "C007-P-DC10": "L_DC",
    "C009-P-DC11": "XL_DC",
    "C005-D-DC12": "L_DC",
}

# C005's five application segments and the deployments each is activated in.
# The two dc_pair "stretch" segments carry two DC legs each, and
# colo-services-stretch one DC leg plus one colocation-metro leg (FR, reached
# over DF-DC10-EQXFR2 through the EVPN Multi-Site border gateways) — eight
# ManagedSegmentDeployment records in total.
ALL_DEMO_SEGMENT_LEGS: dict[str, tuple[str, ...]] = {
    "c005-web-frontend-local-dc10-p": ("DC10",),
    "c005-app-backend-stretch-p": ("DC10", "DC12"),
    "c005-database-local-dc12-d": ("DC12",),
    "c005-message-queue-stretch-p": ("DC10", "DC12"),
    "c005-colo-services-stretch-p": ("DC10", "FR"),
}

# Timeout and polling constants
REPO_SYNC_MAX_ATTEMPTS = 60
REPO_SYNC_POLL_INTERVAL = 5  # seconds
GENERATOR_DEFINITION_MAX_ATTEMPTS = 10
GENERATOR_DEFINITION_POLL_INTERVAL = 5  # seconds
GENERATOR_TASK_TIMEOUT = 1800  # 30 minutes
DIFF_TASK_TIMEOUT = 600  # 10 minutes
MERGE_TASK_TIMEOUT = 600  # 10 minutes
VALIDATION_MAX_ATTEMPTS = 30
VALIDATION_POLL_INTERVAL = 10  # seconds
DATA_PROPAGATION_DELAY = 3  # seconds
MERGE_PROPAGATION_DELAY = 5  # seconds
BRANCH_ENDPOINT_TIMEOUT = 120  # seconds

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
