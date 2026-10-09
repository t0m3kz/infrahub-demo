"""Security rule generators derived from application dependencies."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..cloud_security import CloudSecurityRuleMixin
from ..common import CommonGenerator
from ..helpers.rules import RulesPlanner
from ..named_objects import GetOrCreateByNameMixin
from ..segment_firewall import SegmentFirewallMixin
from ..ztna import ZtnaMixin


def _seg_cidr(seg: dict) -> str | None:
    """Extract a CIDR string from a segment dict (on-prem or cloud)."""
    return RulesPlanner.seg_cidr(seg)


class AppApplicationGenerator(
    CloudSecurityRuleMixin,
    ZtnaMixin,
    SegmentFirewallMixin,
    GetOrCreateByNameMixin,
    CommonGenerator,
):
    """Generate segment-scoped security rules from app dependencies.

    Orchestration only — domain logic lives in the mixins above, one per
    dependency-edge shape: CloudSecurityRuleMixin (either side is a
    CloudNetworkSegment), ZtnaMixin (target_fqdn egress and private-access
    publishing for access-profile grants),
    SegmentFirewallMixin (on-prem segment-to-segment SecurityPolicyRule, plus
    zone/tag handling).
    """

    async def run(self, identifier: str, data: dict[str, Any] | None = None) -> None:
        """Track generated policy objects independently for each application.

        Only an AppApplication payload saves anything (see generate()); the
        dependency/component entry points only fan out, so their groups stay
        empty and — the SDK skips the group update for a run with no members —
        never delete anything.
        """
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
        """Reconcile an application's rules, or fan out to the applications a
        dependency/component touches.

        Only the add_app_application definition writes: its tracking group is
        keyed by the application, so it is the one owner of that
        application's rules. add_app_dependency and add_app_component are the
        same class under another definition, whose group is keyed by the
        dependency/component instead — a rule saved there would be claimed by
        a group that no longer reconciles it once the dependency/component
        moves to another application, and that group's next run would delete
        the old application's rules. So those two entry points only trigger
        add_app_application for the affected application(s) and save nothing.
        """
        cleaned = clean_data(data)

        if deps := cleaned.get("AppDependency"):
            await self._fan_out(self._dependency_trigger_app_ids(deps[0]))
            return

        if components := cleaned.get("AppComponent"):
            # Another application's rule into this component names its
            # segment and ports, and only that application's own run rewrites
            # it. add_app_application does not fan out again, so two
            # applications calling each other cannot loop.
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
        await self._reconcile_application_rules(app_list[0])

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
            # An access-profile grant: the application is the target's,
            # and publishing reads the grant from the component itself.
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
        # The trigger fires once the dependency is committed, so the
        # application query already reads it through depends_on. The
        # app_dependency payload only carries enough to find the
        # application, too little to build a rule from.
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

        # Runs unconditionally per component (not gated on having a
        # dependency edge below) so every segment this app uses gets
        # classified, the same unconditional guarantee VxlanSegmentGenerator
        # gives security_zone.
        for component in components:
            await self._ensure_segment_isolation_mode(component.get("network_segment") or {}, app_security_profile)

        edges = self._dependency_edges_from_components(components)

        # Access-profile grants hang off the target component, not a source
        # component, so they are published whether or not the app has edges.
        rules_created, rules_skipped = await self._reconcile_private_access_components(app_name, components)

        if not edges:
            self.logger.info("Application %s has no depends_on edges - no rules to generate", app_name)
            return

        self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)

        segment_policies: dict[str, Any] = {}
        rule_indexes: dict[str, set[int]] = {}
        proxy_policies: dict[str, Any] = {}

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
                ports = planner.resolve_ports(dep, dst_comp)
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

            if planner.is_cloud_dependency(src_seg, dst_seg):
                # At least one side is a CloudNetworkSegment: route to the
                # cloud-security-group path instead of the on-prem
                # SecurityPolicy/SecurityPolicyRule one below. Previously
                # nothing dispatched here at all, so an on-prem<->cloud
                # dependency silently got an on-prem-shaped rule referencing
                # a segment id that rule couldn't actually enforce against.
                for port in ports:
                    if await self._create_cloud_rule(
                        app_name=app_name,
                        src_comp=src_comp,
                        dst_comp=dst_comp,
                        dep=dep,
                        rule_name=planner.rule_name(app_name, src_comp, dst_comp, dep, port),
                        port=port,
                    ):
                        rules_created += 1
                    else:
                        rules_skipped += 1
                continue

            for port in ports:
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
                )
                rules_created += int(created)
                rules_skipped += int(skipped)

        self.logger.info(
            "Application %s: %d rule(s) created, %d already existed",
            app_name,
            rules_created,
            rules_skipped,
        )

    @staticmethod
    def _calling_application_ids(component: dict[str, Any]) -> list[str]:
        """Ids of the other applications with a component calling this one.
        Access-profile grants have no calling application; the component's
        own application is reconciled already."""
        own_app_id = (component.get("parent") or {}).get("id")
        callers: set[str] = set()
        for dep in component.get("dependents") or []:
            caller_app_id = (((dep.get("source") or {}).get("parent")) or {}).get("id")
            if caller_app_id and caller_app_id != own_app_id:
                callers.add(str(caller_app_id))
        return sorted(callers)

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
