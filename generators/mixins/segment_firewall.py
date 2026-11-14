"""Mixin for segment-to-segment firewall rules (SecurityPolicy/SecurityPolicyRule), any segment kind."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from generators.helpers.rules import RulesPlanner
from generators.mixins.named_objects import GetOrCreateByNameMixin
from generators.protocols import SecurityPolicy, SecurityPolicyRule, SecuritySecurityProfile
from utils.limits import RULE_SAVE_ATTEMPTS
from utils.ports import PortSpec

if TYPE_CHECKING:
    import logging

    from generators.mixins.common import CommonGenerator

# Starting rule index; leaves room below 100 for hand-written high-priority rules
RULE_INDEX_START = 100
RULE_INDEX_STEP = 10
RULE_DEFAULT_VALIDITY_DAYS = 180
# The cardinality-many selectors a generated rule sets: R_client's VIPs, R_server's leg.
_SELECTOR_FIELDS = ("destination_ip_addresses", "source_prefixes")
# The zones that qualify those selectors: R_client's destination, R_server's source.
_ZONE_FIELDS = ("source_zone", "destination_zone")


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


class SegmentPolicyMixin(GetOrCreateByNameMixin):
    """The one default-deny SecurityPolicy of a segment; needs ``client`` and ``logger`` on the host."""

    client: Any
    logger: logging.Logger

    async def _segment_policy(self, segment: dict[str, Any], policy_name: str) -> Any | None:
        """The source segment's SecurityPolicy: its existing one, else the deterministic seg-<segment>-egress; untracked
        (shared by every application).
        """
        existing_id = (segment.get("security_policy") or {}).get("id")
        if existing_id:
            return SimpleNamespace(id=existing_id)
        return await self._get_or_create_by_name(
            kind=SecurityPolicy,
            name=policy_name,
            create_data={
                "name": policy_name,
                "description": f"Egress contract of segment {segment.get('name') or segment.get('id')}",
                "default_action": "deny",
                "enabled": True,
                "segment": {"id": segment["id"]},
            },
            found_log="Using existing policy: %s",
            created_log="Created policy: %s",
            track=False,
        )

    async def ensure_segment_policy(self, segment: dict[str, Any]) -> None:
        """Give the segment its policy, default action deny, when it has none (a hand-written one is kept)."""
        await self._segment_policy(segment, RulesPlanner.segment_policy_name(segment))


class SegmentFirewallMixin(SegmentPolicyMixin):
    """Segment-to-segment dependency rules (SecurityPolicy/SecurityPolicyRule) and threat-profile lookups. The return
    leg is built by the destination leaf ACL. Needs ``client``, ``logger`` and ``_safe_rel_add`` on the host.
    """

    client: Any
    logger: logging.Logger
    if TYPE_CHECKING:
        _safe_rel_add = staticmethod(CommonGenerator._safe_rel_add)

    # policy id -> {rule name: rule} as last read; valid only while the policy is in the caller's rule_indexes.
    _rules_by_policy: dict[str, dict[str, Any]] | None = None

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
        vip: tuple[str, str] | None = None,
    ) -> tuple[bool, bool]:
        """Segment-to-segment dependency rule for one edge and port; returns (created, skipped).
        ``segment_policies`` and ``rule_indexes`` are per-run caches; ``vip`` makes it R_client."""
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
            policy = await self._segment_policy(src_seg, planner.segment_policy_name(src_seg))
            if policy is None:
                return False, True
            segment_policies[src_seg_id] = policy

        policy_id = policy.id
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
            vip_id=vip[1] if vip else None,
        )

        profile_name = planner.pick_profile_name(app_security_profile, cross_zone)
        profile = await self._get_profile(profile_name) if profile_name else None
        if profile:
            rule_data["security_profile"] = {"id": profile.id}

        # A profile that is named but not in the database is left as it is.
        assigned_index = await self._upsert_policy_rule(
            policy_id=policy_id,
            rule_name=rule_name,
            rule_data=rule_data,
            rule_indexes=rule_indexes,
            keep_profile=bool(profile_name) and profile is None,
        )
        if assigned_index is None:
            return False, True
        self.logger.info(
            "  Created rule [%d] '%s' (%s -> %s, %s/%s)",
            assigned_index,
            rule_name,
            src_seg.get("name", src_seg_id),
            f"VIP on {vip[0]}" if vip else dst_seg.get("name", dst_seg_id),
            protocol,
            port_start or "any",
        )
        return True, False

    async def _upsert_policy_rule(
        self,
        *,
        policy_id: str,
        rule_name: str,
        rule_data: dict[str, Any],
        rule_indexes: dict[str, set[int]],
        keep_profile: bool = False,
    ) -> int | None:
        """Create the rule at the next free index, or update the existing one; returns the new index, None if it
        existed. Shared by dependency rules and a load balancer context's R_server; a racing add is caught by the upsert
        or the index-collision re-read.
        """
        existing_rule = await self._find_policy_rule(policy_id, rule_name, rule_indexes)
        if existing_rule is not None:
            existing_index = existing_rule.index.value
            if existing_index is not None and int(existing_index) < RULE_INDEX_START:
                # A hand-written rule (index < 100) of this name is never adopted into the application's group
                self.logger.error(
                    "  Rule '%s' is a hand-written rule (index %s) of policy %s — rename one of them",
                    rule_name,
                    existing_index,
                    policy_id,
                )
                return None
            self.logger.info("  Rule '%s' already exists - registering with tracker", rule_name)
            changed = self._reconcile_rule_payload(existing_rule, rule_data, keep_profile=keep_profile)
            if changed:
                self.logger.info("  Rule '%s' updated: %s", rule_name, ", ".join(changed))
            if _is_expired(existing_rule.expires_at.value) and not existing_rule.disabled.value:
                existing_rule.disabled.value = True
                self.logger.info("  Rule '%s' expired — disabling it", rule_name)
            # Saved every run to keep the rule in this application's group
            await existing_rule.save(allow_upsert=True)
            return None

        # A failure propagates: the caller counts it and fails the run after the loop.
        _rule, assigned_index = await self._create_or_update_policy_rule(
            policy_id=policy_id,
            rule_name=rule_name,
            rule_data=rule_data,
            rule_indexes=rule_indexes,
        )
        return assigned_index

    @staticmethod
    def _reconcile_rule_payload(rule: Any, rule_data: dict[str, Any], *, keep_profile: bool) -> list[str]:
        """Set the derived fields of an existing rule to what the planner derives; return the changed names.
        A selector is reconciled only when the payload has it."""
        changed: list[str] = []
        for field in ("log", "apply_on_switch"):
            attribute = getattr(rule, field)
            if attribute.value != rule_data[field]:
                attribute.value = rule_data[field]
                changed.append(field)
        relationships = ("destination_segment", "source_security_tag", "destination_security_tag")
        for field in relationships if keep_profile else ("security_profile", *relationships):
            wanted = (rule_data.get(field) or {}).get("id")
            peer = getattr(rule, field)
            current = peer.id if peer.initialized else None
            if current != wanted:
                setattr(rule, field, {"id": wanted} if wanted else None)
                changed.append(field)
        # Zone selectors are written by HFID (rules.py zone_ref), so compare by HFID.
        for field in _ZONE_FIELDS:
            wanted_hfid = (rule_data.get(field) or {}).get("hfid")
            peer = getattr(rule, field)
            current_hfid = (list(peer.hfid or []) or None) if peer.initialized else None
            if current_hfid != wanted_hfid:
                setattr(rule, field, {"hfid": wanted_hfid} if wanted_hfid else None)
                changed.append(field)
        for field in _SELECTOR_FIELDS:
            if field not in rule_data:
                continue
            wanted_ids = {peer["id"] for peer in rule_data[field]}
            manager = getattr(rule, field)
            current_ids = set(manager.peer_ids)
            if current_ids != wanted_ids:
                for peer_id in sorted(current_ids - wanted_ids):
                    manager.remove(peer_id)
                manager.extend(sorted(wanted_ids - current_ids))
                changed.append(field)
        return changed

    async def _read_policy_rules(self, policy_id: str, rule_indexes: dict[str, set[int]]) -> list[Any]:
        """List the policy's rules with selectors; record their indexes in ``rule_indexes`` and names for
        _find_policy_rule.
        """
        rules = await self.client.filters(
            kind=SecurityPolicyRule, policy__ids=[policy_id], include=list(_SELECTOR_FIELDS)
        )
        rule_indexes[policy_id] = {int(rule.index.value) for rule in rules if rule.index.value is not None}
        if self._rules_by_policy is None:
            self._rules_by_policy = {}
        self._rules_by_policy[policy_id] = {rule.name.value: rule for rule in rules}
        return rules

    async def _find_policy_rule(self, policy_id: str, rule_name: str, rule_indexes: dict[str, set[int]]) -> Any | None:
        """The policy's rule named ``rule_name``, or None (one read of the rules per run)."""
        if policy_id not in rule_indexes or policy_id not in (self._rules_by_policy or {}):
            await self._read_policy_rules(policy_id, rule_indexes)
        return (self._rules_by_policy or {}).get(policy_id, {}).get(rule_name)

    async def _create_or_update_policy_rule(
        self,
        policy_id: str,
        rule_name: str,
        rule_data: dict[str, Any],
        rule_indexes: dict[str, set[int]],
    ) -> tuple[Any, int]:
        """Save a rule at the lowest free index, retrying on collision (re-read; a same-named rule is updated in place)."""
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
            (self._rules_by_policy or {}).get(policy_id, {})[rule_name] = rule
            return rule, index

    async def _get_profile(self, profile_name: str) -> Any | None:
        """The SecuritySecurityProfile named ``profile_name``, cached per run; never created, a miss is cached as None."""
        cache: dict[str, Any] = getattr(self, "_profile_cache", {})
        if profile_name not in cache:
            try:
                found = await self.client.filters(kind=SecuritySecurityProfile, name__value=profile_name)
                cache[profile_name] = found[0] if found else None
            except Exception:
                cache[profile_name] = None
            self._profile_cache = cache
        return cache[profile_name]

    async def _ensure_segment_isolation_mode(self, segment: dict[str, Any], app_security_profile: str) -> None:
        """Derive a segment's isolation_mode from its application's security_profile; an author-set non-default is kept,
        cloud segments skipped.
        """
        seg_id = segment.get("id")
        seg_typename = segment.get("typename")
        if not seg_id or not seg_typename or seg_typename == "CloudNetworkSegment":
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
            return
        # Recorded in the dict so this run's tags and rules use the mode just written
        segment["isolation_mode"] = derived
