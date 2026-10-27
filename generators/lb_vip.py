"""Mixin producing the LoadbalancerVIPs of load_balanced AppComponents and their R_server rules."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from ipaddress import ip_network
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from infrahub_sdk.protocols import CoreIPAddressPool

from utils.data_cleaning import clean_data
from utils.limits import GROUP_CLOUD_LOADBALANCERS, LB_VIP_REREAD
from utils.ports import PortProfileHelper, PortSpec

from .helpers.lb_vip import (
    CLOUD_LB_TYPE,
    VIP_PREFIXLEN,
    cloud_lb_name,
    cloud_lb_scheme,
    cloud_members,
    cloud_placement,
    component_ref,
    context_ready,
    is_cloud_component,
    leg_prefix_id,
    member_identifier,
    member_pool_name,
    member_prefix,
    serving_contexts,
    vip_identifier,
    vip_pool_name,
    vip_ports,
    vip_range,
)
from .helpers.rules import RulesPlanner
from .protocols import (
    AppComponent,
    CloudLoadBalancer,
    DcimPhysicalDevice,
    DcimVirtualDevice,
    LoadbalancerVIP,
    ManagedVxlanSegment,
    SecurityPolicy,
    TopologyCustomerDC,
)

# The artifact group of cloud load balancers (data/bootstrap/00_groups.yml,
# target of the cloud_lb_terraform artifact definition).
CLOUD_LB_GROUP = {"hfid": [GROUP_CLOUD_LOADBALANCERS], "kind": "CoreStandardGroup"}

# What another generator of the same load can still be writing when this run
# reads it is re-read at most LB_VIP_REREAD.attempts times, LB_VIP_REREAD.delay seconds apart
# (_await_lb_inputs), after waiting for that generator's run in flight.
_QUERIES = Path(__file__).resolve().parents[1] / "queries/topology/add"
_SEGMENT_GATEWAYS_QUERY = _QUERIES / "lb_segment_gateways.gql"
_SERVING_CONTEXTS_QUERY = _QUERIES / "lb_serving_contexts.gql"

if TYPE_CHECKING:
    import logging

    from .common import CommonGenerator
    from .connections import CablingMixin
    from .pools import PoolMixin
    from .segment_firewall import SegmentFirewallMixin

Edge = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]

# Re-derives a component's application and every application calling it
# (generators/topology/application_security.py), so it is the re-run for a
# component whose VIPs moved to another context.
APP_COMPONENT_GENERATOR = "add_app_component"


async def load_balanced_components_in(client: Any, deployment_id: str) -> list[str]:
    """Ids of the load_balanced AppComponents with an instance deployed to the customer DC ``deployment_id``.

    Their VIPs and R_server rules sit on the deployment's serving context. The
    two device kinds carry ``deployment`` each (a footprint lists no devices
    of its own), read together, then the components running on them.
    """
    devices = await asyncio.gather(
        *(
            client.filters(kind=kind, deployment__ids=[deployment_id])
            for kind in (DcimPhysicalDevice, DcimVirtualDevice)
        )
    )
    device_ids = sorted({device.id for kind_devices in devices for device in kind_devices})
    if not device_ids:
        return []
    components = await client.filters(kind=AppComponent, instances__ids=device_ids, load_balanced__value=True)
    return sorted({component.id for component in components})


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

    A component whose instances are all in the cloud (is_cloud_component)
    gets one CloudLoadBalancer in their virtual network instead, with a VIP
    per port without an address (the cloud assigns it) and the instances'
    own private addresses as pool members; the load balancer and its VIPs
    are tracked the same way, the member addresses are the instances' and
    untracked. No context, VIP range or rule: its callers reach it directly.

    A deployment that moves to another serving context after its applications
    ran (dedicated <-> shared) re-runs them: add_customer_deployment_dc, the
    one writer of serving_loadbalancer_context, fans out add_app_component to
    load_balanced_components_in(deployment) on the relink (LOADBALANCER_CONTEXT
    in generators/firewall_context.py), and that re-runs each component's
    application and its callers, whose R_client rules name the VIPs.

    Expects the host class to provide: ``client``, ``logger``,
    ``wait_for_parent_generators`` (CommonGenerator),
    ``resource_lock`` (PoolMixin), ``ensure_prefix_address_pool``,
    ``allocate_prefix_address`` and ``upsert_ip_address`` (CablingMixin),
    ``_get_or_create_by_name`` (GetOrCreateByNameMixin) and
    ``_upsert_policy_rule`` (SegmentFirewallMixin).
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        _upsert_policy_rule = SegmentFirewallMixin._upsert_policy_rule
        allocate_prefix_address = CablingMixin.allocate_prefix_address
        ensure_prefix_address_pool = CablingMixin.ensure_prefix_address_pool
        resource_lock = PoolMixin.resource_lock
        upsert_ip_address = CablingMixin.upsert_ip_address
        wait_for_parent_generators = CommonGenerator.wait_for_parent_generators
    _get_or_create_by_name: Callable[..., Awaitable[Any]]

    # Between two re-reads (_poll_until_ready); tests replace it.
    lb_reread_sleep = staticmethod(asyncio.sleep)

    async def _reconcile_load_balancing(
        self,
        components: list[dict[str, Any]],
        edges: list[Edge],
        rule_indexes: dict[str, set[int]],
        *,
        security_profile: str,
    ) -> tuple[dict[str, dict[PortSpec, dict[str, str]]], list[str]]:
        """The VIP address ids of every on-prem load_balanced component of the
        run, by component id, port and context name, and the errors (a range,
        no serving context, ...) of the application's own components.

        The application's own components get their VIPs produced, then the
        R_server rules of every context they use, one context at a time. An
        edge target of another application only has its VIP addresses
        resolved, for this run's R_client rules: the allocation identifier
        yields the same address its own run puts on the VIP, whichever runs
        first. What is wrong with such a target is its own run's error, never
        its callers' (a misconfigured component would fail every one of them);
        a caller reports only the used port that has no VIP
        (application_security.py). A cloud component has no entry: its own
        run writes its CloudLoadBalancer (``security_profile``, the
        application's, decides the scheme), and nothing is resolved for a
        cloud edge target.
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
        """Re-read the segment prefixes and serving contexts a concurrent generator is still writing.

        The load that triggers this run also triggers add_vxlan_segment, which
        addresses a VXLAN segment (its gateway prefix holds the pool members),
        and add_customer_deployment_dc, which links a customer DC to its
        serving context before it allocates the context's VIP range and wires
        its leg. So this run can read either before it lands. Each one still
        missing it waits for its owner's run in flight, then all of a kind are
        re-read together (_reread_until_ready) and the result is written into
        every dict of them: a segment or deployment shared by two components
        is read once. Whatever is still missing after the last read is the
        component's error, as before.

        Only the application's own components need their segment prefix (the
        pool members); an edge target's contexts are re-read too, its VIPs
        are this run's R_client rules. A VLAN segment (an authored gateway, no
        generator writes it) and a cloud component (no prefix, no context) are
        never re-read.
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
                deployment = instance.get("deployment") or {}
                if (
                    deployment.get("id")
                    and deployment.get("typename") == TopologyCustomerDC.__name__
                    and not context_ready(deployment.get("serving_loadbalancer_context"))
                ):
                    deployments.setdefault(deployment["id"], []).append(deployment)
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
        """Wait for ``owner``'s run in flight for each node of ``dicts`` (by id), then re-read
        their ``field`` until ``ready``, into every dict of the node.

        One query per read for every node still not ready, at most
        LB_VIP_REREAD.attempts reads LB_VIP_REREAD.delay seconds apart; no-op without a node.
        """
        if not dicts:
            return
        node_ids = sorted(dicts)
        self.logger.info("Waiting for %s to write %s of %s", owner, field, ", ".join(node_ids))
        for node_id in node_ids:
            await self.wait_for_parent_generators((owner,), node_id)
        text = query.read_text()
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
        """One address per instance in the component's segment prefix; None (an error) without a prefix.

        Keyed by (instance, segment): two components on one host and segment
        share the host's address.
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
            member_id = await self.allocate_prefix_address(
                pool=pool,
                identifier=member_identifier(instance["id"], segment["id"]),
                prefix_length=prefix_length,
                description=f"Load balancer pool member {instance.get('name', instance['id'])}",
            )
            if member_id is not None:
                member_ids.append(member_id)
        return member_ids

    async def _cloud_component_vips(
        self, component: dict[str, Any], *, security_profile: str, errors: list[str]
    ) -> None:
        """Upsert the component's CloudLoadBalancer and its VIPs, one per port, tracked by this run.

        No load balancer without its one virtual network; a member that
        cannot be addressed is an error and left out.
        """
        ports, port_errors = vip_ports(component)
        network, segments, placement_errors = cloud_placement(component)
        errors.extend([*port_errors, *placement_errors])
        if network is None:
            return
        member_ids = await self._cloud_pool_members(component, network, errors)
        load_balancer = await self._save_cloud_lb(component, network, segments, security_profile, errors)
        if load_balancer is None:
            return
        for port in ports:
            await self._save_vip(component, load_balancer, port, member_ids)

    async def _cloud_pool_members(
        self, component: dict[str, Any], network: dict[str, Any], errors: list[str]
    ) -> list[str]:
        """The ids of the cloud instances' own addresses, each upserted untracked under its lock.

        The address is the instance's, not the application's: two components
        on one instance share it and no application run may delete it.
        """
        members, member_errors = cloud_members(component, network)
        errors.extend(member_errors)
        member_ids: list[str] = []
        for instance_name, address, namespace_id in members:
            try:
                async with self.resource_lock(f"ip-address-{namespace_id}-{address}"):
                    ip = await self.upsert_ip_address(
                        address=address,
                        ip_namespace={"id": namespace_id},
                        description=f"Private address of cloud instance {instance_name}",
                        track=False,
                    )
            except Exception as exc:
                errors.append(f"{component_ref(component)}: address {address} of {instance_name}: {exc}")
                continue
            member_ids.append(ip.id)
        return member_ids

    async def _save_cloud_lb(
        self,
        component: dict[str, Any],
        network: dict[str, Any],
        segments: list[dict[str, Any]],
        security_profile: str,
        errors: list[str],
    ) -> dict[str, str] | None:
        """Upsert the component's CloudLoadBalancer under its name's lock, tracked by this run.

        Returns its id and name, or None (an error) on failure.
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
                        "lb_type": CLOUD_LB_TYPE,
                        "scheme": cloud_lb_scheme(security_profile),
                        "virtual_network": {"id": network["id"]},
                        "network_segments": [segment["id"] for segment in segments],
                        "member_of_groups": [CLOUD_LB_GROUP],
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
    ) -> bool:
        """Upsert the LoadbalancerVIP of (component, port) on ``load_balancer``, tracked by this run;
        False (logged) on failure.

        ``load_balancer`` is a context or a CloudLoadBalancer (id and name); a
        cloud VIP has no ``vip_ip_id``, the cloud assigns its address.
        """
        protocol, port_start, _port_end = port
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
