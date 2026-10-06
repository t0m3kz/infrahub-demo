"""Mixin for on-prem segment-to-segment firewall rules (SecurityPolicy/SecurityPolicyRule)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from utils.ports import PortSpec

from .helpers.rules import RulesPlanner
from .named_objects import GetOrCreateByNameMixin
from .protocols import SecurityPolicy, SecurityPolicyRule, SecuritySecurityProfile, SecurityTagRule, SecurityZone

if TYPE_CHECKING:
    import logging

# Starting rule index - leaves room below 100 for manually-crafted high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10
RULE_SAVE_ATTEMPTS = 5
RULE_DEFAULT_VALIDITY_DAYS = 180


def _default_expiry_iso() -> str:
    """UTC ISO timestamp RULE_DEFAULT_VALIDITY_DAYS from now."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=RULE_DEFAULT_VALIDITY_DAYS)
    return expires_at.replace(microsecond=0).isoformat()


def _is_expired(value: Any) -> bool:
    """Whether a datetime or ISO string (``Z`` suffix allowed) is at or before now (UTC)."""
    if isinstance(value, datetime):
        expires_at = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            expires_at = datetime.fromisoformat(raw)
        except ValueError:
            return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _next_free_rule_index(used: set[int]) -> int:
    rule_index = RULE_INDEX_START
    while rule_index in used:
        rule_index += RULE_INDEX_STEP
    return rule_index


class SegmentFirewallMixin(GetOrCreateByNameMixin):
    """On-prem segment-to-segment dependency rules — the SecurityPolicy/
    SecurityPolicyRule path dispatched from _reconcile_application_rules for
    every AppDependency edge that isn't cloud-side (CloudSecurityRuleMixin)
    or external (target_fqdn, ZtnaMixin's egress path). Also owns macro-zone/threat-profile lookups and the SecurityTagRule
    micro-segmentation mirror. The return leg of a rule is not a rule of its
    own: the destination segment's leaf ACL builds it from inbound_rules
    (transforms/helpers/acl.py).

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
        rule_indexes: dict[str, set[int]],
        port: PortSpec,
    ) -> tuple[bool, bool]:
        """On-prem segment-to-segment dependency rule for one edge and port.

        ``segment_policies`` (source segment id -> policy) and ``rule_indexes``
        (policy id -> used rule indexes) are the caller's per-run caches.
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
        rule_name = planner.rule_name(app_name, src_comp, dst_comp, dep, port)
        protocol, port_start, port_end = port

        src_zone, dst_zone, cross_zone = planner.zone_context(src_seg=src_seg, dst_seg=dst_seg, dep=dep)
        if src_zone is None or dst_zone is None:
            cross_zone = True
            self.logger.warning(
                "  Dependency '%s' has incomplete zone mapping (%s -> %s); treating as cross-zone",
                dep.get("name", dep.get("id", "?")),
                src_zone or "<missing>",
                dst_zone or "<missing>",
            )

        existing_rule = await self._find_rule_by_name(SecurityPolicyRule, policy_id, rule_name)
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
                rule_indexes=rule_indexes,
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
            return True, False
        except Exception as exc:
            self.logger.error("  Failed to create rule '%s': %s", rule_name, exc)
            return False, False

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

    async def _read_policy_rules(self, policy_id: str, rule_indexes: dict[str, set[int]]) -> list[Any]:
        """List the policy's rules from the server and record their indexes in ``rule_indexes``."""
        rules = await self.client.filters(kind=SecurityPolicyRule, policy__ids=[policy_id])
        rule_indexes[policy_id] = {int(rule.index.value) for rule in rules if rule.index.value is not None}
        return rules

    async def _create_or_update_policy_rule(
        self,
        policy_id: str,
        rule_name: str,
        rule_data: dict[str, Any],
        rule_indexes: dict[str, set[int]],
    ) -> tuple[Any, int]:
        """Save a rule at the policy's lowest free index, retrying on an index collision.

        ``rule_indexes`` holds each policy's used indexes for the run, read
        from the server on first use. A collision means another run took the
        index: the policy is re-read, and a rule of this name that run made
        meanwhile is updated in place, keeping its index, expiry and disabled flag.
        """
        if policy_id not in rule_indexes:
            await self._read_policy_rules(policy_id, rule_indexes)
        payload = dict(rule_data)
        payload.setdefault("expires_at", _default_expiry_iso())
        index = _next_free_rule_index(rule_indexes[policy_id])
        existing_disabled = False

        attempt = 1
        while True:
            payload["index"] = index
            payload["disabled"] = bool(
                existing_disabled or rule_data.get("disabled", False) or _is_expired(payload["expires_at"])
            )
            try:
                rule = await self.client.create(kind=SecurityPolicyRule, data=payload)
                await rule.save(allow_upsert=True)
            except Exception as exc:
                if "policy-index" not in str(exc) or attempt == RULE_SAVE_ATTEMPTS:
                    raise
                attempt += 1
                self.logger.warning(
                    "Index collision for '%s', retrying with refreshed index (attempt %d/%d)",
                    rule_name,
                    attempt,
                    RULE_SAVE_ATTEMPTS,
                )
                rules = await self._read_policy_rules(policy_id, rule_indexes)
                existing = next((rule for rule in rules if rule.name.value == rule_name), None)
                if existing is not None and "id" not in payload and existing.index.value is not None:
                    payload["id"] = existing.id
                    index = int(existing.index.value)
                    if "expires_at" not in rule_data and existing.expires_at.value:
                        payload["expires_at"] = existing.expires_at.value
                    existing_disabled = bool(existing.disabled.value)
                else:
                    index = _next_free_rule_index(rule_indexes[policy_id])
                continue
            rule_indexes[policy_id].add(index)
            return rule, index

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

    async def _ensure_segment_isolation_mode(self, segment: dict[str, Any], app_security_profile: str) -> None:
        """Derive and set a segment's isolation_mode from its application's
        security_profile, unless the author already set something other than
        the schema default ("normal") — isolated/microsegmented are never a
        silent default, so either one signals a deliberate explicit choice to
        respect. Cloud segments have no isolation_mode (Managed*Segment only).
        """
        seg_id = segment.get("id")
        seg_typename = segment.get("typename", "ManagedVxlanSegment")
        if not seg_id or seg_typename == "CloudNetworkSegment":
            return

        current = segment.get("isolation_mode")
        derived = RulesPlanner.pick_isolation_mode(app_security_profile)
        if current and current != "normal":
            return
        if current == derived:
            return

        try:
            seg_obj = await self.client.create(kind=seg_typename, data={"id": seg_id, "isolation_mode": derived})
            await seg_obj.save(allow_upsert=True, update_group_context=False)
            self.logger.info("  Derived isolation_mode '%s' for segment %s", derived, segment.get("name", seg_id))
        except Exception as exc:
            self.logger.warning("  Could not set isolation_mode on segment %s: %s", segment.get("name", seg_id), exc)

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
