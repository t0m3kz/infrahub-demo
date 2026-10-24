"""30_all load-balancer contexts, VIPs, client dependencies and the segment deployment legs (test_62, test_64)."""

from typing import TypedDict

from generators.helpers.lb_vip import cloud_lb_name, cloud_lb_scheme
from utils.exchange_transit import TENANT_NAMESPACE_TYPES, lb_leg_pool_name, lb_vip_pool_name
from utils.ports import PortProfileHelper

from .all_demo_circuit_constants import _site
from .all_demo_constants import ALL_DEMO_EXPECTED_OBJECTS, _all_demo
from .all_demo_firewall_constants import (
    _BOOTSTRAP_ROOT,
    _TRANSIT_NAMESPACE_BY_TYPE,
    ALL_DEMO_DC_CUSTOMER_DEPLOYMENTS,
    _dedicated_tenants,
    deployment_namespace,
)
from .demo_data import Declared, declared, name, object_templates

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
