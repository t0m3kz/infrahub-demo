"""Security rule generators derived from application dependencies."""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.rules import RulesPlanner
from ..protocols import (
    CloudSecurityGroup,
    CloudSecurityGroupRule,
    SecurityPolicy,
    SecurityPolicyRule,
    SecuritySecurityProfile,
    SecurityZone,
)
from ..rules import RuleLifecycleMixin

# Starting rule index - leaves room below 100 for manually-crafted high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10
RULE_SAVE_ATTEMPTS = 5
RULE_DEFAULT_VALIDITY_DAYS = 180


def _seg_cidr(seg: dict) -> str | None:
    """Extract a CIDR string from a segment dict (on-prem or cloud)."""
    return RulesPlanner.seg_cidr(seg)


def _resolve_port(dep: dict) -> tuple[str, int | None, int | None] | None:
    """Return (protocol, port_start, port_end) from an AppDependency node."""
    return RulesPlanner.resolve_port(dep)


class AppApplicationGenerator(RuleLifecycleMixin, CommonGenerator):
    """Generate segment-scoped security rules from app dependencies."""

    async def generate(self, data: dict[str, Any]) -> None:
        planner = RulesPlanner()
        cleaned = clean_data(data)
        app_list = cleaned.get("AppApplication", [])
        if not app_list:
            self.logger.error("No AppApplication data in GraphQL response")
            return

        app = app_list[0]
        app_name: str = app.get("name", "")
        app_security_profile: str = app.get("security_profile", "internal_standard")
        self.logger.info("Processing security rules for application: %s", app_name)

        components: list[dict] = app.get("children", [])
        if not components:
            self.logger.warning("Application %s has no components - nothing to do", app_name)
            return

        edges, warnings = planner.collect_dependency_edges(components)
        for warning in warnings:
            self.logger.warning(warning)

        if not edges:
            self.logger.info("Application %s has no depends_on edges - no rules to generate", app_name)
            return

        self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)

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
            existing = await self.client.filters(kind="SecurityPolicy", name__value=policy_name)
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
                kind="SecurityTagRule",
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
                kind="SecurityTagRule",
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


class AppDependencyRuleGenerator(AppApplicationGenerator):
    """Reconcile one dependency rule without full application recompute."""

    async def generate(self, data: dict[str, Any]) -> None:
        planner = RulesPlanner()
        cleaned = clean_data(data)
        deps = cleaned.get("AppDependency", [])
        if not deps:
            self.logger.error("No AppDependency data in GraphQL response")
            return

        dep = deps[0]
        src_comp = dep.get("source") or {}
        dst_comp = dep.get("target") or {}

        if not src_comp or not dst_comp:
            self.logger.warning("Dependency missing source or target component - skipping")
            return

        app = src_comp.get("parent") or {}
        app_name = app.get("name", "")
        if not app_name:
            self.logger.warning("Dependency source has no parent application name - skipping")
            return

        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        src_seg_id = src_seg.get("id")
        dst_seg_id = dst_seg.get("id")
        src_seg_name = str(src_seg.get("name") or src_seg_id or app_name)
        if not src_seg_id or not dst_seg_id:
            self.logger.warning(
                "Dependency %s -> %s: one or both components lack a network_segment - skipping",
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return

        authorized, auth_reason = planner.dependency_is_authorized(src_comp=src_comp, dst_comp=dst_comp, dep=dep)
        if not authorized:
            self.logger.warning(
                "Dependency '%s' (%s -> %s) is not authorized: %s",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
                auth_reason or "missing approval",
            )
            return

        app_security_profile = app.get("security_profile", "internal_standard")
        policy_name = planner.segment_policy_name(src_seg)
        policy = await self._get_or_create_policy(policy_name, src_seg_name)
        if policy is None:
            return
        policy_id = policy.id

        rule_name = planner.rule_name(app_name, src_comp, dst_comp)

        port_info = _resolve_port(dep)
        if port_info is None:
            self.logger.warning(
                "Dependency '%s' (%s -> %s) has no protocol/port - skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return
        protocol, port_start, port_end = port_info

        src_zone, dst_zone, cross_zone = planner.zone_context(src_seg=src_seg, dst_seg=dst_seg, dep=dep)
        if src_zone is None or dst_zone is None:
            cross_zone = True
            self.logger.warning(
                "Dependency '%s' has incomplete zone mapping (%s -> %s); treating as cross-zone",
                dep.get("name", dep.get("id", "?")),
                src_zone or "<missing>",
                dst_zone or "<missing>",
            )

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
            _rule, rule_index = await self._create_or_update_policy_rule(
                policy_id=policy_id,
                rule_name=rule_name,
                rule_data=rule_data,
            )
            self.logger.info(
                "Reconciled dependency rule '%s' [%d] for source segment %s",
                rule_name,
                rule_index,
                src_seg_name,
            )
            await self._reconcile_tag_rule_from_segments(
                src_seg=src_seg,
                dst_seg=dst_seg,
                app_name=app_name,
                dep_name=dep.get("name", dep.get("id", "?")),
                log=cross_zone,
            )
        except Exception as exc:
            self.logger.error("Failed to reconcile dependency rule '%s': %s", rule_name, exc)
            return

        await self._attach_policy_to_source_segment(segment=src_seg, policy_id=policy_id)
