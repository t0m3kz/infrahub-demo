"""Generator: app-level automation triggered per AppApplication node.

Currently derives SecurityPolicyRule nodes from AppDependency edges:
  1. Look up (or create) a SecurityPolicy named after the application.
  2. Create one SecurityPolicyRule per AppDependency:
       - source_segment  = source_component.network_segment
       - destination_segment = target_component.network_segment
       - source_zone / destination_zone derived from each segment's security_zone
       - protocol / ports: taken from AppDependency if set, otherwise inferred
         from component_type pairs via PORT_MAP
       - action: permit
       - log: True for any cross-zone flow; False for same-zone flows
  3. Rules are named deterministically so upsert is idempotent.

When the source or destination segment typename == "CloudNetworkSegment", a
CloudSecurityGroupRule is created instead (attached to a CloudSecurityGroup that
is auto-created per application per VPC).

Engineers can review, edit, or disable individual rules in a Proposed Change
before merging. The generator will not re-create rules that already exist (same
policy + index uniqueness constraint guarantees this via allow_upsert=True).
"""

from __future__ import annotations

import inspect
from typing import Any, Protocol

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..helpers.application import (
    ApplicationComponentPlanner,
    ApplicationSegmentRulePlanner,
    ApplicationServicePortPlanner,
)
from ..protocols import (
    AppComponent,
    AppDependency,
    AppServicePort,
    CloudSecurityGroup,
    CloudSecurityGroupRule,
    DcimPhysicalInterface,
    DcimVirtualDevice,
    DcimVirtualInterface,
    LoadbalancerHealthCheck,
    LoadbalancerPoolInterface,
    LoadbalancerPoolMember,
    LoadbalancerVIP,
    ManagedLoadbalancerHA,
    SecurityPolicy,
    SecurityPolicyRule,
    SecuritySecurityProfile,
    SecurityZone,
)


def _seg_cidr(seg: dict) -> str | None:
    """Extract a CIDR string from a segment dict (on-prem or cloud)."""
    return ApplicationSegmentRulePlanner.seg_cidr(seg)


def _resolve_port(
    dep: dict,
) -> tuple[str, int | None, int | None] | None:
    """Return (protocol, port_start, port_end) from an AppDependency node.

    Returns None when the dependency carries no port/protocol — the caller
    must skip rule creation and warn. Every dependency edge must carry
    explicit port information; there is no automatic fallback.
    """
    return ApplicationSegmentRulePlanner.resolve_port(dep)


# Starting rule index — leaves room below 100 for manually-crafted high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10


