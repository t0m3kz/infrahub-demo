"""Security rule generators derived from application dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.data_cleaning import clean_data

from ..cloud_security import CloudSecurityRuleMixin
from ..common import CommonGenerator
from ..helpers.rules import RulesPlanner
from ..named_objects import GetOrCreateByNameMixin
from ..rules import RuleLifecycleMixin
from ..segment_firewall import SegmentFirewallMixin
from ..service_ports import ServicePortMixin
from ..ztna import ZtnaMixin

_APPLICATION_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/application.gql"


def _seg_cidr(seg: dict) -> str | None:
    """Extract a CIDR string from a segment dict (on-prem or cloud)."""
    return RulesPlanner.seg_cidr(seg)


def _resolve_port(dep: dict) -> tuple[str, int | None, int | None] | None:
    """Return (protocol, port_start, port_end) from an AppDependency node."""
    return RulesPlanner.resolve_port(dep)


class AppApplicationGenerator(
    CloudSecurityRuleMixin,
    ZtnaMixin,
    SegmentFirewallMixin,
    ServicePortMixin,
    GetOrCreateByNameMixin,
    RuleLifecycleMixin,
    CommonGenerator,
):
    """Generate segment-scoped security rules from app dependencies.

    Orchestration only — domain logic lives in the mixins above, one per
    dependency-edge shape: CloudSecurityRuleMixin (either side is a
    CloudNetworkSegment), ZtnaMixin (external_service egress and
    private_access publish/access-profile derivation), SegmentFirewallMixin
    (on-prem segment-to-segment SecurityPolicyRule, plus zone/tag/return-rule
    handling), ServicePortMixin (AppServicePort sync ahead of rule dispatch).
    """

    async def run(self, identifier: str, data: dict[str, Any] | None = None) -> None:
        """Track generated policy objects independently for each application."""
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
        cleaned = clean_data(data)

        deps = cleaned.get("AppDependency", [])
        if deps:
            dep = deps[0]
            src_comp = dep.get("source") or {}
            dst_endpoint = dep.get("target") or {}
            if not src_comp:
                self.logger.warning("Dependency missing source component - skipping")
                return
            if not dst_endpoint:
                self.logger.warning("Dependency missing target endpoint - skipping")
                return

            app = src_comp.get("parent") or {}
            app_name = app.get("name", "")
            if not app_name:
                self.logger.warning("Dependency source has no parent application name - skipping")
                return

            self.logger.info(
                "Dependency trigger '%s' -> full application rule reconciliation for %s",
                dep.get("name", dep.get("id", "?")),
                app_name,
            )
            await self._run_for_application_name(
                app_name,
                forced_edges=[(src_comp, dep, dst_endpoint)],
            )
            return

        components = cleaned.get("AppComponent", [])
        if components:
            component = components[0]
            app = component.get("parent") or {}
            app_name = app.get("name", "")
            if not app_name:
                self.logger.warning(
                    "Component %s has no parent application name - skipping",
                    component.get("slug", component.get("id", "?")),
                )
                return

            self.logger.info(
                "Component trigger '%s' -> full application rule reconciliation for %s",
                component.get("slug", component.get("id", "?")),
                app_name,
            )
            payload_deps = cleaned.get("AppDependency", [])
            forced_edges = self._dependency_edges_from_payload(payload_deps, app_name)
            if forced_edges:
                self.logger.info("Using %d dependency edge(s) from application_component payload", len(forced_edges))

            await self._reconcile_application_rules(
                app,
                forced_edges=forced_edges,
            )
            return

        app_list = cleaned.get("AppApplication", [])
        if not app_list:
            self.logger.error("No AppApplication/AppDependency/AppComponent data in GraphQL response")
            return

        app = app_list[0]
        app_name = str(app.get("name") or "")
        payload_deps = cleaned.get("AppDependency", [])
        forced_edges = self._dependency_edges_from_payload(payload_deps, app_name) if app_name else []
        if forced_edges:
            self.logger.info("Using %d dependency edge(s) from application payload", len(forced_edges))

        await self._reconcile_application_rules(
            app,
            forced_edges=forced_edges,
        )

    async def _run_for_application_name(
        self,
        app_name: str,
        forced_edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] | None = None,
    ) -> None:
        if not app_name:
            self.logger.warning("Cannot run application rule reconciliation without application name")
            return

        try:
            result = await self.client.execute_graphql(
                query=_APPLICATION_QUERY_PATH.read_text(),
                variables={"name": app_name},
            )
        except Exception as exc:
            self.logger.error("Failed to fetch application payload for '%s': %s", app_name, exc)
            return

        cleaned = clean_data(result)
        app_list = cleaned.get("AppApplication", [])
        if not app_list:
            self.logger.warning("Application '%s' not found for rule reconciliation", app_name)
            return

        payload_deps = cleaned.get("AppDependency", [])
        payload_edges = self._dependency_edges_from_payload(payload_deps, app_name)

        merged_edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = payload_edges
        if forced_edges:
            merged_edges = payload_edges + forced_edges

        await self._reconcile_application_rules(app_list[0], forced_edges=merged_edges)

    async def _reconcile_application_rules(
        self,
        app: dict[str, Any],
        forced_edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] | None = None,
    ) -> None:
        planner = RulesPlanner()

        app_name: str = app.get("name", "")
        app_security_profile: str = app.get("security_profile", "internal_standard")
        self.logger.info("Processing security rules for application: %s", app_name)

        components: list[dict] = app.get("children", [])
        if not components:
            self.logger.warning("Application %s has no components - nothing to do", app_name)
            return

        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        warnings: list[str] = []
        if forced_edges:
            all_edges = edges + forced_edges
            deduped: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = {}
            for src_comp, dep, dst_comp in all_edges:
                key = str(dep.get("id") or dep.get("name") or f"{src_comp.get('id')}->{dst_comp.get('id')}")
                deduped[key] = (src_comp, dep, dst_comp)
            edges = list(deduped.values())
            self.logger.info("Applied %d dependency edge(s) from trigger context", len(forced_edges))
        else:
            edges = self._dependency_edges_from_components(components)
        for warning in warnings:
            self.logger.warning(warning)

        if not edges:
            self.logger.info("Application %s has no depends_on edges - no rules to generate", app_name)
            return

        self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)

        await self._reconcile_component_service_ports(components, edges)

        rules_created = 0
        rules_skipped = 0
        segment_policies: dict[str, Any] = {}
        proxy_policies: dict[str, Any] = {}

        pa_created, pa_skipped = await self._reconcile_private_access_endpoints(
            app_name, components, app_security_profile
        )
        rules_created += pa_created
        rules_skipped += pa_skipped

        for src_comp, dep, dst_endpoint in edges:
            dst_comp = dst_endpoint.get("parent") or {}
            if not dst_comp:
                self.logger.warning(
                    "Dependency '%s' target endpoint has no parent component - skipping",
                    dep.get("name", dep.get("id", "?")),
                )
                rules_skipped += 1
                continue
            authorized, auth_reason = planner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)
            if not authorized:
                self.logger.warning(
                    "  Dependency '%s' (%s -> %s) is not authorized: %s",
                    dep.get("name", dep.get("id", "?")),
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                    auth_reason or "missing approval",
                )
                rules_skipped += 1
                continue

            if dst_endpoint.get("endpoint_type") == "external_service":
                if await self._reconcile_proxy_rule(
                    app_name=app_name,
                    src_comp=src_comp,
                    dep=dep,
                    dst_endpoint=dst_endpoint,
                    proxy_policies=proxy_policies,
                ):
                    rules_created += 1
                else:
                    rules_skipped += 1
                continue

            if dst_endpoint.get("endpoint_type") == "private_access":
                # Published separately above via _reconcile_private_access_endpoints:
                # a private_access endpoint is reachable by anyone its own
                # access_profile (MFA/device posture/allowed_groups) admits, not by
                # a specific component "depending on" it, so this isn't dependency-
                # edge-driven the way external_service/internal_service are. An
                # edge that happens to target one anyway (e.g. an explicit intra-app
                # call) still gets its port linked above by
                # _reconcile_component_service_ports; it just contributes no
                # separate firewall/proxy rule of its own here.
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
                rule_name = planner.rule_name(app_name, src_comp, dst_comp)
                if await self._create_cloud_rule(
                    app_name=app_name,
                    src_comp=src_comp,
                    dst_comp=dst_comp,
                    dep=dep,
                    rule_name=rule_name,
                ):
                    rules_created += 1
                else:
                    rules_skipped += 1
                continue

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
            )
            rules_created += int(created)
            rules_skipped += int(skipped)

        attached_seg_ids: set[str] = set()
        for src_comp, _dep, dst_endpoint in edges:
            for comp in (src_comp, dst_endpoint.get("parent") or {}):
                seg = comp.get("network_segment") or {}
                seg_id = seg.get("id")
                if not seg_id or seg_id not in segment_policies or seg_id in attached_seg_ids:
                    continue
                await self._attach_policy_to_source_segment(segment=seg, policy_id=segment_policies[seg_id].id)
                attached_seg_ids.add(seg_id)

        self.logger.info(
            "Application %s: %d rule(s) created, %d already existed",
            app_name,
            rules_created,
            rules_skipped,
        )

    @staticmethod
    def _dependency_edges_from_payload(
        deps: list[dict[str, Any]],
        app_name: str,
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for dep in deps:
            src_comp = dep.get("source") or {}
            dst_endpoint = dep.get("target") or {}
            if not src_comp or not dst_endpoint:
                continue
            src_app_name = str((src_comp.get("parent") or {}).get("name") or "")
            if src_app_name != app_name:
                continue
            edges.append((src_comp, dep, dst_endpoint))
        return edges

    @staticmethod
    def _dependency_edges_from_components(
        components: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
        """Build dependency edges from the source component's reverse relation."""
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]] = []
        for component in components:
            for dependency in component.get("depends_on") or []:
                target = dependency.get("target") or {}
                if target:
                    edges.append((component, dependency, target))
        return edges
