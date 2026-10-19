"""Mixin producing the LoadbalancerVIPs of load_balanced AppComponents and their R_server rules."""

from __future__ import annotations

from collections.abc import Awaitable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from ipaddress import ip_network
from typing import TYPE_CHECKING, Any, Callable

from infrahub_sdk.protocols import CoreIPAddressPool

from utils.ports import PortProfileHelper, PortSpec

from .helpers.lb_vip import (
    VIP_PREFIXLEN,
    component_ref,
    leg_prefix_id,
    member_identifier,
    member_pool_name,
    serving_contexts,
    vip_identifier,
    vip_pool_name,
    vip_ports,
)
from .helpers.rules import RulesPlanner
from .protocols import LoadbalancerVIP, SecurityPolicy

if TYPE_CHECKING:
    import logging

Edge = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]


@dataclass
class _ServerRules:
    """The R_server rules of one context in this run: (component, segment, ports) per backend over its leg /29."""

    context: dict[str, Any]
    leg_id: str
    backends: list[tuple[dict[str, Any], dict[str, Any], list[PortSpec]]] = field(default_factory=list)


class LoadbalancerVipMixin:
    """VIPs from AppComponent.load_balanced, for AppApplicationGenerator.

    One LoadbalancerVIP per (load_balanced component, port, load balancer
    context serving the customer deployment of its instances): the VIP /32
    from the context's VIP range, one pool-member address per instance from
    the component's segment prefix, the component's health checks. And per
    VIP the R_server rule, leg /29 -> backend segment on the port, in the
    context's segment-less policy lb-{context}. All of it tracked by the
    component's own application run, so a component that is no longer
    load_balanced (or loses a port or a context) loses its VIPs and R_server
    rules to that run's delete_unused_nodes. The pools and allocated
    addresses are untracked: shared by every application on the context or
    segment, and the allocation identifier returns the same address when a
    VIP comes back.

    Known gap: nothing re-runs this when a deployment's serving context changes
    after its applications ran, so a dedicated -> shared move keeps stale VIPs.

    Expects the host class to provide: ``client``, ``logger``,
    ``resource_lock`` (PoolMixin), ``ensure_prefix_address_pool`` and
    ``allocate_prefix_address`` (CablingMixin), ``_get_or_create_by_name``
    (GetOrCreateByNameMixin) and ``_upsert_policy_rule`` (SegmentFirewallMixin).
    """

    client: Any
    logger: logging.Logger
    resource_lock: Callable[[str], AbstractAsyncContextManager[None]]
    ensure_prefix_address_pool: Callable[..., Awaitable[Any]]
    allocate_prefix_address: Callable[..., Awaitable[str | None]]
    _get_or_create_by_name: Callable[..., Awaitable[Any]]
    _upsert_policy_rule: Callable[..., Awaitable[int | None]]

    async def _reconcile_load_balancing(
        self,
        components: list[dict[str, Any]],
        edges: list[Edge],
        rule_indexes: dict[str, set[int]],
    ) -> tuple[dict[str, dict[PortSpec, dict[str, str]]], list[str]]:
        """The VIP address ids of every load_balanced component of the run, by
        component id, port and context name, and the errors (a range, no
        serving context, ...) of the application's own components.

        The application's own components get their VIPs produced, then the
        R_server rules of every context they use, one context at a time. An
        edge target of another application only has its VIP addresses
        resolved, for this run's R_client rules: the allocation identifier
        yields the same address its own run puts on the VIP, whichever runs
        first. What is wrong with such a target is its own run's error, never
        its callers' (a misconfigured component would fail every one of them);
        a caller reports only the used port that has no VIP
        (application_security.py).
        """
        own_ids = {component.get("id") for component in components}
        vips: dict[str, dict[PortSpec, dict[str, str]]] = {}
        errors: list[str] = []
        pools: dict[str, Any] = {}
        server_rules: dict[str, _ServerRules] = {}
        for component in [*components, *(dst for _src, _dep, dst in edges)]:
            component_id = component.get("id")
            if not component_id or component_id in vips or not component.get("load_balanced"):
                continue
            produce = component_id in own_ids
            vips[component_id] = await self._component_vips(
                component, produce=produce, pools=pools, errors=errors if produce else [], server_rules=server_rules
            )
        for rules in server_rules.values():
            await self._reconcile_lb_server_rules(rules, rule_indexes)
        return vips, errors

    async def _component_vips(
        self,
        component: dict[str, Any],
        *,
        produce: bool,
        pools: dict[str, Any],
        errors: list[str],
        server_rules: dict[str, _ServerRules],
    ) -> dict[PortSpec, dict[str, str]]:
        """The component's VIP address ids by port and serving context name; with
        ``produce``, its LoadbalancerVIPs written and its R_server rules queued
        on server_rules (by context id).

        A VIP is a /32 in the context's tenant namespace (its VIP range's): the
        rules match it exactly and the border's PBR bypass reads the namespace.
        """
        ports, port_errors = vip_ports(component)
        contexts, context_errors = serving_contexts(component)
        errors.extend([*port_errors, *context_errors])
        fqdn = component_ref(component)
        segment = component.get("network_segment") or {}
        addresses: dict[PortSpec, dict[str, str]] = {}
        for context, instances in contexts.values():
            context_name = str(context.get("name") or context["id"])
            vip_prefix = context.get("vip_prefix") or {}
            if not vip_prefix.get("id") or not (vip_prefix.get("ip_namespace") or {}).get("id"):
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
                if produce and await self._save_vip(component, context, port, vip_ip_id, member_ids):
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
        """One address per instance in the component's segment prefix; None (an error) without a prefix.

        Keyed by (instance, segment): two components on one host and segment
        share the host's address.
        """
        fqdn = component_ref(component)
        prefix = (segment.get("gateway") or {}).get("ip_prefix") or {}
        if not segment.get("id") or not prefix.get("id") or not prefix.get("prefix") or not prefix.get("ip_namespace"):
            errors.append(f"{fqdn}: segment {segment.get('name', '<none>')} has no gateway prefix for the pool members")
            return None
        prefix_length = ip_network(prefix["prefix"], strict=False).prefixlen
        pool = await self._shared_address_pool(pools, member_pool_name(prefix["id"]), prefix, prefix_length)
        if pool is None:
            return None
        member_ids: list[str] = []
        for instance in sorted(instances, key=lambda item: str(item.get("name") or item["id"])):
            member_id = await self.allocate_prefix_address(
                pool=pool,
                identifier=member_identifier(instance["id"], segment["id"]),
                prefix_length=prefix_length,
                description=f"Load balancer pool member {instance.get('name', instance['id'])}",
            )
            if member_id is not None:
                member_ids.append(member_id)
        return member_ids

    async def _save_vip(
        self,
        component: dict[str, Any],
        context: dict[str, Any],
        port: PortSpec,
        vip_ip_id: str,
        member_ids: list[str],
    ) -> bool:
        """Upsert the LoadbalancerVIP of (component, port) on ``context``, tracked by this run; False (logged) on failure."""
        protocol, port_start, _port_end = port
        fqdn = component_ref(component)
        listener = f"{fqdn} {PortProfileHelper.format_port_spec(port)} on {context.get('name')}"
        try:
            vip = await self.client.create(
                kind=LoadbalancerVIP,
                data={
                    "hostname": fqdn,
                    "protocol": protocol,
                    "port": port_start,
                    "status": "active",
                    "description": listener,
                    "load_balancer": {"id": context["id"]},
                    "vip_ip": {"id": vip_ip_id},
                    "component": {"id": component["id"]},
                    "pool_members": member_ids,
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
        """The run's R_server rules of one context in its policy lb-{context}, under one lock.

        The policy is shared by every application the context balances, so it
        is untracked, read once per run and context, and its rule indexes
        ([policy, index] is unique) are read again once the lock is held; each
        rule is tracked by this run. The lock is the policy's own, not the
        boarding run's lb-context-{context}, which only builds the context.
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