class AppApplicationGenerator(CommonGenerator):
    """App-level generator: security policy derivation and future app-scoped automation.

    Triggered per AppApplication node (target group: app_applications).
    Query: application — returns the application with all components,
    their depends_on AppDependency edges (each carrying optional port/protocol),
    and each dependency's target component with its network_segment.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        planner = ApplicationSegmentRulePlanner()
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
            self.logger.warning("Application %s has no components — nothing to do", app_name)
            return

        # ── Collect all dependency edges ──────────────────────────────────
        # Each edge is (src_component, dep_node, dst_component).
        # dep_node carries optional protocol/port_start/port_end.
        edges, warnings = planner.collect_dependency_edges(components)
        for warning in warnings:
            self.logger.warning(warning)

        if not edges:
            self.logger.info("Application %s has no depends_on edges — no rules to generate", app_name)
            return

        self.logger.info("Found %d dependency edge(s) for %s", len(edges), app_name)

        # ── Ensure a SecurityPolicy exists for this application ───────────
        policy_name = f"app-{app_name}-dependencies"
        policy = await self._get_or_create_policy(policy_name, app_name)
        if policy is None:
            return
        policy_id = policy.id

        # ── Fetch existing rules for this policy to determine next index ──
        existing_rules = await self.client.filters(
            kind=SecurityPolicyRule,
            policy__ids=[policy_id],
        )
        used_indexes: set[int] = set()
        existing_names: set[str] = set()
        for r in existing_rules:
            if getattr(r, "index", None) and r.index.value:
                used_indexes.add(int(r.index.value))
            if getattr(r, "name", None) and r.name.value:
                existing_names.add(r.name.value)

        next_index = RULE_INDEX_START
        if used_indexes:
            next_index = max(used_indexes) + RULE_INDEX_STEP

        # ── Generate one rule per dependency edge ─────────────────────────
        rules_created = 0
        rules_skipped = 0

        for src_comp, dep, dst_comp in edges:
            src_seg = src_comp.get("network_segment") or {}
            dst_seg = dst_comp.get("network_segment") or {}

            src_seg_id = src_seg.get("id")
            dst_seg_id = dst_seg.get("id")

            if not src_seg_id or not dst_seg_id:
                self.logger.warning(
                    "Dependency %s → %s: one or both components lack a network_segment — skipping",
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
                continue

            rule_name = planner.rule_name(app_name, src_comp, dst_comp)

            port_info = _resolve_port(dep)
            if port_info is None:
                self.logger.warning(
                    "  Dependency '%s' (%s → %s) has no protocol/port — skipping rule creation",
                    dep.get("name", dep.get("id", "?")),
                    src_comp.get("name", "?"),
                    dst_comp.get("name", "?"),
                )
                rules_skipped += 1
                continue
            protocol, port_start, port_end = port_info

            src_zone = (src_seg.get("security_zone") or {}).get("name")
            dst_zone = (dst_seg.get("security_zone") or {}).get("name")
            if src_zone and dst_zone:
                cross_zone = src_zone != dst_zone
            else:
                # Missing zone metadata should never reduce inspection/logging.
                cross_zone = True
                self.logger.warning(
                    "  Dependency '%s' has incomplete zone mapping (%s -> %s); treating as cross-zone",
                    dep.get("name", dep.get("id", "?")),
                    src_zone or "<missing>",
                    dst_zone or "<missing>",
                )

            if rule_name in existing_names:
                self.logger.info("  Rule '%s' already exists — registering with tracker", rule_name)
                for r in existing_rules:
                    if getattr(r, "name", None) and r.name.value == rule_name:
                        await r.save(allow_upsert=True)
                        break
                await self._reconcile_tag_rule_from_segments(
                    src_seg=src_seg,
                    dst_seg=dst_seg,
                    app_name=app_name,
                    dep_name=dep.get("name", dep.get("id", "?")),
                    log=cross_zone,
                )
                rules_skipped += 1
                continue

            rule_data: dict[str, Any] = {
                "policy": {"id": policy_id},
                "index": next_index,
                "name": rule_name,
                "action": "permit",
                "protocol": protocol,
                "log": cross_zone,
                "disabled": False,
                "description": planner.rule_description(dep, src_comp, dst_comp),
                "source_segment": {"id": src_seg_id},
                "destination_segment": {"id": dst_seg_id},
            }

            # Wire zones when available
            if src_zone:
                src_zone_obj = await self._get_zone(src_zone)
                if src_zone_obj:
                    rule_data["source_zone"] = {"id": src_zone_obj.id}

            if dst_zone:
                dst_zone_obj = await self._get_zone(dst_zone)
                if dst_zone_obj:
                    rule_data["destination_zone"] = {"id": dst_zone_obj.id}

            # Wire ports
            if port_start is not None:
                rule_data["port_start"] = port_start
            if port_end is not None:
                rule_data["port_end"] = port_end

            # Attach a security profile based on explicit app security profile.
            profile_name = self._pick_profile(app_security_profile, cross_zone)
            if profile_name:
                profile = await self._get_profile(profile_name)
                if profile:
                    rule_data["security_profile"] = {"id": profile.id}

            src_isolation = src_seg.get("isolation_mode") or "normal"
            dst_isolation = dst_seg.get("isolation_mode") or "normal"
            apply_on_switch = src_isolation == "microsegmented" or dst_isolation == "microsegmented"
            rule_data["apply_on_switch"] = apply_on_switch

            try:
                rule = await self.client.create(kind=SecurityPolicyRule, data=rule_data)
                await rule.save(allow_upsert=True)
                self.logger.info(
                    "  Created rule [%d] '%s' (%s → %s, %s/%s)",
                    next_index,
                    rule_name,
                    src_seg.get("name", src_seg_id),
                    dst_seg.get("name", dst_seg_id),
                    protocol,
                    port_start or "any",
                )
                existing_names.add(rule_name)
                used_indexes.add(next_index)
                next_index += RULE_INDEX_STEP
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

        # ── Attach policy to every involved segment ────────────────────────
        seg_edges = [(src, dst) for src, _dep, dst in edges]
        await self._attach_policy_to_segments(policy_id, seg_edges)

        self.logger.info(
            "Application %s: %d rule(s) created, %d already existed",
            app_name,
            rules_created,
            rules_skipped,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _get_or_create_sg(self, sg_name: str, vnet_id: str, acct_id: str | None) -> Any | None:
        """Fetch or create a CloudSecurityGroup by name."""
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
        data: dict[str, Any] = {
            "name": sg_name,
            "virtual_network": {"id": vnet_id},
        }
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
        """Create a CloudSecurityGroupRule for a cloud segment dependency."""
        planner = ApplicationSegmentRulePlanner()
        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        dst_typename = dst_seg.get("typename", "")

        # Prefer dst for ingress rules; fall back to src for egress
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
                "Cloud rule %s: no virtual_network id on segment %s — skipping",
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
                "  Cloud dependency '%s' (%s → %s) has no protocol/port — skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return False
        protocol, port_start, port_end = port_info
        direction = "ingress" if cloud_seg_is_dst else "egress"

        # Check if rule already exists
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

        # Source/dest CIDR for the non-cloud side
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
        """Fetch an existing SecurityPolicy or create a new one."""
        try:
            existing = await self.client.filters(
                kind="SecurityPolicy",
                name__value=policy_name,
            )
            if existing:
                self.logger.info("Using existing policy: %s", policy_name)
                await existing[0].save(allow_upsert=True)  # register with tracker
                return existing[0]
        except Exception as exc:
            self.logger.warning("Could not look up policy %s: %s", policy_name, exc)

        try:
            policy = await self.client.create(
                kind=SecurityPolicy,
                data={
                    "name": policy_name,
                    "description": f"Auto-generated dependency rules for application {app_name}",
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

    async def _get_zone(self, zone_name: str) -> Any | None:
        """Fetch a SecurityZone by name (cached per generator run)."""
        cache: dict[str, Any] = getattr(self, "_zone_cache", {})
        if zone_name not in cache:
            try:
                zones = await self.client.filters(
                    kind=SecurityZone,
                    name__value=zone_name,
                )
                cache[zone_name] = zones[0] if zones else None
            except Exception:
                cache[zone_name] = None
            self._zone_cache = cache
        return cache[zone_name]

    async def _get_profile(self, profile_name: str) -> Any | None:
        """Fetch a SecuritySecurityProfile by name (cached)."""
        cache: dict[str, Any] = getattr(self, "_profile_cache", {})
        if profile_name not in cache:
            try:
                profiles = await self.client.filters(
                    kind=SecuritySecurityProfile,
                    name__value=profile_name,
                )
                cache[profile_name] = profiles[0] if profiles else None
            except Exception:
                cache[profile_name] = None
            self._profile_cache = cache
        return cache[profile_name]

    async def _attach_policy_to_segments(
        self,
        policy_id: str,
        edges: list[tuple[dict, dict]],
    ) -> None:
        """Ensure every segment involved in this application's rules has the policy
        in its security_policies relationship."""
        seen_seg_ids: set[str] = set()
        for src_comp, dst_comp in edges:
            for comp in (src_comp, dst_comp):
                seg = comp.get("network_segment") or {}
                seg_id = seg.get("id")
                if not seg_id or seg_id in seen_seg_ids:
                    continue
                seen_seg_ids.add(seg_id)

                seg_typename = seg.get("typename", "ManagedVxlanSegment")
                try:
                    seg_obj = await self.client.get(kind=seg_typename, id=seg_id)
                    policies_rel = getattr(seg_obj, "security_policies")
                    await policies_rel.fetch()
                    existing_policy_ids = {peer.id for peer in policies_rel.peers}
                    if policy_id not in existing_policy_ids:
                        policies_rel.add({"id": policy_id})
                        await seg_obj.save(allow_upsert=True)
                        self.logger.info(
                            "  Attached policy to segment %s",
                            seg.get("name", seg_id),
                        )
                    else:
                        await seg_obj.save(allow_upsert=True)  # register with tracker
                except Exception as exc:
                    self.logger.warning(
                        "  Could not attach policy to segment %s: %s",
                        seg.get("name", seg_id),
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
        """Create or re-register a SecurityTagRule when both segments carry security tags."""
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
        """Select a concrete SecuritySecurityProfile from app-level security posture."""
        if not cross_zone:
            return None  # same-zone east-west — no profile needed
        mapping = {
            "fintech_strict": "strict",
            "internal_standard": "standard",
            "internet_exposed": "strict",
        }
        return mapping.get(app_security_profile, "standard")


class AppDependencyRuleGenerator(AppApplicationGenerator):
    """Lightweight generator reconciling a single AppDependency rule.

    Triggered per AppDependency to avoid full application rule recompute.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        planner = ApplicationSegmentRulePlanner()
        cleaned = clean_data(data)
        deps = cleaned.get("AppDependency", [])
        if not deps:
            self.logger.error("No AppDependency data in GraphQL response")
            return

        dep = deps[0]
        src_comp = dep.get("source") or {}
        dst_comp = dep.get("target") or {}

        if not src_comp or not dst_comp:
            self.logger.warning("Dependency missing source or target component — skipping")
            return

        app = src_comp.get("parent") or {}
        app_name = app.get("name", "")
        if not app_name:
            self.logger.warning("Dependency source has no parent application name — skipping")
            return

        app_security_profile = app.get("security_profile", "internal_standard")
        policy_name = f"app-{app_name}-dependencies"
        policy = await self._get_or_create_policy(policy_name, app_name)
        if policy is None:
            return
        policy_id = policy.id

        src_seg = src_comp.get("network_segment") or {}
        dst_seg = dst_comp.get("network_segment") or {}
        src_seg_id = src_seg.get("id")
        dst_seg_id = dst_seg.get("id")
        if not src_seg_id or not dst_seg_id:
            self.logger.warning(
                "Dependency %s -> %s: one or both components lack a network_segment — skipping",
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return

        rule_name = planner.rule_name(app_name, src_comp, dst_comp)

        port_info = _resolve_port(dep)
        if port_info is None:
            self.logger.warning(
                "Dependency '%s' (%s -> %s) has no protocol/port — skipping rule creation",
                dep.get("name", dep.get("id", "?")),
                src_comp.get("name", "?"),
                dst_comp.get("name", "?"),
            )
            return
        protocol, port_start, port_end = port_info

        existing_rules = await self.client.filters(kind=SecurityPolicyRule, policy__ids=[policy_id])
        used_indexes: set[int] = set()
        matched_rule = None
        for rule in existing_rules:
            if getattr(rule, "index", None) and rule.index.value is not None:
                used_indexes.add(int(rule.index.value))
            if getattr(rule, "name", None) and rule.name.value == rule_name:
                matched_rule = rule

        if matched_rule and getattr(matched_rule, "index", None) and matched_rule.index.value is not None:
            rule_index = int(matched_rule.index.value)
        elif used_indexes:
            rule_index = max(used_indexes) + RULE_INDEX_STEP
        else:
            rule_index = RULE_INDEX_START

        src_zone = (src_seg.get("security_zone") or {}).get("name")
        dst_zone = (dst_seg.get("security_zone") or {}).get("name")
        if src_zone and dst_zone:
            cross_zone = src_zone != dst_zone
        else:
            cross_zone = True
            self.logger.warning(
                "Dependency '%s' has incomplete zone mapping (%s -> %s); treating as cross-zone",
                dep.get("name", dep.get("id", "?")),
                src_zone or "<missing>",
                dst_zone or "<missing>",
            )

        rule_data: dict[str, Any] = {
            "policy": {"id": policy_id},
            "index": rule_index,
            "name": rule_name,
            "action": "permit",
            "protocol": protocol,
            "log": cross_zone,
            "disabled": False,
            "description": planner.rule_description(dep, src_comp, dst_comp),
            "source_segment": {"id": src_seg_id},
            "destination_segment": {"id": dst_seg_id},
            "apply_on_switch": (
                (src_seg.get("isolation_mode") or "normal") == "microsegmented"
                or (dst_seg.get("isolation_mode") or "normal") == "microsegmented"
            ),
        }

        if src_zone:
            src_zone_obj = await self._get_zone(src_zone)
            if src_zone_obj:
                rule_data["source_zone"] = {"id": src_zone_obj.id}
        if dst_zone:
            dst_zone_obj = await self._get_zone(dst_zone)
            if dst_zone_obj:
                rule_data["destination_zone"] = {"id": dst_zone_obj.id}

        if port_start is not None:
            rule_data["port_start"] = port_start
        if port_end is not None:
            rule_data["port_end"] = port_end

        profile_name = self._pick_profile(app_security_profile, cross_zone)
        if profile_name:
            profile = await self._get_profile(profile_name)
            if profile:
                rule_data["security_profile"] = {"id": profile.id}

        for attempt in range(1, 4):
            try:
                rule_data["index"] = rule_index
                rule = await self.client.create(kind=SecurityPolicyRule, data=rule_data)
                await rule.save(allow_upsert=True)
                self.logger.info(
                    "Reconciled dependency rule '%s' [%d] for app %s",
                    rule_name,
                    rule_index,
                    app_name,
                )
                await self._reconcile_tag_rule_from_segments(
                    src_seg=src_seg,
                    dst_seg=dst_seg,
                    app_name=app_name,
                    dep_name=dep.get("name", dep.get("id", "?")),
                    log=cross_zone,
                )
                break
            except Exception as exc:
                msg = str(exc)
                if "policy-index" in msg and attempt < 3:
                    refreshed = await self.client.filters(kind=SecurityPolicyRule, policy__ids=[policy_id])
                    used = {
                        int(r.index.value) for r in refreshed if getattr(r, "index", None) and r.index.value is not None
                    }
                    rule_index = (max(used) + RULE_INDEX_STEP) if used else RULE_INDEX_START
                    self.logger.warning(
                        "Index collision for '%s', retrying with index %d (attempt %d/3)",
                        rule_name,
                        rule_index,
                        attempt + 1,
                    )
                    continue
                self.logger.error("Failed to reconcile dependency rule '%s': %s", rule_name, exc)
                return

        await self._attach_policy_to_segments(policy_id, [(src_comp, dst_comp)])


class _AppServicePortMixin:
    """Shared helpers for AppServicePort upsert/link workflows."""

    class _GeneratorRuntime(Protocol):
        client: Any
        logger: Any

    async def _upsert_service_port_object(
        self: _GeneratorRuntime,
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
        self: _GeneratorRuntime,
        component_id: str,
    ) -> tuple[Any, Any, set[str]] | None:
        component_obj = await self.client.get(kind=AppComponent, id=component_id)
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


class AppServicePortGenerator(_AppServicePortMixin, CommonGenerator):
    """Create/upsert global AppServicePort nodes and link them to an AppComponent.

    Triggered once per AppComponent in the app_components group.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        planner = ApplicationServicePortPlanner()
        cleaned = clean_data(data)
        components = cleaned.get("AppComponent", [])
        if not components:
            self.logger.error("No AppComponent data in GraphQL response")
            return

        component = components[0]
        component_id: str = component.get("id", "")
        component_slug: str = component.get("slug", "")

        if not component_id or not component_slug:
            self.logger.error("AppComponent missing id or slug — cannot proceed")
            return

        self.logger.info("Processing service ports for component: %s", component_slug)

        ports: set[tuple[int, int | None, str]] = set()

        try:
            inbound_deps = await self.client.filters(
                kind=AppDependency,
                target__ids=[component_id],
            )
            for dep in inbound_deps:
                port_start = getattr(getattr(dep, "port_start", None), "value", None)
                port_end = getattr(getattr(dep, "port_end", None), "value", None)
                proto_raw = getattr(getattr(dep, "protocol", None), "value", None) or ""
                derived = planner.derive_port_from_dependency_values(
                    port_start=port_start,
                    port_end=port_end,
                    protocol_raw=proto_raw,
                )
                if derived is None:
                    if port_start is not None and proto_raw not in planner.SKIP_DEP_PROTOCOLS:
                        self.logger.warning("  Dep → unknown protocol '%s' — skipping", proto_raw)
                    continue
                ports.add(derived)
                proto = derived[2]
                range_str = f"{port_start}-{port_end}" if port_end else str(port_start)
                self.logger.info("  Dep → port %s/%s", range_str, proto)
        except Exception as exc:
            self.logger.warning("  Could not fetch inbound AppDependency edges: %s", exc)

        if not ports:
            self.logger.info("  No service ports derived for %s — nothing to create", component_slug)
            return

        component_state = await self._get_component_with_ports(component_id)
        if component_state is None:
            return
        component_obj, service_ports_rel, existing_port_ids = component_state
        component_updated = False

        for port, port_end, protocol in sorted(ports):
            range_str = self._port_range_str(port, port_end)
            try:
                port_obj = await self._upsert_service_port_object(port=port, port_end=port_end, protocol=protocol)
                self.logger.info("  Upserted AppServicePort %s/%s (id: %s)", range_str, protocol, port_obj.id)

                if port_obj.id not in existing_port_ids:
                    service_ports_rel.add(port_obj)
                    existing_port_ids.add(port_obj.id)
                    component_updated = True

            except Exception as exc:
                self.logger.error(
                    "  Failed to upsert AppServicePort %s/%s for %s: %s",
                    range_str,
                    protocol,
                    component_slug,
                    exc,
                )
                continue

        if component_updated:
            try:
                await component_obj.save(allow_upsert=True)
                self.logger.info("  Updated service_ports on component %s", component_slug)
            except Exception as exc:
                self.logger.error("  Failed to save component %s service_ports: %s", component_slug, exc)
        else:
            self.logger.info("  No service_ports relationship updates needed for %s", component_slug)


class AppDependencyServicePortGenerator(_AppServicePortMixin, CommonGenerator):
    """Lightweight generator for dependency-driven service-port updates.

    Triggered per AppDependency to avoid recomputing all ports for a component.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        planner = ApplicationServicePortPlanner()
        cleaned = clean_data(data)
        deps = cleaned.get("AppDependency", [])
        if not deps:
            self.logger.error("No AppDependency data in GraphQL response")
            return

        dep = deps[0]
        target = dep.get("target") or {}
        component_id = target.get("id", "")
        component_slug = target.get("slug", "")

        if not component_id:
            self.logger.warning("AppDependency target missing id — cannot update service ports")
            return

        port_tuple = planner.derive_port_from_dependency_values(
            port_start=dep.get("port_start"),
            port_end=dep.get("port_end"),
            protocol_raw=dep.get("protocol"),
        )
        if port_tuple is None:
            self.logger.info(
                "Dependency %s has no AppServicePort-compatible tuple — nothing to update",
                dep.get("name", dep.get("id", "?")),
            )
            return

        port, port_end, protocol = port_tuple
        range_str = self._port_range_str(port, port_end)

        component_state = await self._get_component_with_ports(component_id)
        if component_state is None:
            return
        component_obj, service_ports_rel, existing_port_ids = component_state

        try:
            port_obj = await self._upsert_service_port_object(port=port, port_end=port_end, protocol=protocol)

            if port_obj.id in existing_port_ids:
                self.logger.info(
                    "Dependency-driven port already linked: %s/%s for %s",
                    range_str,
                    protocol,
                    component_slug or component_id,
                )
                return

            service_ports_rel.add(port_obj)
            await component_obj.save(allow_upsert=True)
            self.logger.info(
                "Linked dependency-driven port %s/%s to %s",
                range_str,
                protocol,
                component_slug or component_id,
            )
        except Exception as exc:
            self.logger.error(
                "Failed dependency-driven AppServicePort upsert/link %s/%s for %s: %s",
                range_str,
                protocol,
                component_slug or component_id,
                exc,
            )


class AppComponentGenerator(CommonGenerator):
    """Wire one AppComponent to its network segment (via instance hosts) and LB backend pool.

    Triggered per AppComponent node — query: app_component_data.
    """

    async def generate(self, data: dict[str, Any]) -> None:
        planner = ApplicationComponentPlanner()
        cleaned = clean_data(data)
        comp_list = cleaned.get("AppComponent", [])
        if not comp_list:
            self.logger.error("No AppComponent data in GraphQL response")
            return

        comp = comp_list[0]
        comp_slug: str = comp.get("slug") or comp.get("name", "")

        self.logger.info("Processing AppComponent: %s", comp_slug)

        instances: list[dict[str, Any]] = comp.get("instances") or []

        network_segment = comp.get("network_segment") or {}
        segment_id: str = network_segment.get("id", "")
        segment_name: str = network_segment.get("name", "")
        segment_typename: str = network_segment.get("typename", "")
        is_cloud_segment = planner.is_cloud_segment(network_segment)

        host_ids = planner.collect_host_ids(instances)

        if segment_id and host_ids and not is_cloud_segment:
            await self._assign_segment_to_hosts(
                segment_id=segment_id,
                segment_name=segment_name,
                segment_kind=segment_typename or "ManagedVxlanSegment",
                host_ids=host_ids,
            )
        elif is_cloud_segment:
            self.logger.debug(
                "  %s uses a cloud segment — host uplink assignment not applicable",
                comp_slug,
            )
        elif host_ids and not segment_id:
            self.logger.warning(
                "  %s has instances but no network_segment — skipping host uplink assignment",
                comp_slug,
            )

        lb_ha_id = await self._resolve_component_lb_ha_id(comp=comp, comp_slug=comp_slug)
        if not lb_ha_id:
            self.logger.info("  %s has no load_balancer assigned — skipping LB wiring", comp_slug)
            return

        vip_obj = await self._ensure_vip_service_from_ports(comp=comp, comp_slug=comp_slug, lb_ha_id=lb_ha_id)
        if not vip_obj:
            self.logger.info("  %s has no creatable VIP from service_ports — skipping LB wiring", comp_slug)
            return

        vip_id = vip_obj.id

        vip_hostname: str = getattr(getattr(vip_obj, "hostname", None), "value", vip_id)
        vip_proto: str = getattr(getattr(vip_obj, "protocol", None), "value", "")
        vip_port: str = str(getattr(getattr(vip_obj, "port", None), "value", ""))

        await self._assign_vip_to_lb(vip_obj, vip_id, vip_hostname)

        service_ports: list[dict[str, Any]] = comp.get("service_ports") or []
        backend_port = planner.backend_port(service_ports)

        vm_instances = [i for i in instances if i.get("typename") == "DcimVirtualDevice"]
        for vm_stub in vm_instances:
            vm_id: str = vm_stub.get("id", "")
            vm_name: str = vm_stub.get("name", vm_id)
            if not vm_id:
                continue
            await self._wire_pool_member(
                member_name=f"{comp_slug}-{vm_name}",
                vm_id=vm_id,
                vm_name=vm_name,
                vip_id=vip_id,
                vip_hostname=vip_hostname,
                vip_proto=vip_proto,
                vip_port=vip_port,
                backend_port=backend_port,
            )

    async def _ensure_vip_service_from_ports(
        self,
        comp: dict[str, Any],
        comp_slug: str,
        lb_ha_id: str,
    ) -> Any | None:
        """Create or upsert one deterministic VIP from service_ports."""
        planner = ApplicationComponentPlanner()

        service_ports: list[dict[str, Any]] = comp.get("service_ports") or []
        selected = planner.select_primary_service_port(service_ports)
        if selected is None:
            self.logger.info("  %s has LB HA assigned but no service_ports — skipping VIP auto-create", comp_slug)
            return None

        port, service_port_protocol = selected

        app_fqdn = ""
        component_name = str(comp.get("name") or comp_slug)
        component_type = str(comp.get("component_type") or "backend")
        vip_hostname = planner.derive_vip_hostname(app_fqdn, component_name, component_type, comp_slug)
        vip_protocol = planner.to_vip_protocol(service_port_protocol=service_port_protocol, port=port)

        try:
            vip_obj = await self.client.create(
                kind=LoadbalancerVIP,
                data={
                    "hostname": vip_hostname,
                    "protocol": vip_protocol,
                    "port": port,
                    "status": "active",
                    "load_balancing_algorithm": "round_robin",
                    "load_balancer": {"id": lb_ha_id},
                },
            )
            await vip_obj.save(allow_upsert=True)
            self.logger.info(
                "  Upserted VIP %s (%s/%s) on LB HA %s",
                vip_hostname,
                vip_protocol,
                port,
                lb_ha_id,
            )
        except Exception as exc:
            self.logger.error(
                "  Failed auto VIP create for %s on LB HA %s: %s",
                comp_slug,
                lb_ha_id,
                exc,
            )
            return None

        component_id = str(comp.get("id") or "")
        if component_id:
            await self._attach_component_to_vip(
                vip_obj=vip_obj,
                component_id=component_id,
                comp_slug=comp_slug,
            )

        health_check = await self._attach_default_health_check(
            vip_obj=vip_obj, vip_protocol=vip_protocol, comp_slug=comp_slug
        )

        if health_check and component_id:
            await self._attach_health_check_to_component(
                component_id=component_id,
                health_check=health_check,
                comp_slug=comp_slug,
            )

        return vip_obj

    @staticmethod
    async def _safe_rel_add(rel: Any, obj: Any) -> None:
        """Add a relationship peer while supporting sync and async add() variants."""
        result = rel.add(obj)
        if inspect.isawaitable(result):
            await result

    async def _resolve_component_lb_ha_id(self, comp: dict[str, Any], comp_slug: str) -> str:
        """Resolve assigned LB HA id for component from query data or SDK object."""
        load_balancer = comp.get("load_balancer") or {}
        lb_ha_id = str(load_balancer.get("id") or "")
        if lb_ha_id:
            return lb_ha_id

        component_id = str(comp.get("id") or "")
        if not component_id:
            return ""

        try:
            component_obj = await self.client.get(kind=AppComponent, id=component_id, prefetch_relationships=True)
            if component_obj is None:
                return ""
            lb_rel = getattr(component_obj, "load_balancer", None)
            lb_peer = getattr(lb_rel, "peer", None) if lb_rel else None
            resolved_id = str(getattr(lb_peer, "id", "") or "")
            if resolved_id:
                return resolved_id
        except Exception as exc:
            self.logger.warning("  Could not resolve load_balancer for %s: %s", comp_slug, exc)

        return ""

    async def _attach_default_health_check(
        self,
        vip_obj: Any,
        vip_protocol: str,
        comp_slug: str,
    ) -> Any | None:
        """Attach one default health check to VIP in an idempotent way."""
        check_type = self._health_check_type_for_vip(vip_protocol)

        try:
            health_check = await self.client.create(
                kind=LoadbalancerHealthCheck,
                data={
                    "check": check_type,
                    "rise": 3,
                    "fall": 3,
                    "timeout": 1000,
                },
            )
            await health_check.save(allow_upsert=True)
        except Exception as exc:
            self.logger.warning(
                "  Could not upsert default health check (%s) for %s: %s",
                check_type,
                comp_slug,
                exc,
            )
            return None

        try:
            vip_hc_rel = getattr(vip_obj, "health_checks", None)
            if vip_hc_rel is None:
                self.logger.warning("  VIP health_checks relationship missing for %s", comp_slug)
                return health_check

            await vip_hc_rel.fetch()
            existing_ids = {peer.id for peer in vip_hc_rel.peers}
            if health_check.id in existing_ids:
                return health_check

            await self._safe_rel_add(vip_hc_rel, health_check)
            await vip_obj.save(allow_upsert=True)
            self.logger.info("  Attached %s health-check to VIP for %s", check_type, comp_slug)
        except Exception as exc:
            self.logger.warning(
                "  Could not attach health check to VIP for %s: %s",
                comp_slug,
                exc,
            )

        return health_check

    async def _attach_component_to_vip(
        self,
        vip_obj: Any,
        component_id: str,
        comp_slug: str,
    ) -> None:
        """Attach AppComponent to VIP for VIP-centric component visibility."""
        vip_components_rel = getattr(vip_obj, "app_components", None)
        if vip_components_rel is None:
            self.logger.debug("  VIP relation app_components missing for %s", comp_slug)
            return

        try:
            await vip_components_rel.fetch()
            existing_ids = {peer.id for peer in vip_components_rel.peers}
            if component_id in existing_ids:
                return

            await self._safe_rel_add(vip_components_rel, {"id": component_id})
            await vip_obj.save(allow_upsert=True)
            self.logger.info("  Attached AppComponent %s to VIP", comp_slug)
        except Exception as exc:
            self.logger.warning(
                "  Could not attach AppComponent %s to VIP: %s",
                comp_slug,
                exc,
            )

    async def _attach_health_check_to_component(
        self,
        component_id: str,
        health_check: Any,
        comp_slug: str,
    ) -> None:
        """Attach health check to application component for component-centric mapping."""
        try:
            component_obj = await self.client.get(kind=AppComponent, id=component_id, prefetch_relationships=True)
        except Exception as exc:
            self.logger.warning("  Could not fetch AppComponent %s for health-check mapping: %s", component_id, exc)
            return

        if component_obj is None:
            return

        component_hc_rel = getattr(component_obj, "health_checks", None)
        if component_hc_rel is None:
            self.logger.debug("  Component %s has no health_checks relationship", component_id)
            return

        try:
            await component_hc_rel.fetch()
            existing_ids = {peer.id for peer in component_hc_rel.peers}
            if health_check.id in existing_ids:
                return

            await self._safe_rel_add(component_hc_rel, health_check)
            await component_obj.save(allow_upsert=True)
            self.logger.info("  Attached health-check to AppComponent %s", comp_slug)
        except Exception as exc:
            self.logger.warning(
                "  Could not attach health-check to AppComponent %s (%s): %s",
                comp_slug,
                component_id,
                exc,
            )

    @staticmethod
    def _health_check_type_for_vip(vip_protocol: str) -> str:
        """Map VIP protocol to one supported LB health-check type."""
        proto = (vip_protocol or "tcp").lower()
        if proto == "http":
            return "http"
        if proto in {"https", "tls"}:
            return "ssl"
        return "tcp"

    async def _assign_segment_to_hosts(
        self,
        segment_id: str,
        segment_name: str,
        segment_kind: str,
        host_ids: set[str],
    ) -> None:
        """Assign network_segment to the uplink interfaces of the given physical hosts."""
        self.logger.info(
            "  Segment '%s' (%s) — assigning to uplink interfaces on %d host(s)",
            segment_name,
            segment_kind,
            len(host_ids),
        )

        try:
            segment_obj = await self.client.get(kind=segment_kind, id=segment_id)
        except Exception as exc:
            self.logger.warning("  Could not fetch segment SDK object for '%s': %s", segment_name, exc)
            return
        if not segment_obj:
            return

        try:
            interfaces = await self.client.filters(
                kind=DcimPhysicalInterface,
                device__ids=list(host_ids),
                role__value="uplink",
                status__value="active",
            )
        except Exception as exc:
            self.logger.warning("  Could not query uplink interfaces for hosts: %s", exc)
            return

        assigned = 0
        for iface in interfaces:
            iface_caps = getattr(iface, "interface_capabilities")
            await iface_caps.fetch()
            existing_ids = {peer.id for peer in iface_caps.peers}
            if segment_id not in existing_ids:
                iface_caps.add(segment_obj)
                assigned += 1
            await iface.save(allow_upsert=True)

        self.logger.info(
            "  Assigned segment '%s' to %d uplink interface(s) (%d already assigned)",
            segment_name,
            assigned,
            len(interfaces) - assigned,
        )

    async def _assign_vip_to_lb(
        self,
        vip_obj: Any,
        vip_id: str,
        vip_hostname: str,
    ) -> None:
        """Add vip_obj to interface_capabilities on the '1.1' interface of every
        physical device that is a member of the VIP's ManagedLoadbalancerHA cluster."""
        lb_rel = getattr(vip_obj, "load_balancer", None)
        lb_peer = getattr(lb_rel, "peer", None) if lb_rel else None
        if not lb_peer or not getattr(lb_peer, "id", None):
            return

        try:
            lb_ha = await self.client.get(
                kind=ManagedLoadbalancerHA,
                id=lb_peer.id,
                prefetch_relationships=True,
            )
        except Exception as exc:
            self.logger.warning("  Could not fetch ManagedLoadbalancerHA %s: %s", lb_peer.id, exc)
            return
        if not lb_ha:
            return

        try:
            await lb_ha.capabilities.fetch()
            lb_devices = list(lb_ha.capabilities.peers)
        except Exception as exc:
            self.logger.warning("  Could not fetch LB HA capabilities: %s", exc)
            return

        for lb_dev in lb_devices:
            lb_dev_name = getattr(getattr(lb_dev, "name", None), "value", lb_dev.id)
            try:
                ingress_ifaces = await self.client.filters(
                    kind=DcimPhysicalInterface,
                    device__ids=[lb_dev.id],
                    name__value="1.1",
                )
            except Exception as exc:
                self.logger.warning("  Could not query ingress interface on %s: %s", lb_dev_name, exc)
                continue

            for iface in ingress_ifaces:
                try:
                    iface_caps = getattr(iface, "interface_capabilities")
                    await iface_caps.fetch()
                    existing_ids = {peer.id for peer in iface_caps.peers}
                    if vip_id not in existing_ids:
                        iface_caps.add(vip_obj)
                        self.logger.info("  Assigned VIP %s to %s:1.1", vip_hostname, lb_dev_name)
                    await iface.save(allow_upsert=True)
                except Exception as exc:
                    self.logger.warning("  Failed to assign VIP to %s:1.1: %s", lb_dev_name, exc)

    async def _wire_pool_member(
        self,
        member_name: str,
        vm_id: str,
        vm_name: str,
        vip_id: str,
        vip_hostname: str,
        vip_proto: str,
        vip_port: str,
        backend_port: int | None,
    ) -> None:
        """Create (or re-register) one LoadbalancerPoolMember + PoolInterface for a VM."""
        try:
            existing_members = await self.client.filters(
                kind=LoadbalancerPoolMember,
                name__value=member_name,
            )
        except Exception as exc:
            self.logger.warning("  Could not check existing PoolMember '%s': %s", member_name, exc)
            existing_members = []

        if existing_members:
            try:
                await existing_members[0].save(allow_upsert=True)
            except Exception:
                pass
            self.logger.info("  PoolMember %s already exists — re-registered", member_name)
            return

        try:
            vm_full = await self.client.get(
                kind=DcimVirtualDevice,
                id=vm_id,
                prefetch_relationships=True,
            )
        except Exception as exc:
            self.logger.warning("  Could not fetch DcimVirtualDevice %s: %s", vm_id, exc)
            vm_full = None

        ip_id: str | None = None
        if vm_full:
            primary_addr_rel = getattr(vm_full, "primary_address", None)
            primary_addr_peer = getattr(primary_addr_rel, "peer", None) if primary_addr_rel else None
            ip_id = getattr(primary_addr_peer, "id", None) if primary_addr_peer else None

        try:
            pool_member = await self.client.create(
                kind=LoadbalancerPoolMember,
                data={
                    "name": member_name,
                    "status": "active",
                    "vip_service": {"id": vip_id},
                    "weight": 1,
                },
            )
            await pool_member.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error("  Failed to create PoolMember '%s': %s", member_name, exc)
            return

        if vm_full:
            try:
                vm_caps = getattr(vm_full, "capabilities")
                await vm_caps.fetch()
                existing_cap_ids = {peer.id for peer in vm_caps.peers}
                if pool_member.id not in existing_cap_ids:
                    vm_caps.add(pool_member)
                    await vm_full.save(allow_upsert=True)
            except Exception as exc:
                self.logger.warning("  Could not link PoolMember to VM %s capabilities: %s", vm_name, exc)

        pi_name = f"{member_name}-iface"
        pool_iface_data: dict[str, Any] = {
            "name": pi_name,
            "status": "active",
            "pool_member": {"id": pool_member.id},
        }
        if backend_port is not None:
            pool_iface_data["port"] = backend_port
        if ip_id:
            pool_iface_data["ip_address"] = {"id": ip_id}

        try:
            pool_iface = await self.client.create(
                kind=LoadbalancerPoolInterface,
                data=pool_iface_data,
            )
            await pool_iface.save(allow_upsert=True)
        except Exception as exc:
            self.logger.error("  Failed to create PoolInterface '%s': %s", pi_name, exc)
            return

        if vm_full:
            target_iface = None
            try:
                vm_ifaces = await self.client.filters(
                    kind=DcimVirtualInterface,
                    device__ids=[vm_full.id],
                    status__value="active",
                )
                if not vm_ifaces:
                    vm_ifaces = await self.client.filters(
                        kind=DcimPhysicalInterface,
                        device__ids=[vm_full.id],
                        status__value="active",
                    )
                if vm_ifaces:
                    target_iface = vm_ifaces[0]
            except Exception as exc:
                self.logger.warning("  Could not query interfaces for VM %s: %s", vm_name, exc)

            if target_iface:
                try:
                    pi_caps = getattr(target_iface, "interface_capabilities")
                    await pi_caps.fetch()
                    existing_pi_ids = {peer.id for peer in pi_caps.peers}
                    if pool_iface.id not in existing_pi_ids:
                        pi_caps.add(pool_iface)
                        await target_iface.save(allow_upsert=True)
                except Exception as exc:
                    self.logger.warning("  Could not link PoolInterface to %s interface: %s", vm_name, exc)

        self.logger.info(
            "  Wired %s → %s:%s:%s",
            member_name,
            vip_hostname,
            vip_proto,
            vip_port,
        )
