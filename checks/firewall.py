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
                sides = [
                    (side, self._rel_name(rule.get(f"{side}_zone")), rule.get(f"{side}_segment") or {})
                    for side in ("source", "destination")
                ]
                for side, zone_name, _seg in sides:
                    if not zone_name:
                        continue
                    if zone_name not in zone_cidrs:
                        self.log_error(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}': "
                                f"{side}_zone '{zone_name}' references a non-existent SecurityZone"
                            )
                        )
                    elif not zone_cidrs[zone_name]:
                        self.log_info(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}': "
                                f"{side}_zone '{zone_name}' has no member segments — zone CIDRs will be empty"
                            )
                        )

                for side, zone_name, seg in sides:
                    seg_zone_name = self._rel_name(seg.get("security_zone"))
                    if zone_name and seg_zone_name and zone_name != seg_zone_name:
                        self.log_error(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}': {side}_zone '{zone_name}' "
                                f"does not match {side}_segment zone '{seg_zone_name}'"
                            )
                        )

                for side, zone_name, seg in sides:
                    selectors = (
                        bool(zone_name)
                        + bool(self._rel_id(seg))
                        + self._rel_list_count(rule.get(f"{side}_ip_addresses"))
                        + self._rel_list_count(rule.get(f"{side}_prefixes"))
                    )
                    if selectors == 0:
                        self.log_error(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}' has no {side} selector "
                                "(zone, segment, IP, or prefix)"
                            )
                        )

                src_seg, dst_seg = sides[0][2], sides[1][2]
                src_tag = self._rel_id(src_seg.get("security_tag"))
                dst_tag = self._rel_id(dst_seg.get("security_tag"))
                if src_tag and dst_tag and (src_tag, dst_tag) not in tag_contracts:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}' uses segment tags without "
                            "a matching SecurityTagRule contract"
                        )
                    )
