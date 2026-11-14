"""Security rule generators derived from application dependencies."""

from __future__ import annotations

from typing import Any

from generators.helpers.lb_vip import is_cloud_component
from generators.helpers.routing_tag import ensure_segment_routing_tag
from generators.helpers.rules import RulesPlanner
from generators.helpers.security_tag import clear_component_tag, ensure_security_tag, ensure_segment_tag
from generators.mixins.common import CommonGenerator
from generators.mixins.connections import CablingMixin
from generators.mixins.lb_vip import LoadbalancerVipMixin
from generators.mixins.named_objects import GetOrCreateByNameMixin
from generators.mixins.pools import PoolMixin
from generators.mixins.segment_firewall import SegmentFirewallMixin
from generators.mixins.ztna import ZtnaMixin
from generators.protocols import AppComponent
from utils.data_cleaning import clean_data
from utils.group_policy import is_microsegmented
from utils.ports import PortProfileHelper

# The segment kind add_vxlan_segment addresses (generators/topology/segment.py).
VXLAN_SEGMENT_KIND = "ManagedVxlanSegment"


class AppApplicationGenerator(
    ZtnaMixin,
    SegmentFirewallMixin,
    LoadbalancerVipMixin,
    GetOrCreateByNameMixin,
    CablingMixin,
    PoolMixin,
    CommonGenerator,
):
    """Generate segment-scoped security rules from app dependencies (orchestration only; logic is in the mixins).
    Touched routed tenant segments get their group tag, microsegmented components their component tag."""

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
        for segment_id in pending:
            refreshed = await self.wait_for_parent_generator_and_refetch(("add_vxlan_segment",), segment_id)
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
        planner = RulesPlanner()

        app_name: str = app.get("name", "")
        app_security_profile: str = app.get("security_profile", "internal_standard")
        self.logger.info("Processing security rules for application: %s", app_name)

        components: list[dict] = app.get("children", [])
        if not components:
            self.logger.warning("Application %s has no components - nothing to do", app_name)
            return

        edges = self._dependency_edges_from_components(components)

        # Per component, even without edges: tags and isolation mode for every segment used and each edge target's (it
        # may belong to an application that has not run yet).
        segments = self._used_segments(components, edges)
        # A segment without an id is a component without one.
        for segment in segments:
            if segment.get("id"):
                await ensure_segment_tag(self.client, self.resource_lock, segment=segment)
                await ensure_segment_routing_tag(self.client, segment=segment)
        for component in components:
            await self._ensure_segment_isolation_mode(component.get("network_segment") or {}, app_security_profile)
        # An edge target on those segments is a second dict of it; the rules below read its mode
        modes = {
            segment["id"]: segment.get("isolation_mode")
            for component in components
            if (segment := component.get("network_segment") or {}).get("id")
        }
        for segment in segments:
            if segment.get("id") in modes:
                segment["isolation_mode"] = modes[segment["id"]]
        # After the isolation modes and before the rules, which carry the component tags
        await self._ensure_component_tags(components, edges)

        # After the tags (R_server carries the backend's) and before the rules (R_client reads the VIPs); errors fail
        # the run last
        rule_indexes: dict[str, set[int]] = {}
        lb_vips, lb_errors = await self._reconcile_load_balancing(
            components, edges, rule_indexes, security_profile=app_security_profile
        )

        # Access-profile grants hang off the target component, so publish them with or without edges
        rules_created, rules_skipped = await self._reconcile_private_access_components(app_name, components)

        if not edges:
            self.logger.info("Application %s has no depends_on edges - no rules to generate", app_name)
            self._fail_on_lb_errors(app_name, lb_errors)
            return

        self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)

        segment_policies: dict[str, Any] = {}
        proxy_policies: dict[str, str] = {}

        rules_failed = 0
        for src_comp, dep, dst_comp in edges:
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
                rules_skipped += 1
                continue

            if dep.get("target_fqdn"):
                if await self._reconcile_proxy_rule(
                    app_name=app_name,
                    src_comp=src_comp,
                    dep=dep,
                    proxy_policies=proxy_policies,
                ):
                    rules_created += 1
                else:
                    rules_skipped += 1
                continue

            try:
                ports = PortProfileHelper.resolve_dependency_ports(dep, dst_comp)
            except ValueError as exc:
                self.logger.warning("  Dependency '%s': %s - skipping rule creation", dep_ref, exc)
                rules_skipped += 1
                continue
            if not ports:
                self.logger.warning(
                    "  Dependency '%s' (%s -> %s) has no ports and its target lists none - skipping rule creation",
                    dep_ref,
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
                rules_skipped += 1
                continue

            src_seg = src_comp.get("network_segment") or {}
            dst_seg = dst_comp.get("network_segment") or {}
            # Strict load balancing: on-prem load_balanced targets are reached through VIPs only; a cloud one keeps the
            # direct rule
            strict = dst_comp.get("load_balanced") and not is_cloud_component(dst_comp)
            target_vips = lb_vips.get(dst_comp.get("id") or "", {}) if strict else None

            for port in ports:
                # One R_client per VIP (context) of the port; None is the direct rule.
                vips: list[tuple[str, str] | None] = [None]
                if target_vips is not None:
                    vips = list((target_vips.get(port) or {}).items())
                    if not vips:
                        lb_errors.append(
                            f"dependency '{dep_ref}': load-balanced target {dst_comp.get('fqdn', '?')} has no VIP "
                            f"on {PortProfileHelper.format_port_spec(port)}"
                        )
                        rules_skipped += 1
                        continue
                for vip in vips:
                    try:
                        created, skipped = await self._reconcile_segment_rule(
                            app_name=app_name,
                            app_security_profile=app_security_profile,
                            src_comp=src_comp,
                            dst_comp=dst_comp,
                            dep=dep,
                            src_seg=src_seg,
                            dst_seg=dst_seg,
                            planner=planner,
                            segment_policies=segment_policies,
                            rule_indexes=rule_indexes,
                            port=port,
                            vip=vip,
                        )
                    except Exception as exc:
                        rules_failed += 1
                        self.logger.error("  Dependency '%s' %s: rule failed: %s", dep_ref, port, exc)
                        continue
                    rules_created += int(created)
                    rules_skipped += int(skipped)

        self.logger.info(
            "Application %s: %d rule(s) created, %d already existed, %d failed",
            app_name,
            rules_created,
            rules_skipped,
            rules_failed,
        )
        self._fail_on_lb_errors(app_name, lb_errors)
        if rules_failed:
            raise RuntimeError(f"Application {app_name}: {rules_failed} rule(s) failed")

    def _fail_on_lb_errors(self, app_name: str, errors: list[str]) -> None:
        """Fail the run with every load balancing error at once; a failed run keeps its tracking group as is."""
        if errors:
            self.fail(f"Application {app_name}: load balancing: {'; '.join(errors)}")

    async def _ensure_component_tags(
        self,
        components: list[dict[str, Any]],
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> None:
        """Tag every component of a microsegmented segment and record the tag on the dicts. Own components get the tag
        (one with a tag elsewhere loses it), edge targets are tagged too, and other applications calling a newly tagged
        component are reconciled again.
        """
        microsegmented = {
            segment["id"]
            for component in components
            if (segment := component.get("network_segment") or {}).get("id") and is_microsegmented(segment)
        }
        targets = [dst for _src, _dep, dst in edges if dst.get("id")]
        own = {id(component) for component in components}
        tag_ids: dict[str, str] = {}
        callers: set[str] = set()
        for component in [*components, *targets]:
            component_id = component.get("id")
            if not component_id or component_id in tag_ids:
                continue
            if (component.get("network_segment") or {}).get("id") in microsegmented:
                untagged = not (component.get("security_tag") or {}).get("id")
                tag_ids[component_id] = await ensure_security_tag(
                    self.client, self.resource_lock, kind=AppComponent, node=component, name=component.get("fqdn")
                )
                if untagged and id(component) in own:
                    callers.update(self._calling_application_ids(component))
            elif id(component) in own and (component.get("security_tag") or {}).get("id"):
                await clear_component_tag(self.client, component_id=component_id)
                component["security_tag"] = None
                self.logger.info("  Component %s left its microsegmented segment - tag detached", component_id)
        for component in [*components, *targets]:
            if component.get("id") in tag_ids:
                component["security_tag"] = {"id": tag_ids[component["id"]]}
        # Rules from other applications carry the component's tag; a component tagged only now needs them to run again
        if callers:
            self.logger.info("  Re-reconciling %d calling application(s) of newly tagged components", len(callers))
            await self._fan_out(sorted(callers))

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
