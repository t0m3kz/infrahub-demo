"""Tenant services, exchange transit and load-balancer inventories and their matching to the expectations (test_62,
test_64)."""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from infrahub_sdk import InfrahubClient

from utils.data_cleaning import clean_data

from .circuit_helpers import _deployed_dc_names
from .query_files import QUERY_GET_LOADBALANCER_INVENTORY, QUERY_GET_TENANT_SERVICES, QUERY_GET_TRANSIT_INVENTORY
from .suite_constants import DATA_PROPAGATION_DELAY

logger = logging.getLogger(__name__)


def scope_tenant_services_to_dcs(
    services: dict[str, list[dict[str, Any]]], dc_names: Iterable[str]
) -> dict[str, list[dict[str, Any]]]:
    """Drop firewall contexts and segment legs that live in a DC outside ``dc_names``.

    Other suites in the same session merge their own customers into main (the
    DC6 scenario chain's test_19 boards C001 onto DC6), and a branch made from
    main inherits that state. Scoping by DC keeps a suite's exact counts about
    its own DCs. Anything not in a TopologyDataCenter, such as a colocation
    metro's cluster or leg, is kept: only DCs are shared between suites.
    """
    allowed = set(dc_names)
    return {
        **services,
        "firewall_contexts": [
            context
            for context in services["firewall_contexts"]
            if not context["cluster_dcs"] or set(context["cluster_dcs"]) & allowed
        ],
        "segment_deployments": [
            leg
            for leg in services["segment_deployments"]
            if not leg["deployment_is_dc"] or leg["deployment"] in allowed
        ],
    }


