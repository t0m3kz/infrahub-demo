"""Security rule generators derived from application dependencies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.ports import PortsPlanner
from ..helpers.rules import RulesPlanner
from ..protocols import AppComponent as AppComponentKind
from ..protocols import (
    AppServicePort,
    CloudSecurityGroup,
    CloudSecurityGroupRule,
    SecurityPolicy,
    SecurityPolicyRule,
    SecuritySecurityProfile,
    SecurityTagRule,
    SecurityZone,
)
from ..rules import RuleLifecycleMixin

# Starting rule index - leaves room below 100 for manually-crafted high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10
RULE_SAVE_ATTEMPTS = 5
RULE_DEFAULT_VALIDITY_DAYS = 180
_APPLICATION_QUERY_PATH = Path(__file__).resolve().parents[2] / "queries/topology/add/application.gql"


def _seg_cidr(seg: dict) -> str | None:
    """Extract a CIDR string from a segment dict (on-prem or cloud)."""
    return RulesPlanner.seg_cidr(seg)


def _resolve_port(dep: dict) -> tuple[str, int | None, int | None] | None:
    """Return (protocol, port_start, port_end) from an AppDependency node."""
    return RulesPlanner.resolve_port(dep)


class AppApplicationGenerator(RuleLifecycleMixin, CommonGenerator):
    """Generate segment-scoped security rules from app dependencies."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        deps = cleaned.get("AppDependency", [])
        if deps:
            dep = deps[0]
            src_comp = dep.get("source") or {}
            dst_comp = dep.get("target") or {}
            if not src_comp:
                self.logger.warning("Dependency missing source component - skipping")
                return
            if not dst_comp:
                self.logger.warning("Dependency missing target component - skipping")
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
                forced_edges=[(src_comp, dep, dst_comp)],
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

        for src_comp, dep, dst_comp in edges:
            src_seg = src_comp.get("network_segment") or {}
            dst_seg = dst_comp.get("network_segment") or {}

            src_seg_id = src_seg.get("id")
            dst_seg_id = dst_seg.get("id")
            src_seg_name = str(src_seg.get("name") or src_seg_id or "")

            if not src_seg_id or not dst_seg_id or not src_seg_name:
                self.logger.warning(
                    "Dependency %s -> %s: one or both components lack a network_segment - skipping",
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
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

            policy = segment_policies.get(src_seg_id)
            if policy is None:
                policy_name = planner.segment_policy_name(src_seg)
                policy = await self._get_or_create_policy(policy_name, src_seg_name)
                if policy is None:
                    rules_skipped += 1
                    continue
                segment_policies[src_seg_id] = policy

            policy_id = policy.id
            rule_name = planner.rule_name(app_name, src_comp, dst_comp)

            port_info = _resolve_port(dep)
            if port_info is None:
                self.logger.warning(
                    "  Dependency '%s' (%s -> %s) has no protocol/port - skipping rule creation",
                    dep.get("name", dep.get("id", "?")),
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
                rules_skipped += 1
                continue
            protocol, port_start, port_end = port_info

            src_zone, dst_zone, cross_zone = planner.zone_context(src_seg=src_seg, dst_seg=dst_seg, dep=dep)
            if src_zone is None or dst_zone is None:
                cross_zone = True
                self.logger.warning(
                    "  Dependency '%s' has incomplete zone mapping (%s -> %s); treating as cross-zone",
                    dep.get("name", dep.get("id", "?")),
                    src_zone or "<missing>",
                    dst_zone or "<missing>",
                )

            existing_rule = await self._find_existing_policy_rule(policy_id=policy_id, rule_name=rule_name)
            if existing_rule is not None:
                self.logger.info("  Rule '%s' already exists - registering with tracker", rule_name)
                await existing_rule.save(allow_upsert=True)
                await self._reconcile_tag_rule_from_segments(
                    src_seg=src_seg,
                    dst_seg=dst_seg,
                    app_name=app_name,
                    dep_name=dep.get("name", dep.get("id", "?")),
                    log=cross_zone,
                )
                rules_skipped += 1
                continue

            rule_data = planner.build_rule_payload(
                policy_id=policy_id,
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
            )

            if src_zone:
                src_zone_obj = await self._get_zone(src_zone)
                if src_zone_obj:
                    rule_data["source_zone"] = {"id": src_zone_obj.id}

            if dst_zone:
                dst_zone_obj = await self._get_zone(dst_zone)
                if dst_zone_obj:
                    rule_data["destination_zone"] = {"id": dst_zone_obj.id}

            profile_name = planner.pick_profile_name(app_security_profile, cross_zone)
            if profile_name:
                profile = await self._get_profile(profile_name)
                if profile:
                    rule_data["security_profile"] = {"id": profile.id}

            try:
                _rule, assigned_index = await self._create_or_update_policy_rule(
                    policy_id=policy_id,
                    rule_name=rule_name,
                    rule_data=rule_data,
                )
                self.logger.info(
                    "  Created rule [%d] '%s' (%s -> %s, %s/%s)",
                    assigned_index,
                    rule_name,
                    src_seg.get("name", src_seg_id),
                    dst_seg.get("name", dst_seg_id),
                    protocol,
                    port_start or "any",
                )
                rules_created += 1
                await self._reconcile_tag_rule_from_segments(
                    src_seg=src_seg,
                    dst_seg=dst_seg,
                    app_name=app_name,
                    dep_name=dep.get("name", dep.get("id", "?")),
                    log=cross_zone,
                )
            except Exception as exc:
                self.logger.error("  Failed to create rule '%s': %s", rule_name, exc)

        for src_comp, _dep, _dst_comp in edges:
            src_seg = src_comp.get("network_segment") or {}
            src_seg_id = src_seg.get("id")
            if not src_seg_id or src_seg_id not in segment_policies:
                continue
            await self._attach_policy_to_source_segment(segment=src_seg, policy_id=segment_policies[src_seg_id].id)

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
            dst_comp = dep.get("target") or {}
            if not src_comp or not dst_comp:
                continue
            src_app_name = str((src_comp.get("parent") or {}).get("name") or "")
            if src_app_name != app_name:
                continue
            edges.append((src_comp, dep, dst_comp))
        return edges

    async def _get_or_create_sg(self, sg_name: str, vnet_id: str, acct_id: str | None) -> Any | None:
        """Legacy cloud-SG helper retained for backward compatibility/tests."""
        cache: dict[str, Any] = getattr(self, "_sg_cache", {})
        if sg_name in cache:
            return cache[sg_name]
        try:
            existing = await self.client.filters(kind=CloudSecurityGroup, name__value=sg_name)
            if existing:
                await existing[0].save(allow_upsert=True)
                cache[sg_name] = existing[0]
                self._sg_cache = cache
                return existing[0]
        except Exception:
            pass
        data: dict[str, Any] = {"name": sg_name, "virtual_network": {"id": vnet_id}}
        if acct_id:
            data["account"] = {"id": acct_id}
        try:
            sg = await self.client.create(kind=CloudSecurityGroup, data=data)
            await sg.save(allow_upsert=True)
            self.logger.info("Created CloudSecurityGroup: %s", sg_name)
            cache[sg_name] = sg
            self._sg_cache = cache
            return sg
        except Exception as exc:
            self.logger.error("Failed to create CloudSecurityGroup %s: %s", sg_name, exc)
            cache[sg_name] = None
            self._sg_cache = cache
            return None

    async def _create_cloud_rule(
        self,
        app_name: str,
        src_comp: dict,
        dst_comp: dict,
        dep: dict,
        rule_name: str,
    ) -> bool:
        """Legacy cloud-rule helper retained for backward compatibility/tests."""
        planner = RulesPlanner()
        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        dst_typename = dst_seg.get("typename", "")

        if dst_typename == "CloudNetworkSegment":
            cloud_seg = dst_seg
            cloud_seg_is_dst = True
        else:
            cloud_seg = src_seg
            cloud_seg_is_dst = False

        vnet = cloud_seg.get("virtual_network") or {}
        vnet_id = vnet.get("id")
        acct = vnet.get("account") or {}
        acct_id = acct.get("id") if acct else None

        if not vnet_id:
            self.logger.warning(
                "Cloud rule %s: no virtual_network id on segment %s - skipping",
                rule_name,
                cloud_seg.get("name", "?"),
            )
            return False

        sg_name = f"sg-{app_name}"
        sg = await self._get_or_create_sg(sg_name, vnet_id, acct_id)
        if sg is None:
            return False

        port_info = _resolve_port(dep)
        if port_info is None:
            self.logger.warning(
                "  Cloud dependency '%s' (%s -> %s) has no protocol/port - skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return False
        protocol, port_start, port_end = port_info
        direction = "ingress" if cloud_seg_is_dst else "egress"

        try:
            existing = await self.client.filters(kind=CloudSecurityGroupRule, name__value=rule_name)
            if existing:
                await existing[0].save(allow_upsert=True)
                self.logger.info("  Cloud rule '%s' already exists", rule_name)
                return True
        except Exception:
            pass

        rule_data: dict[str, Any] = {
            "security_group": {"id": sg.id},
            "name": rule_name,
            "direction": direction,
            "protocol": protocol,
            "action": "allow",
            "log": True,
            "disabled": False,
            "description": planner.rule_description(dep, src_comp, dst_comp),
        }
        if port_start is not None:
            rule_data["port_start"] = port_start
        if port_end is not None:
            rule_data["port_end"] = port_end

        if cloud_seg_is_dst:
            src_cidr = planner.seg_cidr(src_seg)
            if src_cidr:
                rule_data["source_cidr"] = src_cidr
        else:
            dst_cidr = planner.seg_cidr(dst_seg)
            if dst_cidr:
                rule_data["dest_cidr"] = dst_cidr

        try:
            rule = await self.client.create(kind=CloudSecurityGroupRule, data=rule_data)
            await rule.save(allow_upsert=True)
            self.logger.info(
                "  Created cloud rule '%s' (dir=%s, sg=%s, %s/%s)",
                rule_name,
                direction,
                sg_name,
                protocol,
                port_start or "any",
            )
            return True
        except Exception as exc:
            self.logger.error("  Failed to create cloud rule '%s': %s", rule_name, exc)
            return False

    async def _get_or_create_policy(self, policy_name: str, app_name: str) -> Any | None:
        try:
            existing = await self.client.filters(kind=SecurityPolicy, name__value=policy_name)
            if existing:
                self.logger.info("Using existing policy: %s", policy_name)
                await existing[0].save(allow_upsert=True)
                return existing[0]
        except Exception as exc:
            self.logger.warning("Could not look up policy %s: %s", policy_name, exc)

        try:
            policy = await self.client.create(
                kind=SecurityPolicy,
                data={
                    "name": policy_name,
                    "description": f"Auto-generated dependency rules for source segment {app_name}",
                    "default_action": "deny",
                    "enabled": True,
                },
            )
            await policy.save(allow_upsert=True)
            self.logger.info("Created policy: %s", policy_name)
            return policy
        except Exception as exc:
            self.logger.error("Failed to create policy %s: %s", policy_name, exc)
            return None

    @staticmethod
    def _segment_policy_name(segment: dict[str, Any]) -> str:
        return RulesPlanner.segment_policy_name(segment)

    @staticmethod
    def _owner_org_id_from_component(component: dict[str, Any]) -> str | None:
        return RulesPlanner.owner_org_id_from_component(component)

    @staticmethod
    def _owner_name_from_component(component: dict[str, Any]) -> str | None:
        return RulesPlanner.owner_name_from_component(component)

    @staticmethod
    def _dependency_access_status(dep: dict[str, Any]) -> str:
        return RulesPlanner.dependency_access_status(dep)

    def _dependency_is_authorized(
        self,
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        dep: dict[str, Any],
    ) -> tuple[bool, str | None]:
        return RulesPlanner.dependency_is_authorized(src_comp, dst_comp, dep)

    def _governance_suffix(self, src_comp: dict[str, Any], dst_comp: dict[str, Any], dep: dict[str, Any]) -> str:
        return RulesPlanner.governance_suffix(src_comp, dst_comp, dep)

    async def _find_existing_policy_rule(self, policy_id: str, rule_name: str) -> Any | None:
        existing_rules = await self.client.filters(kind=SecurityPolicyRule, policy__ids=[policy_id])
        for rule in existing_rules:
            if getattr(rule, "name", None) and rule.name.value == rule_name:
                return rule
        return None

    async def _allocate_policy_rule_index(self, policy_id: str) -> int:
        existing_rules = await self.client.filters(kind=SecurityPolicyRule, policy__ids=[policy_id])
        used_indexes: set[int] = set()
        for rule in existing_rules:
            if getattr(rule, "index", None) and rule.index.value is not None:
                used_indexes.add(int(rule.index.value))

        rule_index = RULE_INDEX_START
        while rule_index in used_indexes:
            rule_index += RULE_INDEX_STEP
        return rule_index

    async def _create_or_update_policy_rule(
        self,
        policy_id: str,
        rule_name: str,
        rule_data: dict[str, Any],
    ) -> tuple[Any, int]:
        return await self._create_or_update_indexed_rule(
            rule_kind=SecurityPolicyRule,
            parent_id=policy_id,
            rule_name=rule_name,
            rule_data=rule_data,
            find_existing=self._find_existing_policy_rule,
            allocate_index=self._allocate_policy_rule_index,
            collision_hint="policy-index",
            max_attempts=RULE_SAVE_ATTEMPTS,
            default_validity_days=RULE_DEFAULT_VALIDITY_DAYS,
        )

    async def _get_zone(self, zone_name: str) -> Any | None:
        cache: dict[str, Any] = getattr(self, "_zone_cache", {})
        if zone_name not in cache:
            try:
                zones = await self.client.filters(kind=SecurityZone, name__value=zone_name)
                cache[zone_name] = zones[0] if zones else None
            except Exception:
                cache[zone_name] = None
            self._zone_cache = cache
        return cache[zone_name]

    async def _get_profile(self, profile_name: str) -> Any | None:
        cache: dict[str, Any] = getattr(self, "_profile_cache", {})
        if profile_name not in cache:
            try:
                profiles = await self.client.filters(kind=SecuritySecurityProfile, name__value=profile_name)
                cache[profile_name] = profiles[0] if profiles else None
            except Exception:
                cache[profile_name] = None
            self._profile_cache = cache
        return cache[profile_name]

    async def _attach_policy_to_source_segment(self, segment: dict[str, Any], policy_id: str) -> None:
        seg_id = segment.get("id")
        if not seg_id:
            return

        seg_typename = segment.get("typename", "ManagedVxlanSegment")
        try:
            seg_obj = await self.client.get(kind=seg_typename, id=seg_id)
            policies_rel = getattr(seg_obj, "security_policies")
            await policies_rel.fetch()
            existing_policy_ids = {peer.id for peer in policies_rel.peers}
            if policy_id not in existing_policy_ids:
                await self._safe_rel_add(policies_rel, {"id": policy_id})
                await seg_obj.save(allow_upsert=True)
                self.logger.info("  Attached source policy to segment %s", segment.get("name", seg_id))
            else:
                await seg_obj.save(allow_upsert=True)
        except Exception as exc:
            self.logger.warning(
                "  Could not attach source policy to segment %s: %s",
                segment.get("name", seg_id),
                exc,
            )

    async def _reconcile_tag_rule_from_segments(
        self,
        src_seg: dict[str, Any],
        dst_seg: dict[str, Any],
        app_name: str,
        dep_name: str,
        log: bool,
    ) -> None:
        src_tag = src_seg.get("security_tag") or {}
        dst_tag = dst_seg.get("security_tag") or {}
        src_tag_id = str(src_tag.get("id") or "")
        dst_tag_id = str(dst_tag.get("id") or "")

        if not src_tag_id or not dst_tag_id:
            return

        try:
            existing = await self.client.filters(
                kind=SecurityTagRule,
                source_tag__ids=[src_tag_id],
                destination_tag__ids=[dst_tag_id],
            )
            if existing:
                await existing[0].save(allow_upsert=True)
                return
        except Exception:
            pass

        try:
            tag_rule = await self.client.create(
                kind=SecurityTagRule,
                data={
                    "source_tag": {"id": src_tag_id},
                    "destination_tag": {"id": dst_tag_id},
                    "action": "permit",
                    "log": log,
                    "description": f"Auto-generated from {app_name} dependency {dep_name}",
                },
            )
            await tag_rule.save(allow_upsert=True)
            self.logger.info(
                "  Reconciled SecurityTagRule %s -> %s",
                src_tag.get("name", src_tag_id),
                dst_tag.get("name", dst_tag_id),
            )
        except Exception as exc:
            self.logger.warning(
                "  Could not reconcile SecurityTagRule for %s -> %s: %s",
                src_tag.get("name", src_tag_id),
                dst_tag.get("name", dst_tag_id),
                exc,
            )

    @staticmethod
    def _pick_profile(app_security_profile: str, cross_zone: bool) -> str | None:
        return RulesPlanner.pick_profile_name(app_security_profile, cross_zone)

    async def _upsert_service_port_object(
        self,
        port: int,
        port_end: int | None,
        protocol: str,
    ) -> Any:
        port_data: dict[str, Any] = {"port": port, "protocol": protocol}
        if port_end is not None:
            port_data["port_end"] = port_end
        port_obj = await self.client.create(kind=AppServicePort, data=port_data)
        await port_obj.save(allow_upsert=True)
        return port_obj

    async def _get_component_with_ports(
        self,
        component_id: str,
    ) -> tuple[Any, Any, set[str]] | None:
        component_obj = await self.client.get(kind=AppComponentKind, id=component_id)
        if component_obj is None:
            self.logger.error("Could not fetch AppComponent object for id %s", component_id)
            return None
        service_ports_rel = getattr(component_obj, "service_ports")
        await service_ports_rel.fetch()
        existing_port_ids = {peer.id for peer in service_ports_rel.peers}
        return component_obj, service_ports_rel, existing_port_ids

    @staticmethod
    def _port_range_str(port: int, port_end: int | None) -> str:
        return f"{port}-{port_end}" if port_end else str(port)

    async def _reconcile_component_service_ports(
        self,
        components: list[dict[str, Any]],
        edges: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    ) -> None:
        ports_by_target: dict[str, set[tuple[int, int | None, str]]] = {}

        for _src_comp, dep, dst_comp in edges:
            target_id = str(dst_comp.get("id") or "")
            if not target_id:
                continue

            derived = PortsPlanner.derive_port_from_dependency_values(
                port_start=dep.get("port_start"),
                port_end=dep.get("port_end"),
                protocol_raw=dep.get("protocol"),
            )
            if derived is None:
                port_start = dep.get("port_start")
                protocol_raw = str(dep.get("protocol") or "").strip().lower()
                if port_start is not None and protocol_raw not in PortsPlanner.SKIP_DEP_PROTOCOLS:
                    self.logger.warning("  Dep -> unknown protocol '%s' - skipping AppServicePort", protocol_raw)
                continue

            ports_by_target.setdefault(target_id, set()).add(derived)

        if not ports_by_target:
            return

        known_components: dict[str, dict[str, Any]] = {
            str(comp.get("id") or ""): comp for comp in components if comp.get("id")
        }

        for target_id, ports in sorted(ports_by_target.items()):
            component_ref = known_components.get(target_id) or {}
            component_slug = str(component_ref.get("slug") or component_ref.get("name") or target_id)

            component_state = await self._get_component_with_ports(target_id)
            if component_state is None:
                continue

            component_obj, service_ports_rel, existing_port_ids = component_state
            component_updated = False

            for port, port_end, protocol in sorted(ports):
                range_str = self._port_range_str(port, port_end)
                try:
                    port_obj = await self._upsert_service_port_object(port=port, port_end=port_end, protocol=protocol)
                    if port_obj.id in existing_port_ids:
                        continue
                    await self._safe_rel_add(service_ports_rel, port_obj)
                    existing_port_ids.add(port_obj.id)
                    component_updated = True
                    self.logger.info("  Linked AppServicePort %s/%s to %s", range_str, protocol, component_slug)
                except Exception as exc:
                    self.logger.error(
                        "  Failed AppServicePort upsert/link %s/%s for %s: %s",
                        range_str,
                        protocol,
                        component_slug,
                        exc,
                    )

            if component_updated:
                try:
                    await component_obj.save(allow_upsert=True)
                except Exception as exc:
                    self.logger.error("  Failed to save component %s service_ports: %s", component_slug, exc)
