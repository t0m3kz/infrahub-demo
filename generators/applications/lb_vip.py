"""The LoadbalancerVIPs of load_balanced AppComponents and their R_server rules, as plan specs: one LoadbalancerVIP per
(component, port, serving context), tracked by the component's application run; the pools, the addresses of the VIPs and
the pool members, and a context's policy are shared and untracked. A fully cloud component gets a CloudLoadBalancer and
a listener per port instead. ``plan_load_balancing`` is pure: it reads cleaned application data and adds specs to a plan,
collecting what cannot be written as errors. ``await_lb_inputs`` is the one imperative step: it re-reads what
add_vxlan_segment and add_customer_deployment_dc may still be writing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from typing import Any

from infrahub_sdk.protocols import CoreIPAddressPool

from generators.applications.policy_rules import RuleRequest, lb_policy_target
from generators.helpers.common import gather_bounded
from generators.helpers.lb_vip import (
    VIP_PREFIXLEN,
    cloud_lb_name,
    cloud_lb_scheme,
    cloud_lb_type,
    cloud_placement,
    component_ref,
    context_ready,
    instance_footprint,
    is_cloud_component,
    leg_prefix_id,
    leg_zone,
    listener_protocol,
    member_identifier,
    member_pool_name,
    member_prefix,
    serving_contexts,
    vip_identifier,
    vip_pool_name,
    vip_ports,
    vip_range,
)
from generators.helpers.rules import RulesPlanner
from generators.plan import AddressAllocationSpec, Hfid, NodeRef, NodeSpec, TopologyPlan
from generators.protocols import (
    AppComponent,
    CloudLoadBalancer,
    LoadbalancerVIP,
    ManagedVxlanSegment,
    TopologyCustomerDC,
)
from utils.data_cleaning import clean_data
from utils.limits import DEFAULT_NAMESPACE, LB_VIP_REREAD
from utils.ports import PortProfileHelper, PortSpec

# Inputs another load generator may still be writing are re-read at most LB_VIP_REREAD.attempts times,
# LB_VIP_REREAD.delay seconds apart (``await_lb_inputs``), after waiting for that generator
_QUERIES = Path(__file__).resolve().parents[2] / "queries/topology/add"
_SEGMENT_GATEWAYS_QUERY = _QUERIES / "lb_segment_gateways.gql"
_SERVING_CONTEXTS_QUERY = _QUERIES / "lb_serving_contexts.gql"

Edge = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]

# Re-derives a component's application and its callers (generators/applications/application_security.py): the re-run when a
# component's VIPs move to another context
APP_COMPONENT_GENERATOR = "add_app_component"


async def load_balanced_components_in(client: Any, deployment_id: str) -> list[str]:
    """Ids of the load_balanced AppComponents on a segment activated in the customer DC ``deployment_id``.
    A footprint lists no components, so the segments activated in it are read first, then the components on them."""
    segments = await client.filters(kind=ManagedVxlanSegment, customer_deployments__ids=[deployment_id])
    segment_ids = sorted({segment.id for segment in segments})
    if not segment_ids:
        return []
    components = await client.filters(kind=AppComponent, network_segment__ids=segment_ids, load_balanced__value=True)
    return sorted({component.id for component in components})


async def await_lb_inputs(generator: Any, components: list[dict[str, Any]], edges: list[Edge]) -> None:
    """Re-read the segment prefixes and serving contexts that add_vxlan_segment and add_customer_deployment_dc may still
    be writing. Each missing one waits for its owner's run, then all of a kind are re-read together and written into
    every dict of them. What is still missing afterwards is the component's error. Only own components need the segment
    prefix; cloud components are never re-read.
    """
    own_ids = {component.get("id") for component in components}
    segments: dict[str, list[dict[str, Any]]] = {}
    deployments: dict[str, list[dict[str, Any]]] = {}
    for component in [*components, *(dst for _src, _dep, dst in edges)]:
        if not component.get("load_balanced") or is_cloud_component(component):
            continue
        segment = component.get("network_segment") or {}
        if (
            component.get("id") in own_ids
            and segment.get("id")
            and segment.get("typename") == ManagedVxlanSegment.__name__
            and member_prefix(segment) is None
        ):
            segments.setdefault(segment["id"], []).append(segment)
        for instance in component.get("instances") or []:
            footprint = instance_footprint(component, instance)
            if footprint and footprint.get("id") and not context_ready(footprint.get("serving_loadbalancer_context")):
                deployments.setdefault(footprint["id"], []).append(footprint)
    # Read-only waits on different nodes and fields
    await gather_bounded(
        [
            _reread_until_ready(
                generator,
                segments,
                owner="add_vxlan_segment",
                query=_SEGMENT_GATEWAYS_QUERY,
                kind=ManagedVxlanSegment.__name__,
                field="gateway",
                ready=lambda node: member_prefix(node) is not None,
            ),
            _reread_until_ready(
                generator,
                deployments,
                owner="add_customer_deployment_dc",
                query=_SERVING_CONTEXTS_QUERY,
                kind=TopologyCustomerDC.__name__,
                field="serving_loadbalancer_context",
                ready=lambda node: context_ready(node.get("serving_loadbalancer_context")),
            ),
        ]
    )


async def _reread_until_ready(
    generator: Any,
    dicts: dict[str, list[dict[str, Any]]],
    *,
    owner: str,
    query: Path,
    kind: str,
    field: str,
    ready: Callable[[dict[str, Any]], bool],
) -> None:
    """Wait for ``owner``'s run in flight for each node of ``dicts``, then re-read ``field`` until ``ready``. One query
    per read, at most LB_VIP_REREAD.attempts reads LB_VIP_REREAD.delay seconds apart; no-op without a node.
    """
    if not dicts:
        return
    node_ids = sorted(dicts)
    generator.logger.info("Waiting for %s to write %s of %s", owner, field, ", ".join(node_ids))
    await gather_bounded(generator.wait_for_parent_generators((owner,), node_id) for node_id in node_ids)

    text = query.read_text()
    pending = node_ids
    for attempt in range(LB_VIP_REREAD.attempts):
        if attempt:
            await asyncio.sleep(LB_VIP_REREAD.delay)
        result = await generator.client.execute_graphql(query=text, variables={"ids": pending})
        for node in clean_data(result).get(kind) or []:
            for target in dicts.get(node.get("id") or "", []):
                target[field] = node.get(field)
            if ready(node):
                pending = [node_id for node_id in pending if node_id != node["id"]]
        if not pending:
            return
    generator.logger.warning("%s of %s still missing after %d reads", field, ", ".join(pending), LB_VIP_REREAD.attempts)


@dataclass
class _ServerRules:
    """The R_server rules of one context in this run: (component, segment, ports) per backend over its leg /29."""

    context: dict[str, Any]
    leg_id: str
    backends: list[tuple[dict[str, Any], dict[str, Any], list[PortSpec]]] = field(default_factory=list)


@dataclass
class LoadBalancing:
    """What ``plan_load_balancing`` decided: the VIP address of every on-prem load_balanced component, by component id,
    port and context name (a ``NodeRef`` to the address the plan allocates); the R_server rule requests; the errors that
    keep the plan from being written; the lock keys of the shared objects it creates.
    """

    vips: dict[str, dict[PortSpec, dict[str, NodeRef]]] = field(default_factory=dict)
    requests: list[RuleRequest] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    locks: list[str] = field(default_factory=list)
    _pools: set[str] = field(default_factory=set)


def plan_load_balancing(
    plan: TopologyPlan, components: list[dict[str, Any]], edges: list[Edge], *, security_profile: str
) -> LoadBalancing:
    """Add the load balancing of the run to ``plan``. Own components get their VIPs and R_server rules; an edge target of
    another application only has its VIP addresses resolved (same identifier, same address: its misconfiguration is its
    own run's error). A cloud component has no entry in ``vips``.
    """
    own_ids = {component.get("id") for component in components}
    balancing = LoadBalancing()
    server_rules: dict[str, _ServerRules] = {}
    seen: set[str] = set()
    for component in [*components, *(dst for _src, _dep, dst in edges)]:
        component_id = component.get("id")
        if not component_id or component_id in seen or not component.get("load_balanced"):
            continue
        seen.add(component_id)
        produce = component_id in own_ids
        if is_cloud_component(component):
            if produce:
                _plan_cloud_component(plan, balancing, component, security_profile)
            continue
        errors: list[str] = []
        balancing.vips[component_id] = _plan_component_vips(plan, balancing, component, produce, errors, server_rules)
        if produce:
            balancing.errors.extend(errors)
    for rules in server_rules.values():
        balancing.requests.extend(_server_rule_requests(rules))
    return balancing


def _vip_pool(plan: TopologyPlan, balancing: LoadBalancing, name: str, prefix: dict[str, Any], length: int) -> NodeRef:
    """The shared address pool ``name`` over ``prefix``: written once per plan, created only when missing."""
    if name not in balancing._pools:
        balancing._pools.add(name)
        balancing.locks.append(f"address-pool-{name}")
        namespace_id = (prefix.get("ip_namespace") or {}).get("id")
        plan.add(
            NodeSpec(
                f"pool-{name}",
                CoreIPAddressPool,
                {"name": name},
                hfid=Hfid("CoreIPAddressPool", (name,)),
                ownership="shared",
                create_only={
                    "default_address_type": "IpamIPAddress",
                    "default_prefix_length": length,
                    "ip_namespace": {"id": namespace_id} if namespace_id else {"hfid": [DEFAULT_NAMESPACE]},
                    "resources": [prefix["id"]],
                },
            )
        )
    return NodeRef(f"pool-{name}")


def _plan_component_vips(
    plan: TopologyPlan,
    balancing: LoadBalancing,
    component: dict[str, Any],
    produce: bool,
    errors: list[str],
    server_rules: dict[str, _ServerRules],
) -> dict[PortSpec, dict[str, NodeRef]]:
    """The component's VIP addresses by port and serving context name; with ``produce``, its LoadbalancerVIPs and the
    R_server rules queued on ``server_rules``. A VIP is a /32 in the context's tenant namespace.
    """
    ports, port_errors = vip_ports(component)
    contexts, context_errors = serving_contexts(component)
    errors.extend([*port_errors, *context_errors])
    fqdn = component_ref(component)
    segment = component.get("network_segment") or {}
    addresses: dict[PortSpec, dict[str, NodeRef]] = {}
    for context, instances in contexts.values():
        context_name = str(context.get("name") or context["id"])
        vip_prefix = vip_range(context)
        if vip_prefix is None:
            errors.append(f"{fqdn}: load balancer context {context_name} has no VIP prefix in a namespace")
            continue
        pool_name = vip_pool_name(vip_prefix["id"])
        vip_pool = _vip_pool(plan, balancing, pool_name, vip_prefix, VIP_PREFIXLEN)
        member_ids: list[str] = []
        leg_id = None
        if produce:
            leg_id = leg_prefix_id(context)
            if leg_id is None:
                errors.append(f"{fqdn}: load balancer context {context_name} has no single leg prefix")
                continue
            reserved = _plan_pool_members(plan, balancing, component, segment, instances, errors)
            if reserved is None:
                continue
            member_ids = reserved
        context_ports: list[PortSpec] = []
        for port in ports:
            identifier = vip_identifier(component["id"], port)
            address_key = f"vip-address-{pool_name}-{identifier}"
            plan.add(
                AddressAllocationSpec(
                    address_key,
                    identifier,
                    VIP_PREFIXLEN,
                    f"VIP {fqdn} {PortProfileHelper.format_port_spec(port)}",
                    "shared",
                    pool_id=vip_pool,
                )
            )
            addresses.setdefault(port, {})[context_name] = NodeRef(address_key)
            if produce:
                _plan_vip(
                    plan, component, context, port, member_ids, f"vip-{context['id']}", vip_ip=NodeRef(address_key)
                )
                context_ports.append(port)
        if leg_id and context_ports:
            rules = server_rules.setdefault(context["id"], _ServerRules(context=context, leg_id=leg_id))
            rules.backends.append((component, segment, context_ports))
    return addresses


def _plan_pool_members(
    plan: TopologyPlan,
    balancing: LoadBalancing,
    component: dict[str, Any],
    segment: dict[str, Any],
    instances: list[dict[str, Any]],
    errors: list[str],
) -> list[str] | None:
    """The ids of the servers the VIP balances across; None (an error) without a segment prefix. A member is the server:
    its primary address, or one reserved in the segment prefix (keyed by instance and segment) and kept as its primary
    address.
    """
    prefix = member_prefix(segment)
    if prefix is None:
        errors.append(
            f"{component_ref(component)}: segment {segment.get('name', '<none>')} has no gateway prefix for the pool "
            "members"
        )
        return None
    prefix_length = ip_network(prefix["prefix"], strict=False).prefixlen
    pool_name = member_pool_name(prefix["id"])
    pool = _vip_pool(plan, balancing, pool_name, prefix, prefix_length)
    member_ids: list[str] = []
    for instance in sorted(instances, key=lambda item: str(item.get("name") or item["id"])):
        if not (instance.get("primary_address") or {}).get("id"):
            identifier = member_identifier(instance["id"], segment["id"])
            address_key = f"member-address-{pool_name}-{identifier}"
            plan.add(
                AddressAllocationSpec(
                    address_key,
                    identifier,
                    prefix_length,
                    f"Address of {instance.get('name', instance['id'])} in {segment.get('name', segment['id'])}",
                    "shared",
                    pool_id=pool,
                )
            )
            primary = NodeRef(address_key)
            plan.add(
                NodeSpec(
                    f"primary-address-{instance['id']}",
                    str(instance["typename"]),
                    {"id": instance["id"], "primary_address": {"id": primary}},
                    ownership="shared",
                )
            )
            instance["primary_address"] = {"id": primary}
        member_ids.append(instance["id"])
    return member_ids


def _plan_cloud_component(
    plan: TopologyPlan, balancing: LoadBalancing, component: dict[str, Any], security_profile: str
) -> None:
    """The component's CloudLoadBalancer and its listeners (one per port), tracked by this run. Needs its one virtual
    network; the targets are the instances themselves.
    """
    ports, port_errors = vip_ports(component)
    network, segments, placement_errors = cloud_placement(component)
    balancing.errors.extend([*port_errors, *placement_errors])
    if network is None:
        return
    member_ids = [
        instance["id"]
        for instance in sorted(component.get("instances") or [], key=lambda item: str(item.get("name") or item["id"]))
    ]
    name = cloud_lb_name(component)
    balancing.locks.append(f"cloud-lb-{name}")
    plan.add(
        NodeSpec(
            f"cloud-lb-{name}",
            CloudLoadBalancer,
            {
                "name": name,
                "description": f"Cloud load balancer of {component_ref(component)}",
                "status": "active",
                "lb_type": cloud_lb_type(component),
                "scheme": cloud_lb_scheme(security_profile),
                "virtual_network": {"id": network["id"]},
                "network_segments": [segment["id"] for segment in segments],
            },
            ownership="owned",
        )
    )
    load_balancer = {"id": NodeRef(f"cloud-lb-{name}"), "name": name}
    for port in ports:
        protocol, protocol_error = listener_protocol(component, port)
        if protocol is None:
            balancing.errors.append(str(protocol_error))
            continue
        _plan_vip(plan, component, load_balancer, port, member_ids, f"vip-{name}", protocol=protocol)


def _plan_vip(
    plan: TopologyPlan,
    component: dict[str, Any],
    load_balancer: dict[str, Any],
    port: PortSpec,
    member_ids: list[str],
    key_prefix: str,
    *,
    vip_ip: NodeRef | None = None,
    protocol: str | None = None,
) -> None:
    """The LoadbalancerVIP of (component, port) on ``load_balancer``, tracked. ``load_balancer`` is a context or a
    CloudLoadBalancer (its id, a ``NodeRef`` for a new one, and name); a cloud VIP has no ``vip_ip``. ``protocol`` is
    the listener's.
    """
    port_protocol, port_start, _port_end = port
    protocol = protocol or port_protocol
    fqdn = component_ref(component)
    plan.add(
        NodeSpec(
            f"{key_prefix}-{component['id']}-{port_protocol}-{port_start}",
            LoadbalancerVIP,
            {
                "hostname": fqdn,
                "protocol": protocol,
                "port": port_start,
                "status": "active",
                "description": f"{fqdn} {PortProfileHelper.format_port_spec(port)} on {load_balancer.get('name')}",
                "load_balancer": {"id": load_balancer["id"]},
                **({"vip_ip": {"id": vip_ip}} if vip_ip else {}),
                "pool_members": member_ids,
                **({"ssl_certificate": component["lb_certificate"]} if protocol == "https" else {}),
                "health_checks": [check["id"] for check in component.get("health_checks") or [] if check.get("id")],
            },
            ownership="owned",
        )
    )


def _server_rule_requests(rules: _ServerRules) -> list[RuleRequest]:
    """The R_server rules of one context, in its policy lb-{context}."""
    context_name = str(rules.context.get("name") or rules.context["id"])
    target = lb_policy_target(context_name)
    requests: list[RuleRequest] = []
    for component, segment, ports in rules.backends:
        for port in ports:
            rule_name = RulesPlanner.lb_server_rule_name(component, port)
            payload = RulesPlanner.build_lb_server_rule_payload(
                rule_name=rule_name,
                context_name=context_name,
                component=component,
                segment=segment,
                leg_prefix_id=rules.leg_id,
                leg_zone=leg_zone(rules.context),
                port=port,
            )
            requests.append(RuleRequest(target, rule_name, payload))
    return requests
