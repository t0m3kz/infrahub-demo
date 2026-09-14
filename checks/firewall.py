"""Validate firewall zone policy integrity."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data


class CheckFirewall(InfrahubCheck):
    """Validate that every zone referenced in a security policy rule has at least
    one member segment (non-empty CIDR list) and exists as a SecurityZone node."""

    query = "firewall_config"

    @staticmethod
    def _rel_name(value: Any) -> str | None:
        if isinstance(value, dict):
            return value.get("name")
        if isinstance(value, str) and value:
            return value
        return None

    @staticmethod
    def _rel_id(value: Any) -> str | None:
        if isinstance(value, dict):
            rel_id = value.get("id")
            if rel_id:
                return str(rel_id)
        return None

    @staticmethod
    def _rel_list_count(value: Any) -> int:
        if isinstance(value, list):
            return len(value)
        return 0

    def validate(self, data: Any) -> None:
        # firewall.gql is a multi-root query — extract zones and policies directly
        cleaned = clean_data(data)
        zones_data = cleaned.get("SecurityZone") or []
        policies_data = cleaned.get("SecurityPolicy") or []
        tag_rules_data = cleaned.get("SecurityTagRule") or []

        # Build zone → member CIDRs index
        zone_cidrs: dict[str, list[str]] = {}
        for zone in zones_data:
            name = zone.get("name")
            if not name:
                continue
            cidrs: list[str] = [
                prefix
                for seg in (zone.get("network_segments") or [])
                if (prefix := (seg.get("prefix") or {}).get("prefix"))
            ]
            zone_cidrs[name] = cidrs

        tag_contracts: set[tuple[str, str]] = set()
        for tag_rule in tag_rules_data:
            src_tag = self._rel_id(tag_rule.get("source_tag"))
            dst_tag = self._rel_id(tag_rule.get("destination_tag"))
            if src_tag and dst_tag:
                tag_contracts.add((src_tag, dst_tag))

        # Validate each enabled policy rule's zone references
        for policy in policies_data:
            if not policy.get("enabled", True):
                continue
            policy_name = policy.get("name", "<unnamed>")
            for rule in policy.get("rules") or []:
                if rule.get("disabled"):
                    continue
                rule_name = rule.get("name", "<unnamed>")
                for field in ("source_zone", "destination_zone"):
                    zone_ref = rule.get(field) or {}
                    zone_name = zone_ref.get("name") if isinstance(zone_ref, dict) else zone_ref
                    if not zone_name:
                        continue
                    if zone_name not in zone_cidrs:
                        self.log_error(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}': "
                                f"{field} '{zone_name}' references a non-existent SecurityZone"
                            )
                        )
                    elif not zone_cidrs[zone_name]:
                        self.log_info(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}': "
                                f"{field} '{zone_name}' has no member segments — zone CIDRs will be empty"
                            )
                        )

                src_zone_name = self._rel_name(rule.get("source_zone"))
                dst_zone_name = self._rel_name(rule.get("destination_zone"))
                src_seg = rule.get("source_segment") or {}
                dst_seg = rule.get("destination_segment") or {}
                src_seg_zone_name = self._rel_name(src_seg.get("security_zone"))
                dst_seg_zone_name = self._rel_name(dst_seg.get("security_zone"))

                if src_zone_name and src_seg_zone_name and src_zone_name != src_seg_zone_name:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}': source_zone '{src_zone_name}' "
                            f"does not match source_segment zone '{src_seg_zone_name}'"
                        )
                    )
                if dst_zone_name and dst_seg_zone_name and dst_zone_name != dst_seg_zone_name:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}': destination_zone '{dst_zone_name}' "
                            f"does not match destination_segment zone '{dst_seg_zone_name}'"
                        )
                    )

                src_selectors = 0
                if src_zone_name:
                    src_selectors += 1
                if self._rel_id(src_seg):
                    src_selectors += 1
                src_selectors += self._rel_list_count(rule.get("source_ip_addresses"))
                src_selectors += self._rel_list_count(rule.get("source_prefixes"))

                dst_selectors = 0
                if dst_zone_name:
                    dst_selectors += 1
                if self._rel_id(dst_seg):
                    dst_selectors += 1
                dst_selectors += self._rel_list_count(rule.get("destination_ip_addresses"))
                dst_selectors += self._rel_list_count(rule.get("destination_prefixes"))

                if src_selectors == 0:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}' has no source selector "
                            "(zone, segment, IP, or prefix)"
                        )
                    )
                if dst_selectors == 0:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}' has no destination selector "
                            "(zone, segment, IP, or prefix)"
                        )
                    )

                src_tag = self._rel_id(src_seg.get("security_tag"))
                dst_tag = self._rel_id(dst_seg.get("security_tag"))
                if src_tag and dst_tag and (src_tag, dst_tag) not in tag_contracts:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}' uses segment tags without "
                            "a matching SecurityTagRule contract"
                        )
                    )
