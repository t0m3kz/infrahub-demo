"""Validate firewall policy rule integrity."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data


class CheckFirewall(InfrahubCheck):
    """Validate that every enabled security policy rule has a selector on both
    sides and that segment tags it relies on have a SecurityTagRule contract.
    A rule's zone is its segment's security_zone; a segment without one is
    reported, since zone-based firewalls then match any zone on that side."""

    query = "firewall_config"

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
        # firewall.gql is a multi-root query — extract policies directly
        cleaned = clean_data(data)
        policies_data = cleaned.get("SecurityPolicy") or []
        tag_rules_data = cleaned.get("SecurityTagRule") or []

        tag_contracts: set[tuple[str, str]] = set()
        for tag_rule in tag_rules_data:
            src_tag = self._rel_id(tag_rule.get("source_tag"))
            dst_tag = self._rel_id(tag_rule.get("destination_tag"))
            if src_tag and dst_tag:
                tag_contracts.add((src_tag, dst_tag))

        for policy in policies_data:
            if not policy.get("enabled", True):
                continue
            policy_name = policy.get("name", "<unnamed>")
            for rule in policy.get("rules") or []:
                if rule.get("disabled"):
                    continue
                rule_name = rule.get("name", "<unnamed>")
                for side in ("source", "destination"):
                    seg = rule.get(f"{side}_segment") or {}
                    selectors = (
                        bool(self._rel_id(seg))
                        + self._rel_list_count(rule.get(f"{side}_ip_addresses"))
                        + self._rel_list_count(rule.get(f"{side}_prefixes"))
                    )
                    if selectors == 0:
                        self.log_error(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}' has no {side} selector "
                                "(segment, IP, or prefix)"
                            )
                        )
                    elif self._rel_id(seg) and not seg.get("security_zone"):
                        self.log_info(
                            message=(
                                f"Policy '{policy_name}' rule '{rule_name}': {side}_segment "
                                f"'{seg.get('name', seg.get('id'))}' has no security_zone — zone-based "
                                "firewalls match any zone on that side"
                            )
                        )

                src_seg = rule.get("source_segment") or {}
                dst_seg = rule.get("destination_segment") or {}
                src_tag = self._rel_id(src_seg.get("security_tag"))
                dst_tag = self._rel_id(dst_seg.get("security_tag"))
                if src_tag and dst_tag and (src_tag, dst_tag) not in tag_contracts:
                    self.log_error(
                        message=(
                            f"Policy '{policy_name}' rule '{rule_name}' uses segment tags without "
                            "a matching SecurityTagRule contract"
                        )
                    )
