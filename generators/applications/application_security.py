"""Security rules derived from application dependencies, written as one plan per application: the group tags of the
segments and components it touches, the isolation modes, the VIPs of its load balanced components, the segment and
load balancer SecurityPolicyRules and the ZTNA proxy rules."""

from __future__ import annotations

from typing import Any

from generators.applications.lb_vip import LoadBalancing, await_lb_inputs, plan_load_balancing
from generators.applications.policy_rules import (
    RuleRequest,
    plan_policy_rules,
    read_policy_facts,
    segment_policy_target,
)
from generators.applications.proxy_rules import (
    ProxyRuleRequest,
    egress_request,
    plan_proxy_rules,
    private_access_requests,
    proxy_lock_keys,
    read_proxy_facts,
)
from generators.base import BaseGenerator
from generators.helpers.common import gather_bounded
from generators.helpers.lb_vip import is_cloud_component
from generators.helpers.routing_tag import segment_routing_tag_spec
from generators.helpers.rules import RulesPlanner
from generators.helpers.security_tag import (
    component_target,
    plan_clear_component_tag,
    plan_security_tags,
    read_tag_pool,
    segment_target,
    tag_lock_keys,
)
from generators.plan import NodeRef, NodeSpec, TopologyPlan
from generators.protocols import AppComponent
from utils.data_cleaning import clean_data
from utils.group_policy import is_microsegmented
from utils.ports import PortProfileHelper, PortSpec

Edge = tuple[dict[str, Any], dict[str, Any], dict[str, Any]]

# The segment kind add_vxlan_segment addresses (generators/segments/segment.py).
VXLAN_SEGMENT_KIND = "ManagedVxlanSegment"


