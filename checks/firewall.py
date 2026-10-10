"""Validate the policy rules a firewall enforces."""

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from transforms.config.firewall import Firewall

from .common import validate_exchange_gateways


class CheckFirewall(InfrahubCheck):
    """Validate the rules this firewall gets — the same ones its config renders
    (Firewall.collect_policies: its served segments' policies and the rules
    into them, per context). Every rule lives in its source segment's policy,
    so a rule whose source is another segment is an error, as is a rule with
    no destination selector (segment, prefix or IP). A rule's zone is its
    segment's security_zone; a segment without one is reported, since
    zone-based firewalls then match any zone on that side. Its exchange legs
    (checks/common.py validate_exchange_gateways) are validated too."""

    query = "firewall_config"

    def validate(self, data: Any) -> None:
        device, _, _ = Firewall._device_and_platform(data)
        for error in validate_exchange_gateways(device):
            self.log_error(message=error)
        # collect_policies only reads class attributes, no client: the check
        # validates exactly what the transform places, not a second copy.
        _, _, root, by_context = Firewall.__new__(Firewall).collect_policies(device)

        seen: set[str] = set()
        for policy in [*root, *(policy for policies in by_context.values() for policy in policies)]:
            if not policy.get("enabled", True):
                continue
            policy_name = policy.get("name", "<unnamed>")
            policy_segment = (policy.get("segment") or {}).get("id")
            for rule in policy.get("rules") or []:
                rule_id = str(rule.get("id") or (policy_name, rule.get("name")))
                if rule.get("disabled") or rule_id in seen:
                    continue
                seen.add(rule_id)
                self._validate_rule(rule, policy_name, policy_segment)

    def _validate_rule(self, rule: dict[str, Any], policy_name: str, policy_segment: str | None) -> None:
        rule_name = rule.get("name", "<unnamed>")
        source = rule.get("source_segment") or {}
        if not policy_segment or source.get("id") != policy_segment:
            self.log_error(
                message=(
                    f"Policy '{policy_name}' rule '{rule_name}': source segment "
                    f"'{source.get('name') or source.get('id') or '<none>'}' is not the policy's segment — "
                    "a rule belongs in its source segment's policy"
                )
            )
        destination = rule.get("destination_segment") or {}
        if not (destination.get("id") or rule.get("destination_prefixes") or rule.get("destination_ip_addresses")):
            self.log_error(
                message=(
                    f"Policy '{policy_name}' rule '{rule_name}' has no destination selector (segment, IP, or prefix)"
                )
            )
        for side, segment in (("source", source), ("destination", destination)):
            if segment.get("id") and not segment.get("security_zone"):
                self.log_info(
                    message=(
                        f"Policy '{policy_name}' rule '{rule_name}': {side}_segment "
                        f"'{segment.get('name', segment.get('id'))}' has no security_zone — zone-based "
                        "firewalls match any zone on that side"
                    )
                )
