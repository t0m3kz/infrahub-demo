"""Mixin for on-prem segment-to-segment firewall rules (SecurityPolicy/SecurityPolicyRule)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from .helpers.rules import RulesPlanner
from .named_objects import GetOrCreateByNameMixin
from .protocols import SecurityPolicy, SecurityPolicyRule, SecuritySecurityProfile, SecurityTagRule, SecurityZone
from .rules import RuleLifecycleMixin

if TYPE_CHECKING:
    import logging

# Starting rule index - leaves room below 100 for manually-crafted high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10
RULE_SAVE_ATTEMPTS = 5
RULE_DEFAULT_VALIDITY_DAYS = 180


class SegmentFirewallMixin(GetOrCreateByNameMixin, RuleLifecycleMixin):
    """On-prem segment-to-segment dependency rules — the SecurityPolicy/
    SecurityPolicyRule path dispatched from _reconcile_application_rules for
    every AppDependency edge that isn't cloud-side (CloudSecurityRuleMixin),
    external (ZtnaMixin's egress path) or private_access (ZtnaMixin's publish
    path). Also owns macro-zone/threat-profile lookups, the SecurityTagRule
    micro-segmentation mirror, and the microsegmented return-rule leg.

    Expects the host class to provide: ``client``, ``logger``, ``_safe_rel_add``.
    """

    client: Any
    logger: logging.Logger
    _safe_rel_add: Callable[..., Any]

    async def _reconcile_segment_rule(
        self,
        *,
        app_name: str,
        app_security_profile: str,
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        dep: dict[str, Any],
        src_seg: dict[str, Any],
        dst_seg: dict[str, Any],
        planner: RulesPlanner,
        segment_policies: dict[str, Any],
    ) -> tuple[bool, bool]:
        """On-prem segment-to-segment dependency rule for one edge.

        Returns (created, skipped) for the caller to fold into its running
        counters — a missing network_segment contributes to neither (matches
        the pre-extraction behaviour of silently `continue`-ing).
        """
        src_seg_id = src_seg.get("id")
        dst_seg_id = dst_seg.get("id")
        src_seg_name = str(src_seg.get("name") or src_seg_id or "")

        if not src_seg_id or not dst_seg_id or not src_seg_name:
            self.logger.warning(
                "Dependency %s -> %s: one or both components lack a network_segment - skipping",
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return False, False

        policy = segment_policies.get(src_seg_id)
        if policy is None:
            policy_name = planner.segment_policy_name(src_seg)
            policy = await self._get_or_create_policy(policy_name, src_seg_name)
            if policy is None:
                return False, True
            segment_policies[src_seg_id] = policy

        policy_id = policy.id
        rule_name = planner.rule_name(app_name, src_comp, dst_comp)

        port_info = RulesPlanner.resolve_port(dep)
        if port_info is None:
            self.logger.warning(
                "  Dependency '%s' (%s -> %s) has no protocol/port - skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return False, True
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
            return False, True

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
            await self._reconcile_tag_rule_from_segments(
                src_seg=src_seg,
                dst_seg=dst_seg,
                app_name=app_name,
                dep_name=dep.get("name", dep.get("id", "?")),
                log=cross_zone,
            )
            if rule_data.get("apply_on_switch"):
                await self._reconcile_return_rule_for_microsegmented(
                    app_name=app_name,
                    src_comp=src_comp,
                    dst_comp=dst_comp,
                    dep=dep,
                    src_seg=src_seg,
                    dst_seg=dst_seg,
                    dst_seg_id=dst_seg_id,
                    protocol=protocol,
                    port_start=port_start,
                    port_end=port_end,
                    cross_zone=cross_zone,
                    segment_policies=segment_policies,
                )
            return True, False
        except Exception as exc:
            self.logger.error("  Failed to create rule '%s': %s", rule_name, exc)
            return False, False

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

    async def _get_or_create_policy(self, policy_name: str, app_name: str) -> Any | None:
        return await self._get_or_create_by_name(
            kind=SecurityPolicy,
            name=policy_name,
            create_data={
                "name": policy_name,
                "description": f"Auto-generated dependency rules for source segment {app_name}",
                "default_action": "deny",
                "enabled": True,
            },
            found_log="Using existing policy: %s",
            created_log="Created policy: %s",
        )

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

    async def _cached_lookup_by_name(self, *, cache_attr: str, kind: Any, name: str) -> Any | None:
        """Shared cache-or-fetch-by-name-value shape (read-only — never
        creates on a miss, unlike _get_or_create_by_name)."""
        cache: dict[str, Any] = getattr(self, cache_attr, {})
        if name not in cache:
            try:
                found = await self.client.filters(kind=kind, name__value=name)
                cache[name] = found[0] if found else None
            except Exception:
                cache[name] = None
            setattr(self, cache_attr, cache)
        return cache[name]

    async def _get_zone(self, zone_name: str) -> Any | None:
        return await self._cached_lookup_by_name(cache_attr="_zone_cache", kind=SecurityZone, name=zone_name)

    async def _get_profile(self, profile_name: str) -> Any | None:
        return await self._cached_lookup_by_name(
            cache_attr="_profile_cache", kind=SecuritySecurityProfile, name=profile_name
        )

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
                await seg_obj.save(allow_upsert=True, update_group_context=False)
                self.logger.info("  Attached source policy to segment %s", segment.get("name", seg_id))
            else:
                await seg_obj.save(allow_upsert=True, update_group_context=False)
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

    async def _reconcile_return_rule_for_microsegmented(
        self,
        *,
        app_name: str,
        src_comp: dict[str, Any],
        dst_comp: dict[str, Any],
        dep: dict[str, Any],
        src_seg: dict[str, Any],
        dst_seg: dict[str, Any],
        dst_seg_id: str,
        protocol: str,
        port_start: int | None,
        port_end: int | None,
        cross_zone: bool,
        segment_policies: dict[str, Any],
    ) -> bool:
        """Mirror a microsegmented (apply_on_switch) permit with a return rule.

        A microsegmented rule is enforced at a stateless switch ACL, which has
        no connection tracking to auto-permit response traffic the way a
        stateful firewall would — so the forward permit alone silently drops
        the return leg. Scoped to the destination segment's own policy
        (reusing the same segment_policies cache the forward pass built), so
        it composes with the existing per-source-segment policy attachment.
        """
        planner = RulesPlanner()
        dst_seg_name = str(dst_seg.get("name") or dst_seg_id or "")

        policy = segment_policies.get(dst_seg_id)
        if policy is None:
            policy_name = planner.segment_policy_name(dst_seg)
            policy = await self._get_or_create_policy(policy_name, dst_seg_name)
            if policy is None:
                return False
            segment_policies[dst_seg_id] = policy

        return_rule_name = f"{planner.rule_name(app_name, dst_comp, src_comp)}-return"
        existing_rule = await self._find_existing_policy_rule(policy_id=policy.id, rule_name=return_rule_name)
        if existing_rule is not None:
            await existing_rule.save(allow_upsert=True)
            return True

        return_rule_data = planner.build_rule_payload(
            policy_id=policy.id,
            rule_name=return_rule_name,
            dep=dep,
            src_comp=dst_comp,
            dst_comp=src_comp,
            src_seg=dst_seg,
            dst_seg=src_seg,
            protocol=protocol,
            port_start=port_start,
            port_end=port_end,
            cross_zone=cross_zone,
        )
        try:
            await self._create_or_update_policy_rule(
                policy_id=policy.id,
                rule_name=return_rule_name,
                rule_data=return_rule_data,
            )
            self.logger.info("  Created microsegmented return rule '%s'", return_rule_name)
            return True
        except Exception as exc:
            self.logger.error("  Failed to create return rule '%s': %s", return_rule_name, exc)
            return False

    @staticmethod
    def _pick_profile(app_security_profile: str, cross_zone: bool) -> str | None:
        return RulesPlanner.pick_profile_name(app_security_profile, cross_zone)
