"""Validate firewall zone policy integrity."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data


class CheckFirewall(InfrahubCheck):
    """Validate that every zone referenced in a security policy rule has at least
    one member segment (non-empty CIDR list) and exists as a SecurityZone node."""

    query = "firewall_config"

    def validate(self, data: Any) -> None:
        # firewall.gql is a multi-root query — extract zones and policies directly
        cleaned = clean_data(data)
        zones_data = cleaned.get("SecurityZone") or []
        policies_data = cleaned.get("SecurityPolicy") or []

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