class AppApplicationGenerator(BaseGenerator):
    """Generate segment-scoped security rules from app dependencies (orchestration; the planners are in
    generators/applications/). Touched routed tenant segments get their group tag, microsegmented components their
    component tag."""

    async def run(self, identifier: str, data: dict[str, Any] | None = None) -> None:
        """Track generated policy objects per application; only an AppApplication payload saves anything."""
        if not data:
            data = await self.collect_data()
        unpacked = data.get("data") or data
        await self.process_nodes(data=unpacked)

        params = dict(self.params)
        cleaned = clean_data(unpacked)
        applications = cleaned.get("AppApplication") or []
        if applications and applications[0].get("name"):
            params["name"] = str(applications[0]["name"])

        group_type = "CoreGeneratorGroup" if self.execute_after_merge else "CoreGeneratorAwareGroup"
        async with self._init_client.start_tracking(
            identifier=identifier,
            params=params,
            delete_unused_nodes=True,
            group_type=group_type,
        ) as self.client:
            await self.generate(data=unpacked)

    async def generate(self, data: dict[str, Any]) -> None:
        """Reconcile an application's rules, or fan out to the applications a dependency/component touches.
        Only add_app_application writes; add_app_dependency and add_app_component trigger it and save nothing."""
        cleaned = clean_data(data)

        if deps := cleaned.get("AppDependency"):
            await self._fan_out(self._dependency_trigger_app_ids(deps[0]))
            return

        if components := cleaned.get("AppComponent"):
            # Another application's rule names this segment and ports; only its own run rewrites it, and no fan-out
            # loop.
            component = components[0]
            own_app_id = self._component_trigger_app_id(component)
            callers = self._calling_application_ids(component)
            if callers:
                self.logger.info("Re-reconciling %d calling application(s) of %s", len(callers), own_app_id)
            await self._fan_out(sorted({*([own_app_id] if own_app_id else []), *callers}))
            return

        app_list = cleaned.get("AppApplication", [])
        if not app_list:
            self.logger.error("No AppApplication/AppDependency/AppComponent data in GraphQL response")
            return
        await self._reconcile_application_rules(await self._await_segment_addressing(app_list[0]))

    async def _await_segment_addressing(self, app: dict[str, Any]) -> dict[str, Any]:
        """The application, re-read after add_vxlan_segment addressed the gateway-less segments it uses (waits for
        in-flight runs, never on this one).
        """
        components: list[dict[str, Any]] = app.get("children") or []
        pending = sorted(
            {
                str(segment["id"])
                for segment in self._used_segments(components, self._dependency_edges_from_components(components))
                if segment.get("id") and segment.get("typename") == VXLAN_SEGMENT_KIND and not segment.get("gateway")
            }
        )
        # The waits are independent; the data is re-read once, after all of them
        waited = await gather_bounded(self.wait_for_parent_generators(("add_vxlan_segment",), sid) for sid in pending)
        if any(waited):
            refreshed = await self.collect_data()
            if refreshed is not None:
                app = (clean_data(refreshed).get("AppApplication") or [app])[0]
        return app

    async def _fan_out(self, app_ids: list[str]) -> None:
        """Trigger add_app_application for ``app_ids`` without waiting; no-op when empty."""
        if app_ids:
            await self.run_generator("add_app_application", app_ids, wait=False)

    def _dependency_trigger_app_ids(self, dep: dict[str, Any]) -> list[str]:
        """The application(s) an app_dependency trigger reconciles — [] (logged) to skip."""
        src_comp = dep.get("source") or {}
        dst_comp = dep.get("target") or {}
        dep_ref = dep.get("name", dep.get("id", "?"))
        if not dst_comp and not dep.get("target_fqdn"):
            self.logger.warning("Dependency has neither a target component nor a target_fqdn - skipping")
            return []
        if not src_comp:
            if not dep.get("source_profile"):
                self.logger.warning("Dependency has neither a source component nor a source_profile - skipping")
                return []
            # An access-profile grant: the application is the target's
            target_app_id = (dst_comp.get("parent") or {}).get("id", "")
            if not target_app_id:
                self.logger.warning("Dependency target has no parent application - skipping")
                return []
            self.logger.info("Access grant '%s' -> add_app_application for %s", dep_ref, target_app_id)
            return [str(target_app_id)]

        app_id = (src_comp.get("parent") or {}).get("id", "")
        if not app_id:
            self.logger.warning("Dependency source has no parent application - skipping")
            return []
        # The trigger fires after commit, so the query reads the dependency; the payload only finds the application.
        self.logger.info("Dependency trigger '%s' -> add_app_application for %s", dep_ref, app_id)
        return [str(app_id)]

    def _component_trigger_app_id(self, component: dict[str, Any]) -> str | None:
        """The component's own application id, or None (logged) to skip it."""
        component_ref = component.get("fqdn", component.get("id", "?"))
        app_id = (component.get("parent") or {}).get("id", "")
        if not app_id:
            self.logger.warning("Component %s has no parent application - skipping", component_ref)
            return None
        self.logger.info("Component trigger '%s' -> add_app_application for %s", component_ref, app_id)
        return str(app_id)

    async def _reconcile_application_rules(self, app: dict[str, Any]) -> None:
        """Plan the application's tags, isolation modes, VIPs and rules, then write the plan once, under the locks of
        what it decides from. Whatever cannot be written is reported before the first write.
        """
        planner = RulesPlanner()

        app_name: str = app.get("name", "")
        app_security_profile: str = app.get("security_profile", "internal_standard")
        self.logger.info("Processing security rules for application: %s", app_name)

        components: list[dict] = app.get("children", [])
        if not components:
            self.logger.warning("Application %s has no components - nothing to do", app_name)
            return

        edges = self._dependency_edges_from_components(components)
        segments = self._used_segments(components, edges)
        plan = TopologyPlan(root_id=None)
        locks: list[str] = []

        # Per component, even without edges: tags and isolation mode for every segment used and each edge target's (it
        # may belong to an application that has not run yet).
        self._plan_routing_tags(plan, segments)
        for component in components:
            self._plan_isolation_mode(plan, component.get("network_segment") or {}, app_security_profile)
        # An edge target on those segments is a second dict of it; the rules below read its mode
        modes = {
            segment["id"]: segment.get("isolation_mode")
            for component in components
            if (segment := component.get("network_segment") or {}).get("id")
        }
        for segment in segments:
            if segment.get("id") in modes:
                segment["isolation_mode"] = modes[segment["id"]]
        # After the isolation modes (microsegmentation is decided from them) and before the rules, which carry the tags
        callers = await self._plan_tags(plan, segments, components, edges, locks)

        # Before the rules (R_client reads the VIPs); its errors fail the run
        await await_lb_inputs(self, components, edges)
        balancing = plan_load_balancing(plan, components, edges, security_profile=app_security_profile)
        locks.extend(balancing.locks)
        requests = list(balancing.requests)

        # Access-profile grants hang off the target component, so publish them with or without edges
        proxy_requests, rules_skipped = private_access_requests(self.logger, app_name, components)
        if edges:
            self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)
        else:
            self.logger.info("Application %s has no depends_on edges - no rules to generate", app_name)
        for src_comp, dep, dst_comp in edges:
            skipped = self._plan_dependency(
                app_name,
                app_security_profile,
                planner,
                (src_comp, dep, dst_comp),
                balancing,
                requests,
                proxy_requests,
            )
            rules_skipped += skipped
        if balancing.errors:
            self.fail(f"Application {app_name}: load balancing: {'; '.join(balancing.errors)}")

        locks.extend(request.policy.lock for request in requests)
        locks.extend(proxy_lock_keys(proxy_requests))
        async with self.plan_locks(*locks):
            facts = await read_policy_facts(
                self.client,
                [request.policy for request in requests],
                sorted({name for request in requests if (name := request.profile_name)}),
            )
            try:
                created, existing = plan_policy_rules(plan, facts, requests)
            except ValueError as exc:
                self.fail(f"Application {app_name}: {exc}")
            proxy_planned, proxy_skipped = plan_proxy_rules(
                plan, self.logger, proxy_requests, await read_proxy_facts(self.client, proxy_requests)
            )
            await self.apply_plan(plan)
        self.logger.info(
            "Application %s: %d rule(s) created, %d already existed, %d proxy rule(s), %d skipped",
            app_name,
            created,
            existing,
            proxy_planned,
            rules_skipped + proxy_skipped,
        )
        # Rules from other applications carry the component's tag; a component tagged only now needs them to run again
        if callers:
            self.logger.info("  Re-reconciling %d calling application(s) of newly tagged components", len(callers))
            await self._fan_out(sorted(callers))

    def _plan_dependency(
        self,
        app_name: str,
        app_security_profile: str,
        planner: RulesPlanner,
        edge: Edge,
        balancing: LoadBalancing,
        requests: list[RuleRequest],
        proxy_requests: list[ProxyRuleRequest],
    ) -> int:
        """The rule requests of one dependency edge, one per port (and per VIP of a load-balanced target); returns how
        many were skipped.
        """
        src_comp, dep, dst_comp = edge
        dep_ref = dep.get("name", dep.get("id", "?"))
        authorized, auth_reason = planner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)
        if not authorized:
            self.logger.warning(
                "  Dependency '%s' (%s -> %s) is not authorized: %s",
                dep_ref,
                src_comp.get("name", "?"),
                dst_comp.get("name") or dep.get("target_fqdn") or "?",
                auth_reason or "missing approval",
            )
            return 1

        if dep.get("target_fqdn"):
            request = egress_request(self.logger, app_name, src_comp, dep)
            if request is None:
                return 1
            proxy_requests.append(request)
            return 0

        try:
            ports = PortProfileHelper.resolve_dependency_ports(dep, dst_comp)
        except ValueError as exc:
            self.logger.warning("  Dependency '%s': %s - skipping rule creation", dep_ref, exc)
            return 1
        if not ports:
            self.logger.warning(
                "  Dependency '%s' (%s -> %s) has no ports and its target lists none - skipping rule creation",
                dep_ref,
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return 1

        # Strict load balancing: on-prem load_balanced targets are reached through VIPs only; a cloud one keeps the
        # direct rule
        strict = dst_comp.get("load_balanced") and not is_cloud_component(dst_comp)
        target_vips = balancing.vips.get(dst_comp.get("id") or "", {}) if strict else None
        skipped = 0
        for port in ports:
            # One R_client per VIP (context) of the port; None is the direct rule.
            vips: list[tuple[str, NodeRef] | None] = [None]
            if target_vips is not None:
                vips = list((target_vips.get(port) or {}).items())
                if not vips:
                    balancing.errors.append(
                        f"dependency '{dep_ref}': load-balanced target {dst_comp.get('fqdn', '?')} has no VIP "
                        f"on {PortProfileHelper.format_port_spec(port)}"
                    )
                    skipped += 1
                    continue
            for vip in vips:
                request = self._segment_rule_request(
                    app_name, app_security_profile, planner, (src_comp, dep, dst_comp), port, vip
                )
                if request is None:
                    skipped += 1
                else:
                    requests.append(request)
        return skipped

    def _segment_rule_request(
        self,
        app_name: str,
        app_security_profile: str,
        planner: RulesPlanner,
        edge: Edge,
        port: PortSpec,
        vip: tuple[str, NodeRef] | None,
    ) -> RuleRequest | None:
        """Segment-to-segment dependency rule for one edge and port, or None (warned) without both segments; ``vip``
        makes it R_client.
        """
        src_comp, dep, dst_comp = edge
        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        src_seg_id = src_seg.get("id")
        if not src_seg_id or not dst_seg.get("id") or not str(src_seg.get("name") or src_seg_id):
            self.logger.warning(
                "Dependency %s -> %s: one or both components lack a network_segment - skipping",
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return None

        rule_name = planner.rule_name(app_name, src_comp, dst_comp, dep, port)
        if vip:
            # Own name per VIP, so a target turning load-balanced gets new rules and tracking deletes the old
            rule_name = f"{rule_name}-vip-{planner.name_part(vip[0])}"
        protocol, port_start, port_end = port

        src_zone, dst_zone, cross_zone = planner.zone_context(src_seg=src_seg, dst_seg=dst_seg)
        if src_zone is None or dst_zone is None:
            cross_zone = True
            self.logger.warning(
                "  Dependency '%s' has incomplete zone mapping (%s -> %s); treating as cross-zone",
                dep.get("name", dep.get("id", "?")),
                src_zone or "<missing>",
                dst_zone or "<missing>",
            )
        payload = planner.build_rule_payload(
            rule_name=rule_name,
            dep=dep,
            src_comp=src_comp,
            dst_comp=dst_comp,
            src_seg=src_seg,
            dst_seg=dst_seg,
            protocol=protocol,
            port_start=port_start,
            port_end=port_end,
            cross_zone=cross_zone,
            vip_id=vip[1] if vip else None,
        )
        return RuleRequest(
            segment_policy_target(src_seg),
            rule_name,
            payload,
            planner.pick_profile_name(app_security_profile, cross_zone),
        )

    def _plan_routing_tags(self, plan: TopologyPlan, segments: list[dict[str, Any]]) -> None:
        """The routing tag of every routed tenant segment that has none, once per segment."""
        drawn: set[str] = set()
        for segment in segments:
            # A segment without an id is a component without one
            if segment.get("id") and segment["id"] not in drawn and (spec := segment_routing_tag_spec(segment)):
                drawn.add(segment["id"])
                plan.add(spec)

    def _plan_isolation_mode(self, plan: TopologyPlan, segment: dict[str, Any], app_security_profile: str) -> None:
        """Derive a segment's isolation_mode from its application's security_profile; an author-set non-default is kept,
        cloud segments skipped. The mode is recorded in the dict so this run's tags and rules use it.
        """
        seg_id = segment.get("id")
        seg_typename = segment.get("typename")
        if not seg_id or not seg_typename or seg_typename == "CloudNetworkSegment":
            return

        current = segment.get("isolation_mode")
        derived = RulesPlanner.pick_isolation_mode(app_security_profile)
        if (current and current != "normal") or current == derived:
            return
        key = f"isolation-{seg_id}"
        if not any(isinstance(spec, NodeSpec) and spec.key == key for spec in plan.specs):
            plan.add(NodeSpec(key, seg_typename, {"id": seg_id, "isolation_mode": derived}, ownership="shared"))
            self.logger.info("  Derived isolation_mode '%s' for segment %s", derived, segment.get("name", seg_id))
        segment["isolation_mode"] = derived

    async def _plan_tags(
        self,
        plan: TopologyPlan,
        segments: list[dict[str, Any]],
        components: list[dict[str, Any]],
        edges: list[Edge],
        locks: list[str],
    ) -> set[str]:
        """Tag every routed tenant segment, and every component of a microsegmented segment (own components, and edge
        targets), recording the tag on the dicts for the rules. An own component with a tag elsewhere loses it. The tags
        are written under ``locks`` (added to); returns the other applications calling a component tagged only now,
        which are reconciled again after the write.
        """
        microsegmented = {
            segment["id"]
            for component in components
            if (segment := component.get("network_segment") or {}).get("id") and is_microsegmented(segment)
        }
        own = {id(component) for component in components}
        targets = [target for segment in segments if segment.get("id") and (target := segment_target(segment))]
        seen: set[str] = set()
        callers: set[str] = set()
        for component in [*components, *(dst for _src, _dep, dst in edges if dst.get("id"))]:
            component_id = component.get("id")
            if not component_id or component_id in seen:
                continue
            seen.add(component_id)
            if (component.get("network_segment") or {}).get("id") in microsegmented:
                if not (component.get("security_tag") or {}).get("id") and id(component) in own:
                    callers.update(self._calling_application_ids(component))
                targets.append(component_target(component))
            elif id(component) in own and (component.get("security_tag") or {}).get("id"):
                plan_clear_component_tag(plan, component_id)
                component["security_tag"] = None
                self.logger.info("  Component %s left its microsegmented segment - tag detached", component_id)
        if any(not (target.node.get("security_tag") or {}).get("id") for target in targets):
            locks.extend(
                tag_lock_keys(
                    target.name for target in targets if not (target.node.get("security_tag") or {}).get("id")
                )
            )
            plan_security_tags(plan, targets, await read_tag_pool(self.client))
        # A second dict of a component (an edge target of the application) carries the tag as well
        tagged = {
            target.node["id"]: target.node["security_tag"]
            for target in targets
            if target.kind is AppComponent and target.node.get("security_tag")
        }
        for component in [*components, *(dst for _src, _dep, dst in edges)]:
            if component.get("id") in tagged:
                component["security_tag"] = tagged[component["id"]]
        return callers

    @staticmethod
    def _calling_application_ids(component: dict[str, Any]) -> list[str]:
        """Ids of the other applications with a component calling this one."""
        own_app_id = (component.get("parent") or {}).get("id")
        callers: set[str] = set()
        for dep in component.get("dependents") or []:
            caller_app_id = (((dep.get("source") or {}).get("parent")) or {}).get("id")
            if caller_app_id and caller_app_id != own_app_id:
                callers.add(str(caller_app_id))
        return sorted(callers)

    @staticmethod
    def _used_segments(
        components: list[dict[str, Any]],
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """The segment dict of every component, then of every edge target ({} where there is none)."""
        return [component.get("network_segment") or {} for component in components] + [
            dst.get("network_segment") or {} for _src, _dep, dst in edges
        ]

    @staticmethod
    def _dependency_edges_from_components(
        components: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        """Build dependency edges from the source component's reverse relation."""
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for component in components:
            for dependency in component.get("depends_on") or []:
                target = dependency.get("target") or {}
                if target or dependency.get("target_fqdn"):
                    edges.append((component, dependency, target))
        return edges