async def fetch_tenant_services(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch the tenant-scoped firewall contexts and segment deployment legs.

    The load-balancer contexts have their own inventory (fetch_loadbalancer_inventory).

    Returns:
        {"firewall_contexts": [{"name", "cluster", "cluster_dcs", "tenant",
                                "tenant_design"}],
         "segment_deployments": [{"segment", "segment_kind", "vni", "status",
                                  "deployment", "deployment_is_dc"}]}

        ``cluster_dcs`` is the sorted set of TopologyDataCenter names the
        cluster's member firewalls are deployed in (empty for a colocation
        metro's cluster); see scope_tenant_services_to_dcs.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)

    raw = await client.execute_graphql(query=QUERY_GET_TENANT_SERVICES)
    result = clean_data(raw)

    firewall_contexts = [
        {
            "name": str(context.get("name") or ""),
            "cluster": (context.get("cluster") or {}).get("name"),
            "cluster_dcs": _deployed_dc_names((context.get("cluster") or {}).get("capabilities")),
            "tenant": (context.get("tenant") or {}).get("name"),
            "tenant_design": ((context.get("tenant") or {}).get("design") or {}).get("name"),
        }
        for context in result.get("ManagedFirewallContext", []) or []
    ]

    segment_deployments = [
        {
            "segment": (deployment.get("segment") or {}).get("name"),
            "segment_kind": (deployment.get("segment") or {}).get("typename"),
            "vni": deployment.get("vni"),
            "status": str(deployment.get("status") or ""),
            "deployment": (deployment.get("deployment") or {}).get("name"),
            "deployment_is_dc": (deployment.get("deployment") or {}).get("typename") == "TopologyDataCenter",
        }
        for deployment in result.get("ManagedSegmentDeployment", []) or []
    ]

    logger.info(
        "Tenant services on branch '%s': %d firewall context(s), %d segment leg(s)",
        branch,
        len(firewall_contexts),
        len(segment_deployments),
    )
    for context in sorted(firewall_contexts, key=lambda c: str(c["name"])):
        logger.info("  FW %s tenant=%s design=%s", context["name"], context["tenant"], context["tenant_design"])
    for leg in sorted(segment_deployments, key=lambda d: (str(d["segment"]), str(d["deployment"]))):
        logger.info("  SEG %s -> %s (vni=%s, %s)", leg["segment"], leg["deployment"], leg["vni"], leg["status"])
    return {"firewall_contexts": firewall_contexts, "segment_deployments": segment_deployments}


def _edge_nodes(connection: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The nodes of a raw GraphQL `{edges: [{node}]}` connection."""
    return [edge["node"] for edge in (connection or {}).get("edges", [])]


def _rel_node(relationship: dict[str, Any] | None) -> dict[str, Any]:
    """The node of a raw GraphQL cardinality-one `{node}` relationship, {} when unset."""
    return (relationship or {}).get("node") or {}


def _value(attribute: dict[str, Any] | None) -> Any:
    """The `value` of a raw GraphQL attribute, None when absent."""
    return (attribute or {}).get("value")


def health_check_hfids(connection: dict[str, Any] | None) -> tuple[tuple[str, ...], ...]:
    """The health checks of a raw `{edges: [{node}]}` connection as sorted [check, rise, fall, timeout] strings."""
    return tuple(
        sorted(
            tuple(str(_value(check.get(field))) for field in ("check", "rise", "fall", "timeout"))
            for check in _edge_nodes(connection)
        )
    )


#: Device roles whose service ports (firewall- or load-balancer-facing) a context is tagged on: a DC's border
#: leaf, a colocation metro's edge.
_SERVICE_PORT_DEVICE_ROLES = frozenset({"border-leaf", "edge"})


def _context_legs_and_ports(node: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """A service context's legs and the service ports tagged with it, from its raw ``capabilities``.

    A leg is a DcimVirtualInterface (a member's VLAN sub-interface): {"interface", "device",
    "device_id", "address", "namespace", "namespace_type", "exchanges"}, ``exchanges`` the names of
    the TopologyRoutedExchanges it is linked to (always empty for a load-balancer leg). A service port
    is a DcimPhysicalInterface on a border leaf (DC) or an edge (colocation metro): {"id",
    "interface", "device", "role"}, ``role`` the port's own role where the query selects it.
    """
    legs: list[dict[str, Any]] = []
    border_ports: list[dict[str, Any]] = []
    for cap in _edge_nodes(node.get("capabilities")):
        device = _rel_node(cap.get("device"))
        if cap["__typename"] == "DcimVirtualInterface":
            address = _rel_node(cap.get("ip_address"))
            namespace = _rel_node(address.get("ip_namespace"))
            legs.append(
                {
                    "interface": _value(cap.get("name")),
                    "device": _value(device.get("name")),
                    "device_id": device.get("id"),
                    "address": _value(address.get("address")),
                    "namespace": _value(namespace.get("name")),
                    "namespace_type": _value(namespace.get("namespace_type")),
                    "exchanges": sorted(
                        _value(exchange.get("name"))
                        for exchange in _edge_nodes(cap.get("capabilities"))
                        if exchange.get("__typename") == "TopologyRoutedExchange"
                    ),
                }
            )
        elif cap["__typename"] == "DcimPhysicalInterface" and _value(device.get("role")) in _SERVICE_PORT_DEVICE_ROLES:
            border_ports.append(
                {
                    "id": cap["id"],
                    "interface": _value(cap.get("name")),
                    "device": _value(device.get("name")),
                    "role": _value(cap.get("role")),
                }
            )
    return legs, border_ports


def _cluster_dcs(cluster: dict[str, Any]) -> list[str]:
    """The TopologyDataCenter names a raw HA cluster's physical members are deployed in (see _deployed_dc_names)."""
    return _deployed_dc_names(
        [
            {
                "deployment": {
                    "typename": _rel_node(member.get("deployment")).get("__typename"),
                    "name": _value(_rel_node(member.get("deployment")).get("name")),
                }
            }
            for member in _edge_nodes(cluster.get("capabilities"))
        ]
    )


async def fetch_transit_inventory(client: InfrahubClient, branch: str) -> dict[str, list[dict[str, Any]]]:
    """Fetch every firewall context with its transit legs, plus every routed exchange.

    Returns:
        {"contexts": [{"id", "name", "vlan", "snat", "snat_namespace", "tenant", "cluster_dcs",
                       "served": [{"name", "environment"}],
                       "legs": [{"interface", "device", "device_id", "address",
                                 "namespace", "namespace_type", "exchanges"}],
                       "border_ports": [{"id", "interface", "device", "role"}]}],
         "exchanges": [{"id", "name", "namespace_a", "namespace_a_type",
                        "namespace_z", "namespace_z_type", "gateway_id",
                        "gateway_name"}]}

        ``legs`` and ``border_ports`` as _context_legs_and_ports returns them.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)
    raw = await client.execute_graphql(query=QUERY_GET_TRANSIT_INVENTORY, branch_name=branch)

    contexts: list[dict[str, Any]] = []
    for node in _edge_nodes(raw["ManagedFirewallContext"]):
        legs, border_ports = _context_legs_and_ports(node)
        contexts.append(
            {
                "id": node["id"],
                "name": _value(node.get("name")),
                "vlan": _value(node.get("vlan_id")),
                "snat": _value(_rel_node(node.get("snat_address")).get("address")),
                "snat_namespace": _value(
                    _rel_node(_rel_node(node.get("snat_address")).get("ip_namespace")).get("name")
                ),
                "tenant": _value(_rel_node(node.get("tenant")).get("name")),
                "cluster_dcs": _cluster_dcs(_rel_node(node.get("cluster"))),
                "served": [
                    {"name": _value(served.get("name")), "environment": _value(served.get("environment"))}
                    for served in _edge_nodes(node.get("served_deployments"))
                ],
                "legs": legs,
                "border_ports": border_ports,
            }
        )

    exchanges = [
        {
            "id": node["id"],
            "name": _value(node.get("name")),
            "namespace_a": _value(_rel_node(node.get("namespace_a")).get("name")),
            "namespace_a_type": _value(_rel_node(node.get("namespace_a")).get("namespace_type")),
            "namespace_z": _value(_rel_node(node.get("namespace_z")).get("name")),
            "namespace_z_type": _value(_rel_node(node.get("namespace_z")).get("namespace_type")),
            "gateway_id": _rel_node(node.get("gateway")).get("id"),
            "gateway_name": _value(_rel_node(node.get("gateway")).get("name")),
        }
        for node in _edge_nodes(raw["TopologyRoutedExchange"])
    ]
    logger.info(
        "Transit inventory on branch '%s': %d firewall context(s), %d routed exchange(s)",
        branch,
        len(contexts),
        len(exchanges),
    )
    return {"contexts": contexts, "exchanges": exchanges}


def _tenant_namespace(context: dict[str, Any]) -> str | None:
    """The one of PROD / NON-PROD a firewall context has a leg in (INTERNET and MANAGEMENT are its peers), else None."""
    namespaces = {str(leg["namespace"]) for leg in context["legs"]} & {"PROD", "NON-PROD"}
    return next(iter(namespaces)) if len(namespaces) == 1 else None


def match_transit_contexts(
    contexts: list[dict[str, Any]],
    dc_names: Iterable[str],
    dedicated_tenants: Iterable[str],
    colocation_sites: Mapping[str, Iterable[str]] | None = None,
) -> tuple[dict[tuple[str, str, str | None], dict[str, Any]], list[dict[str, Any]]]:
    """Split the contexts into the ones the suite owns, keyed by site, and the other colocation ones.

    Returns ({(site, namespace, tenant): context}, [unmatched contexts]). The namespace is the context's
    tenant VRF (PROD or NON-PROD, see _tenant_namespace): a shared context is one per (cluster, namespace). A
    shared context is keyed (dc, namespace, None) by the DC its member firewalls are deployed in; a dedicated
    one (its members are virtual, so carry no deployment) by the tenant's own trailing DC (`C007-P-DC10` -> DC10)
    and the tenant name. Contexts of a DC outside ``dc_names`` (the DC6 chain's) and dedicated contexts of
    tenants outside ``dedicated_tenants`` are dropped. A colocation context is one with neither tenant nor DC:
    when the deployments it serves are all among those of a metro in ``colocation_sites`` (serving zone ->
    deployment names) it is keyed (zone, namespace, None), otherwise it is returned in the list. So is a
    context whose legs are in no single tenant namespace, or whose key another context already holds.
    """
    allowed = set(dc_names)
    tenants = set(dedicated_tenants)
    sites = {site: frozenset(names) for site, names in (colocation_sites or {}).items()}
    by_key: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []

    def keep(site: str, tenant: str | None, context: dict[str, Any]) -> None:
        namespace = _tenant_namespace(context)
        if namespace is None or (site, namespace, tenant) in by_key:
            unmatched.append(context)
        else:
            by_key[(site, namespace, tenant)] = context

    for context in contexts:
        tenant = context["tenant"]
        if tenant:
            dc = str(tenant).rsplit("-", 1)[-1]
            if tenant in tenants and dc in allowed:
                keep(dc, str(tenant), context)
            continue
        dcs = sorted(set(context["cluster_dcs"]) & allowed)
        if dcs:
            keep(dcs[0], None, context)
        elif not context["cluster_dcs"]:
            served = frozenset(str(d["name"]) for d in context.get("served", []))
            site = next((name for name, deployments in sites.items() if served and served <= deployments), None)
            if site is None:
                unmatched.append(context)
            else:
                keep(site, None, context)
    return by_key, unmatched


async def fetch_loadbalancer_inventory(client: InfrahubClient, branch: str) -> list[dict[str, Any]]:
    """Fetch every load-balancer context with its leg, service ports, VIP range and VIPs.

    Returns:
        [{"id", "name", "vlan", "tenant", "cluster", "cluster_member_ids", "cluster_dcs",
          "served": [names], "vip_prefix", "vip_prefix_namespace",
          "legs": [...], "border_ports": [...],
          "vips": [{"hostname", "protocol", "port", "address", "members": [addresses],
                    "component", "segment", "segment_prefix"}]}]

        ``legs`` and ``border_ports`` as _context_legs_and_ports returns them;
        ``component`` is the fronted AppComponent's fqdn, ``segment`` and
        ``segment_prefix`` its network segment and that segment's gateway prefix.
    """
    client.default_branch = branch
    await asyncio.sleep(DATA_PROPAGATION_DELAY)
    raw = await client.execute_graphql(query=QUERY_GET_LOADBALANCER_INVENTORY, branch_name=branch)

    contexts: list[dict[str, Any]] = []
    for node in _edge_nodes(raw["ManagedLoadbalancerContext"]):
        legs, border_ports = _context_legs_and_ports(node)
        cluster = _rel_node(node.get("cluster"))
        vip_prefix = _rel_node(node.get("vip_prefix"))
        vips = []
        for vip in _edge_nodes(node.get("vips")):
            component = _rel_node(vip.get("component"))
            segment = _rel_node(component.get("network_segment"))
            vips.append(
                {
                    "hostname": _value(vip.get("hostname")),
                    "protocol": _value(vip.get("protocol")),
                    "port": _value(vip.get("port")),
                    "address": _value(_rel_node(vip.get("vip_ip")).get("address")),
                    "members": sorted(
                        str(_value(member.get("address"))) for member in _edge_nodes(vip.get("pool_members"))
                    ),
                    "health_checks": health_check_hfids(vip.get("health_checks")),
                    "component": _value(component.get("fqdn")),
                    "segment": _value(segment.get("name")),
                    "segment_prefix": _value(
                        _rel_node(_rel_node(segment.get("gateway")).get("ip_prefix")).get("prefix")
                    ),
                }
            )
        contexts.append(
            {
                "id": node["id"],
                "name": _value(node.get("name")),
                "vlan": _value(node.get("vlan_id")),
                "tenant": _value(_rel_node(node.get("tenant")).get("name")),
                "cluster": _value(cluster.get("name")),
                "cluster_member_ids": sorted(member["id"] for member in _edge_nodes(cluster.get("capabilities"))),
                "cluster_dcs": _cluster_dcs(cluster),
                "served": sorted(str(_value(d.get("name"))) for d in _edge_nodes(node.get("served_deployments"))),
                "vip_prefix": _value(vip_prefix.get("prefix")),
                "vip_prefix_namespace": _value(_rel_node(vip_prefix.get("ip_namespace")).get("name")),
                "legs": legs,
                "border_ports": border_ports,
                "vips": vips,
            }
        )
    logger.info("Load-balancer inventory on branch '%s': %d context(s)", branch, len(contexts))
    for context in sorted(contexts, key=lambda c: str(c["name"])):
        logger.info(
            "  LB %s vlan=%s tenant=%s vip_prefix=%s vips=%d",
            context["name"],
            context["vlan"],
            context["tenant"],
            context["vip_prefix"],
            len(context["vips"]),
        )
    return contexts


def match_loadbalancer_contexts(
    contexts: list[dict[str, Any]], dc_names: Iterable[str], dedicated_tenants: Iterable[str]
) -> tuple[dict[tuple[str, str, str | None], dict[str, Any]], list[dict[str, Any]]]:
    """Key the load-balancer contexts the suite owns by (DC, tenant namespace, dedicated tenant or None).

    The DC is found as match_transit_contexts finds it (the shared cluster's
    members' DC, a dedicated tenant's trailing DC); contexts of another DC or of
    a tenant outside ``dedicated_tenants`` are dropped. The namespace is not
    stored on a context: it is the one namespace of its legs. Returns
    (keyed, unkeyed): a shared context in no DC at all, an in-scope context
    whose legs are in no single namespace, or one whose key another context
    already holds, is unkeyed.
    """
    allowed = set(dc_names)
    tenants = set(dedicated_tenants)
    by_key: dict[tuple[str, str, str | None], dict[str, Any]] = {}
    unkeyed: list[dict[str, Any]] = []
    for context in contexts:
        tenant = context["tenant"]
        if tenant:
            dc = str(tenant).rsplit("-", 1)[-1]
            if tenant not in tenants or dc not in allowed:
                continue
        elif not context["cluster_dcs"]:
            unkeyed.append(context)
            continue
        else:
            dcs = sorted(set(context["cluster_dcs"]) & allowed)
            if not dcs:
                continue
            dc = dcs[0]
        namespaces = {str(leg["namespace"]) for leg in context["legs"] if leg["namespace"]}
        key = (dc, next(iter(namespaces)), str(tenant) if tenant else None) if len(namespaces) == 1 else None
        if key is None or key in by_key:
            unkeyed.append(context)
        else:
            by_key[key] = context
    return by_key, unkeyed
