"""30_all, the everything demo: the branch and staged load, the generator dispatch and object inventory, and the
compute layer expectations (test_61)."""

# ---------------------------------------------------------------------------
# 30_all — the everything demo (DC fabrics + colo + cloud + offices +
# customer deployments + applications + interconnects), shared by every
# test_59-test_63 module through the single branch below.
# ---------------------------------------------------------------------------
from tasks import ALL_DEMO_DATA

from .demo_data import ROOT, Declared, created_dispatches, declared, distinct, name
from .suite_constants import SCENARIO_ENDPOINT_APPLICATION

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
        "add_customer_routing_tag",
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


# The customers 30_all declares, by org_id: each is created on the branch, so the created-trigger of
# add_customer_routing_tag fires for it and it must end up with a routing tag. Not the bootstrap Internet
# pseudo-customer: it is created on main, where no trigger fires.
ALL_DEMO_CUSTOMER_ORG_IDS: tuple[str, ...] = tuple(
    sorted(str(customer.item["org_id"]) for customer in _all_demo("OrganizationCustomer"))
)


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
