"""Transform: an office's segments and their rules -> the stateful firewall policy of its SD-WAN edge."""

from __future__ import annotations

import json
from typing import Any

from infrahub_sdk.transforms import InfrahubTransform

from transforms.helpers.address_table import build_address_table
from transforms.helpers.firewall import get_zone_policies
from transforms.helpers.policy import enabled_policies, segment_policies, segment_rule_policies
from utils.data_cleaning import clean_data
from utils.segment_addressing import own_prefix

OFFICE_KIND = "TopologyCustomerOffice"


def _zone(segment: dict[str, Any]) -> str | None:
    return (segment.get("security_zone") or {}).get("name")


def _default_action(segment: dict[str, Any]) -> str:
    """The closing action of the segment's own egress: permit when an enabled policy says so, else deny."""
    policies = enabled_policies(segment_policies(segment))
    return "permit" if any(policy.get("default_action") == "permit" for policy in policies) else "deny"


def build_office_policy(office: dict[str, Any]) -> dict[str, Any]:
    """The firewall policy payload of one cleaned office row (``office_firewall_policy.gql``)."""
    segments = [segment for segment in office.get("network_segments") or [] if isinstance(segment, dict)]
    rules = get_zone_policies(segment_rule_policies(segments))
    zones = sorted(
        {zone for segment in segments if (zone := _zone(segment))}
        | {
            zone
            for policy in rules
            for rule in policy["rules"]
            for zone in (rule.get("src_zone"), rule.get("dst_zone"))
            if zone
        }
    )
    return {
        "office": office.get("name"),
        "owner": (office.get("owner") or {}).get("org_id"),
        "zones": zones,
        # The prefixes the rules name, once, family-agnostic (the same table the firewall templates render).
        "addresses": build_address_table([], rules),
        "segments": [
            {
                "name": segment.get("name"),
                "zone": _zone(segment),
                "prefix": own_prefix(segment),
                "default_action": _default_action(segment),
            }
            for segment in sorted(segments, key=lambda s: str(s.get("name")))
        ],
        "policies": rules,
    }


class OfficeFirewallPolicy(InfrahubTransform):
    """The stateful firewall policy of an office's SD-WAN edge, as JSON."""

    query = "office_firewall_policy"

    async def transform(self, data: Any) -> str:
        offices = clean_data(data).get(OFFICE_KIND) or []
        if not offices:
            return ""
        return json.dumps(build_office_policy(offices[0]), indent=2, sort_keys=True)
