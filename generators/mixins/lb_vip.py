"""Mixin producing the LoadbalancerVIPs of load_balanced AppComponents and their R_server rules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from infrahub_sdk.protocols import CoreIPAddressPool

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
from generators.protocols import (
    AppComponent,
    CloudLoadBalancer,
    LoadbalancerVIP,
    ManagedVxlanSegment,
    SecurityPolicy,
    TopologyCustomerDC,
)
from utils.data_cleaning import clean_data
from utils.limits import LB_VIP_REREAD
from utils.ports import PortProfileHelper, PortSpec

# Inputs another load generator may still be writing are re-read at most LB_VIP_REREAD.attempts times,
# LB_VIP_REREAD.delay seconds apart (_await_lb_inputs), after waiting for that generator
_QUERIES = Path(__file__).resolve().parents[2] / "queries/topology/add"
_SEGMENT_GATEWAYS_QUERY = _QUERIES / "lb_segment_gateways.gql"
_SERVING_CONTEXTS_QUERY = _QUERIES / "lb_serving_contexts.gql"

if TYPE_CHECKING:
    import logging

    from generators.mixins.common import CommonGenerator
    from generators.mixins.connections import CablingMixin
    from generators.mixins.pools import PoolMixin
    from generators.mixins.segment_firewall import SegmentFirewallMixin

Edge = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]

# Re-derives a component's application and its callers (generators/topology/application_security.py): the re-run when a
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


@dataclass
class _ServerRules:
    """The R_server rules of one context in this run: (component, segment, ports) per backend over its leg /29."""

    context: dict[str, Any]
    leg_id: str
    backends: list[tuple[dict[str, Any], dict[str, Any], list[PortSpec]]] = field(default_factory=list)


class LoadbalancerVipMixin:
    """VIPs from AppComponent.load_balanced, for AppApplicationGenerator: one LoadbalancerVIP per (component, port,
    serving context). Tracked by the component's application run; pools and addresses are untracked. A fully cloud
    component gets a CloudLoadBalancer instead. The host provides the client, logger and helper methods.
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        _upsert_policy_rule = SegmentFirewallMixin._upsert_policy_rule
        allocate_prefix_address = CablingMixin.allocate_prefix_address
        attach_refs = PoolMixin.attach_refs
        ensure_prefix_address_pool = CablingMixin.ensure_prefix_address_pool
        resource_lock = PoolMixin.resource_lock
        wait_for_parent_generators = CommonGenerator.wait_for_parent_generators
    _get_or_create_by_name: Callable[..., Awaitable[Any]]

    # Between two re-reads (_poll_until_ready); tests replace it
    lb_reread_sleep = staticmethod(asyncio.sleep)

    async def _reconcile_load_balancing(
        self,
        components: list[dict[str, Any]],
        edges: list[Edge],
        rule_indexes: dict[str, set[int]],
        *,
        security_profile: str,
    ) -> tuple[dict[str, dict[PortSpec, dict[str, str]]], list[str]]:
        """VIP address ids of every on-prem load_balanced component of the run, by component id, port and context name,
        and the errors. Own components get their VIPs produced, then the R_server rules per context. An edge target of
        another application only has its VIP addresses resolved (same identifier, same address); its misconfiguration is
        its own run's error. A cloud component has no entry.
        """
        own_ids = {component.get("id") for component in components}
        await self._await_lb_inputs(components, edges)
        vips: dict[str, dict[PortSpec, dict[str, str]]] = {}
        errors: list[str] = []
        pools: dict[str, Any] = {}
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
                    await self._cloud_component_vips(component, security_profile=security_profile, errors=errors)
                continue
            vips[component_id] = await self._component_vips(
                component, produce=produce, pools=pools, errors=errors if produce else [], server_rules=server_rules
            )
        for rules in server_rules.values():
            await self._reconcile_lb_server_rules(rules, rule_indexes)
        return vips, errors

    async def _await_lb_inputs(self, components: list[dict[str, Any]], edges: list[Edge]) -> None:
        """Re-read the segment prefixes and serving contexts that add_vxlan_segment and add_customer_deployment_dc may
        still be writing. Each missing one waits for its owner's run, then all of a kind are re-read together and
        written into every dict of them. What is still missing afterwards is the component's error. Only own components
        need the segment prefix; cloud components are never re-read.
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
                if (
                    footprint
                    and footprint.get("id")
                    and not context_ready(footprint.get("serving_loadbalancer_context"))
                ):
                    deployments.setdefault(footprint["id"], []).append(footprint)
        await self._reread_until_ready(
            segments,
            owner="add_vxlan_segment",
            query=_SEGMENT_GATEWAYS_QUERY,
            kind=ManagedVxlanSegment.__name__,
            field="gateway",
            ready=lambda node: member_prefix(node) is not None,
        )
        await self._reread_until_ready(
            deployments,
            owner="add_customer_deployment_dc",
            query=_SERVING_CONTEXTS_QUERY,
            kind=TopologyCustomerDC.__name__,
            field="serving_loadbalancer_context",
            ready=lambda node: context_ready(node.get("serving_loadbalancer_context")),
        )

    async def _reread_until_ready(
        self,
        dicts: dict[str, list[dict[str, Any]]],
        *,
        owner: str,
        query: Path,
        kind: str,
        field: str,
        ready: Callable[[dict[str, Any]], bool],
    ) -> None:
        """Wait for ``owner``'s run in flight for each node of ``dicts``, then re-read ``field`` until ``ready``. One
        query per read, at most LB_VIP_REREAD.attempts reads LB_VIP_REREAD.delay seconds apart; no-op without a node.
        """
        if not dicts:
            return
        node_ids = sorted(dicts)
        self.logger.info("Waiting for %s to write %s of %s", owner, field, ", ".join(node_ids))
        for node_id in node_ids:
            await self.wait_for_parent_generators((owner,), node_id)
        from utils.query_flatten import load_flat_query

        text = load_flat_query(query)
        pending = node_ids
        for attempt in range(LB_VIP_REREAD.attempts):
            if attempt:
                await self.lb_reread_sleep(LB_VIP_REREAD.delay)
            result = await self.client.execute_graphql(query=text, variables={"ids": pending})
            for node in clean_data(result).get(kind) or []:
                for target in dicts.get(node.get("id") or "", []):
                    target[field] = node.get(field)
                if ready(node):
                    pending = [node_id for node_id in pending if node_id != node["id"]]
            if not pending:
                return
        self.logger.warning("%s of %s still missing after %d reads", field, ", ".join(pending), LB_VIP_REREAD.attempts)

    async def _component_vips(
        self,
        component: dict[str, Any],
        *,
        produce: bool,
        pools: dict[str, Any],
        errors: list[str],
        server_rules: dict[str, _ServerRules],
    ) -> dict[PortSpec, dict[str, str]]:
        """The component's VIP address ids by port and serving context name; with ``produce``, its LoadbalancerVIPs
        written and R_server rules queued on server_rules. A VIP is a /32 in the context's tenant namespace.
        """
        ports, port_errors = vip_ports(component)
        contexts, context_errors = serving_contexts(component)
        errors.extend([*port_errors, *context_errors])
        fqdn = component_ref(component)
        segment = component.get("network_segment") or {}
        addresses: dict[PortSpec, dict[str, str]] = {}
        for context, instances in contexts.values():
            context_name = str(context.get("name") or context["id"])
            vip_prefix = vip_range(context)
            if vip_prefix is None:
                errors.append(f"{fqdn}: load balancer context {context_name} has no VIP prefix in a namespace")
                continue
            vip_pool = await self._shared_address_pool(
                pools, vip_pool_name(vip_prefix["id"]), vip_prefix, VIP_PREFIXLEN
            )
            if vip_pool is None:
                continue
            member_ids: list[str] = []
            leg_id = None
            if produce:
                leg_id = leg_prefix_id(context)
                if leg_id is None:
                    errors.append(f"{fqdn}: load balancer context {context_name} has no single leg prefix")
                    continue
                reserved = await self._pool_members(component, segment, instances, pools, errors)
                if reserved is None:
                    continue
                member_ids = reserved
            context_ports: list[PortSpec] = []
            for port in ports:
                vip_ip_id = await self.allocate_prefix_address(
                    pool=vip_pool,
                    identifier=vip_identifier(component["id"], port),
                    prefix_length=VIP_PREFIXLEN,
                    description=f"VIP {fqdn} {PortProfileHelper.format_port_spec(port)}",
                )
                if vip_ip_id is None:
                    continue
                addresses.setdefault(port, {})[context_name] = vip_ip_id
                if produce and await self._save_vip(component, context, port, member_ids, vip_ip_id=vip_ip_id):
                    context_ports.append(port)
            if leg_id and context_ports:
                rules = server_rules.setdefault(context["id"], _ServerRules(context=context, leg_id=leg_id))
                rules.backends.append((component, segment, context_ports))
        return addresses

    async def _shared_address_pool(
        self, pools: dict[str, Any], name: str, prefix: dict[str, Any], prefix_length: int
    ) -> Any | None:
        """The untracked address pool ``name`` over ``prefix``, created once under its lock; cached per run."""
        if name not in pools:
            existing = await self.client.filters(kind=CoreIPAddressPool, name__value=name)
            if existing:
                pools[name] = existing[0]
            else:
                async with self.resource_lock(f"address-pool-{name}"):
                    pools[name] = await self.ensure_prefix_address_pool(
                        pool_name=name,
                        prefix_id=prefix["id"],
                        prefix_length=prefix_length,
                        namespace_id=(prefix.get("ip_namespace") or {}).get("id"),
                        track=False,
                    )
        return pools[name]

    async def _pool_members(
        self,
        component: dict[str, Any],
        segment: dict[str, Any],
        instances: list[dict[str, Any]],
        pools: dict[str, Any],
        errors: list[str],
    ) -> list[str] | None:
        """The ids of the servers the VIP balances across; None (an error) without a segment prefix. A member is the
        server: its primary address, or one reserved in the segment prefix (keyed by instance and segment) and kept as
        its primary address.
        """
        fqdn = component_ref(component)
        prefix = member_prefix(segment)
        if prefix is None:
            errors.append(f"{fqdn}: segment {segment.get('name', '<none>')} has no gateway prefix for the pool members")
            return None
        prefix_length = ip_network(prefix["prefix"], strict=False).prefixlen
        pool = await self._shared_address_pool(pools, member_pool_name(prefix["id"]), prefix, prefix_length)
        if pool is None:
            return None
        member_ids: list[str] = []
        for instance in sorted(instances, key=lambda item: str(item.get("name") or item["id"])):
            primary = (instance.get("primary_address") or {}).get("id")
            if not primary:
                primary = await self.allocate_prefix_address(
                    pool=pool,
                    identifier=member_identifier(instance["id"], segment["id"]),
                    prefix_length=prefix_length,
                    description=f"Address of {instance.get('name', instance['id'])} in {segment.get('name', segment['id'])}",
                )
                if primary is None:
                    continue
                await self.attach_refs(
                    kind=str(instance["typename"]),
                    node_id=instance["id"],
                    refs={"primary_address": primary},
                    current={"primary_address": (instance.get("primary_address") or {}).get("id")},
                )
                instance["primary_address"] = {"id": primary}
            member_ids.append(instance["id"])
        return member_ids

    async def _cloud_component_vips(
        self, component: dict[str, Any], *, security_profile: str, errors: list[str]
    ) -> None:
        """Upsert the component's CloudLoadBalancer and its listeners (one per port), tracked by this run.
        Needs its one virtual network; the targets are the instances themselves."""
        ports, port_errors = vip_ports(component)
        network, segments, placement_errors = cloud_placement(component)
        errors.extend([*port_errors, *placement_errors])
        if network is None:
            return
        member_ids = [
            instance["id"]
            for instance in sorted(
                component.get("instances") or [], key=lambda item: str(item.get("name") or item["id"])
            )
        ]
        load_balancer = await self._save_cloud_lb(component, network, segments, security_profile, errors)
        if load_balancer is None:
            return
        for port in ports:
            protocol, protocol_error = listener_protocol(component, port)
            if protocol is None:
                errors.append(str(protocol_error))
                continue
            await self._save_vip(component, load_balancer, port, member_ids, protocol=protocol)

    async def _save_cloud_lb(
        self,
        component: dict[str, Any],
        network: dict[str, Any],
        segments: list[dict[str, Any]],
        security_profile: str,
        errors: list[str],
    ) -> dict[str, str] | None:
        """Upsert the component's CloudLoadBalancer under its name's lock, tracked; returns its id and name, or None (an
        error).
        """
        name = cloud_lb_name(component)
        try:
            async with self.resource_lock(f"cloud-lb-{name}"):
                load_balancer = await self.client.create(
                    kind=CloudLoadBalancer,
                    data={
                        "name": name,
                        "description": f"Cloud load balancer of {component_ref(component)}",
                        "status": "active",
                        "lb_type": cloud_lb_type(component),
                        "scheme": cloud_lb_scheme(security_profile),
                        "virtual_network": {"id": network["id"]},
                        "network_segments": [segment["id"] for segment in segments],
                    },
                )
                await load_balancer.save(allow_upsert=True)
        except Exception as exc:
            errors.append(f"{component_ref(component)}: cloud load balancer {name}: {exc}")
            return None
        self.logger.info("  Cloud load balancer %s in %s", name, network.get("name", network["id"]))
        return {"id": load_balancer.id, "name": name}

    async def _save_vip(
        self,
        component: dict[str, Any],
        load_balancer: dict[str, Any],
        port: PortSpec,
        member_ids: list[str],
        *,
        vip_ip_id: str | None = None,
        protocol: str | None = None,
    ) -> bool:
        """Upsert the LoadbalancerVIP of (component, port) on ``load_balancer``, tracked; False (logged) on failure.
        ``load_balancer`` is a context or a CloudLoadBalancer (id and name); a cloud VIP has no ``vip_ip_id``.
        ``protocol`` is the listener's.
        """
        port_protocol, port_start, _port_end = port
        protocol = protocol or port_protocol
        fqdn = component_ref(component)
        listener = f"{fqdn} {PortProfileHelper.format_port_spec(port)} on {load_balancer.get('name')}"
        try:
            vip = await self.client.create(
                kind=LoadbalancerVIP,
                data={
                    "hostname": fqdn,
                    "protocol": protocol,
                    "port": port_start,
                    "status": "active",
                    "description": listener,
                    "load_balancer": {"id": load_balancer["id"]},
                    **({"vip_ip": {"id": vip_ip_id}} if vip_ip_id else {}),
                    "pool_members": member_ids,
                    **({"ssl_certificate": component["lb_certificate"]} if protocol == "https" else {}),
                    "health_checks": [check["id"] for check in component.get("health_checks") or [] if check.get("id")],
                },
            )
            await vip.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error(f"Failed to save the VIP {listener}: {exc}")
            return False
        self.logger.info("  VIP %s", listener)
        return True

    async def _reconcile_lb_server_rules(self, rules: _ServerRules, rule_indexes: dict[str, set[int]]) -> None:
        """The run's R_server rules of one context in its policy lb-{context}, under one lock. The policy is shared and
        untracked, read once per run and context; rule indexes are re-read under the lock; each rule is tracked.
        """
        context = rules.context
        context_name = str(context.get("name") or context["id"])
        policy_name = RulesPlanner.lb_policy_name(context_name)
        async with self.resource_lock(f"lb-policy-{context_name}"):
            policy = await self._get_or_create_by_name(
                kind=SecurityPolicy,
                name=policy_name,
                create_data={
                    "name": policy_name,
                    "description": f"Load balancer context {context_name} to its backends",
                    "default_action": "deny",
                    "enabled": True,
                },
                created_log="Created policy: %s",
                track=False,
            )
            if policy is None:
                return
            rule_indexes.pop(policy.id, None)
            for component, segment, ports in rules.backends:
                for port in ports:
                    rule_name = RulesPlanner.lb_server_rule_name(component, port)
                    try:
                        index = await self._upsert_policy_rule(
                            policy_id=policy.id,
                            rule_name=rule_name,
                            rule_data=RulesPlanner.build_lb_server_rule_payload(
                                policy_id=policy.id,
                                rule_name=rule_name,
                                context_name=context_name,
                                component=component,
                                segment=segment,
                                leg_prefix_id=rules.leg_id,
                                leg_zone=leg_zone(rules.context),
                                port=port,
                            ),
                            rule_indexes=rule_indexes,
                        )
                    except Exception as exc:
                        self.logger.error(
                            f"Failed to write R_server '{rule_name}' for {component_ref(component)} "
                            f"{PortProfileHelper.format_port_spec(port)} on {context_name}: {exc}"
                        )
                        continue
                    if index is not None:
                        self.logger.info("  Created rule [%d] '%s' in %s", index, rule_name, policy_name)
