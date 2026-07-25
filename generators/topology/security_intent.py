"""Generator: render SecurityIntentRule into enforceable policy objects.

Materializes one SecurityPolicyRule per SecurityIntentRule and, when label
selectors are present, also reconciles a SecurityTagRule.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator

RULE_INDEX_STEP = 10


class SecurityIntentRuleGenerator(CommonGenerator):
    """Render one SecurityIntentRule into concrete policy/tag rules."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        intents = cleaned.get("SecurityIntentRule", [])
        if not intents:
            self.logger.error("No SecurityIntentRule data in GraphQL response")
            return

        intent = intents[0]
        await self._render_intent_rule(intent)

    async def _render_intent_rule(self, intent: dict[str, Any]) -> None:
        intent_id = intent.get("id", "")
        intent_name = intent.get("name", "")
        mode = intent.get("match_mode", "ip_exact")

        policy = intent.get("policy") or {}
        policy_id = policy.get("id")
        if not policy_id:
            self.logger.error("SecurityIntentRule '%s' has no policy relationship", intent_name)
            return

        priority = int(intent.get("priority", 100))
        action = intent.get("action", "permit")
        protocol = intent.get("protocol", "any")
        port_start = intent.get("port_start")
        port_end = intent.get("port_end")
        log = bool(intent.get("log", False))
        enabled = bool(intent.get("enabled", True))
        description = intent.get("description") or f"Rendered from SecurityIntentRule {intent_name}"

        src_zone = intent.get("source_zone") or {}
        dst_zone = intent.get("destination_zone") or {}
        src_tag = intent.get("source_tag") or {}
        dst_tag = intent.get("destination_tag") or {}

        src_segments = intent.get("source_segments") or []
        dst_segments = intent.get("destination_segments") or []
        src_ips = intent.get("source_ip_addresses") or []
        dst_ips = intent.get("destination_ip_addresses") or []
        src_prefixes = intent.get("source_prefixes") or []
        dst_prefixes = intent.get("destination_prefixes") or []

        # Reconcile SecurityPolicyRule
        existing_rules = await self.client.filters(kind="SecurityPolicyRule", rendered_from_intent__ids=[intent_id])
        existing_rule = existing_rules[0] if existing_rules else None

        if existing_rule:
            rule_index = int(getattr(existing_rule, "index").value or priority)
        else:
            used = set()
            all_policy_rules = await self.client.filters(kind="SecurityPolicyRule", policy__ids=[policy_id])
            for rule in all_policy_rules:
                idx_attr = getattr(rule, "index", None)
                if idx_attr and idx_attr.value is not None:
                    used.add(int(idx_attr.value))

            rule_index = priority
            while rule_index in used:
                rule_index += RULE_INDEX_STEP

        rule_data: dict[str, Any] = {
            "name": intent_name,
            "policy": {"id": policy_id},
            "index": rule_index,
            "action": action,
            "protocol": protocol,
            "log": log,
            "disabled": not enabled,
            "description": description,
            "rendered_from_intent": {"id": intent_id},
            "apply_on_switch": mode in {"segment", "hybrid"},
        }

        if port_start is not None:
            rule_data["port_start"] = int(port_start)
        if port_end is not None:
            rule_data["port_end"] = int(port_end)

        if src_zone.get("id"):
            rule_data["source_zone"] = {"id": src_zone["id"]}
        if dst_zone.get("id"):
            rule_data["destination_zone"] = {"id": dst_zone["id"]}

        # Keep backward-compatible selectors used by existing transforms.
        if src_segments:
            rule_data["source_segment"] = {"id": src_segments[0]["id"]}
        if dst_segments:
            rule_data["destination_segment"] = {"id": dst_segments[0]["id"]}

        # New IP/prefix selectors (many-to-many) for IP-first model.
        if src_ips:
            rule_data["source_ip_addresses"] = [{"id": ip["id"]} for ip in src_ips if ip.get("id")]
        if dst_ips:
            rule_data["destination_ip_addresses"] = [{"id": ip["id"]} for ip in dst_ips if ip.get("id")]
        if src_prefixes:
            rule_data["source_prefixes"] = [{"id": pfx["id"]} for pfx in src_prefixes if pfx.get("id")]
        if dst_prefixes:
            rule_data["destination_prefixes"] = [{"id": pfx["id"]} for pfx in dst_prefixes if pfx.get("id")]

        # Fallback: derive prefixes from IP addresses when no explicit prefixes are set.
        if "source_prefixes" not in rule_data and src_ips:
            src_pfx_ids = [ip.get("ip_prefix", {}).get("id") for ip in src_ips if (ip.get("ip_prefix") or {}).get("id")]
            if src_pfx_ids:
                rule_data["source_prefixes"] = [{"id": pfx_id} for pfx_id in sorted(set(src_pfx_ids))]

        if "destination_prefixes" not in rule_data and dst_ips:
            dst_pfx_ids = [ip.get("ip_prefix", {}).get("id") for ip in dst_ips if (ip.get("ip_prefix") or {}).get("id")]
            if dst_pfx_ids:
                rule_data["destination_prefixes"] = [{"id": pfx_id} for pfx_id in sorted(set(dst_pfx_ids))]

        security_profile = intent.get("security_profile") or {}
        if security_profile.get("id"):
            rule_data["security_profile"] = {"id": security_profile["id"]}

        try:
            rule_obj = await self.client.create(kind="SecurityPolicyRule", data=rule_data)
            await rule_obj.save(allow_upsert=True)
            self.logger.info(
                "Rendered SecurityPolicyRule '%s' [%d] from intent '%s'",
                intent_name,
                rule_index,
                intent_name,
            )
        except Exception as exc:
            self.logger.error("Failed to render SecurityPolicyRule from intent '%s': %s", intent_name, exc)
            return

        # Reconcile label-level SecurityTagRule when both tags are present.
        if src_tag.get("id") and dst_tag.get("id"):
            tag_rule_data: dict[str, Any] = {
                "source_tag": {"id": src_tag["id"]},
                "destination_tag": {"id": dst_tag["id"]},
                "action": action if action in {"permit", "deny"} else "permit",
                "log": log,
                "description": f"Rendered from SecurityIntentRule {intent_name}",
            }
            try:
                tag_rule = await self.client.create(kind="SecurityTagRule", data=tag_rule_data)
                await tag_rule.save(allow_upsert=True)
                self.logger.info(
                    "Rendered SecurityTagRule %s -> %s from intent '%s'",
                    src_tag.get("name", "source"),
                    dst_tag.get("name", "destination"),
                    intent_name,
                )
            except Exception as exc:
                self.logger.error("Failed to render SecurityTagRule from intent '%s': %s", intent_name, exc)
