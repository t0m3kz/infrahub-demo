"""30_all, the everything demo: the branch and staged load, the generator dispatch and object inventory, and the
compute layer expectations (test_61)."""

# 30_all, the everything demo, shared by test_59-test_63 through the single branch below.
from tasks import ALL_DEMO_DATA

from .demo_data import ROOT, Declared, created_dispatches, declared, distinct, name
from .suite_constants import SCENARIO_ENDPOINT_APPLICATION

# What 30_all declares, read from its YAML (tests/integration/demo_data.py)
_ALL_DEMO_ROOT = ROOT / ALL_DEMO_DATA


def _all_demo(kind: str) -> list[Declared]:
    """The distinct objects of ``kind`` 30_all declares (a re-declaration that updates one counts once)."""
    return distinct(declared(kind, _ALL_DEMO_ROOT))


# Generators the 30_all load must dispatch by itself
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
        "add_customer_deployment_cloud",
        "add_cloud_attachment_range",
        "add_sdwan_edge",
        "add_office_fabric",
        "add_application_orchestrator_routing",
        "add_app_component_segment",
        "add_customer_dc_orchestrator_routing",
        "add_customer_colocation_orchestrator_routing",
        "add_customer_cloud_orchestrator_routing",
        "add_customer_office_orchestrator_routing",
    )
}


# The customers 30_all declares, by org_id: each is created on the branch
ALL_DEMO_CUSTOMER_ORG_IDS: tuple[str, ...] = tuple(
    sorted(str(customer.item["org_id"]) for customer in _all_demo("OrganizationCustomer"))
)


# Declared-object inventory of data/demos/30_all, counted from the YAML.
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
        "AppApplication",
        "AppComponent",
        "AppDependency",
        "SecurityAccessProfile",
        "DcimVirtualDevice",
        "TopologyCarrierCircuit",
        "TopologySessionCircuit",
        "CloudInstance",
        "ManagedCloudProxy",
    )
}


# ---------------------------------------------------------------------------
# 30_all — compute layer expectations (test_61)
# ---------------------------------------------------------------------------

ALL_DEMO_DC_NAMES: tuple[str, ...] = tuple(sorted(name(dc) for dc in _all_demo("TopologyDataCenter")))


# The three fabrics are declared with identical shapes (size L, ebgp-ebgp, ipv6 underlay, pbr)
ALL_DEMO_DC_ROLE_COUNTS: dict[str, int] = {
    "super-spine": 2,
    "border-leaf": 2,
    "spine": 4,
    "leaf": 4,
    "access-leaf": 4,
    "internal-firewall": 4,
    "load-balancer": 4,
}


# Every fabric device routes the underlay
ALL_DEMO_DC_UNDERLAY_ROLES = ("super-spine", "spine", "leaf", "access-leaf", "border-leaf")

ALL_DEMO_DC_OVERLAY_ROLES = ("super-spine", "spine", "border-leaf")


# Application hosts in row pairs
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


# The colocation cages are the deliberate counter-example
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


# Applications with no compute footprint
ALL_DEMO_NO_COMPUTE_APPLICATIONS: tuple[str, ...] = tuple(
    sorted(name(app) for app in _ALL_DEMO_APPLICATIONS if not _instances(app))
)


# name -> (criticality, component count), criticality defaulting like the schema.
ALL_DEMO_EXPECTED_APPLICATIONS: dict[str, tuple[str, int]] = {
    name(app): (str(app.value("criticality")), len(_components(app)))
    for app in _ALL_DEMO_APPLICATIONS
    if _instances(app)
}


# Cloud-native applications: their instances are CloudInstance nodes with no hosting_device.
ALL_DEMO_CLOUD_APPLICATIONS: tuple[str, ...] = tuple(
    sorted(
        name(app) for app in _ALL_DEMO_APPLICATIONS if _instances(app) and _instances(app) <= _ALL_DEMO_CLOUD_INSTANCES
    )
)


# Applications on the branch that test_61's application-graph check does not cover
ALL_DEMO_OUT_OF_SCOPE_APPLICATIONS = (*ALL_DEMO_NO_COMPUTE_APPLICATIONS, SCENARIO_ENDPOINT_APPLICATION)


# Each component is deployed as an HA pair.
ALL_DEMO_COMPONENT_INSTANCE_COUNT = 2
