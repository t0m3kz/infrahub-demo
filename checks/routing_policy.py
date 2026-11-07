"""Validate the routing policy objects (rules RP1 to RP3).

The schema accepts any text in a list and any prefix in an entry; these rules hold what it cannot.

RP1  a community of a community list is a community of the list's type (standard: ASN:value or a well-known one;
     large: ASN:value:value)
RP2  a prefix list entry is a prefix of the family of its list, and its length range lies between its own length and the
     size of the address, minimum not above maximum
RP3  a community a policy term adds is a standard, well-known or large community
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.checks import InfrahubCheck

from utils.data_cleaning import clean_data
from utils.routing_policy import any_community_error, community_error, prefix_entry_errors


def routing_policy_errors(cleaned: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(message, object id, kind) for each broken object of one cleaned answer of ``routing_policy.gql``."""
    errors: list[tuple[str, str, str]] = []
    for plist in cleaned.get("RoutingPrefixList") or []:
        for entry in plist.get("entries") or []:
            for problem in prefix_entry_errors(entry, str(plist.get("family"))):
                errors.append(
                    (
                        f"RP2: prefix list '{plist.get('name')}' entry {entry.get('seq')}: {problem}.",
                        entry["id"],
                        "RoutingPrefixListEntry",
                    )
                )
    for clist in cleaned.get("RoutingCommunityList") or []:
        for member in clist.get("members") or []:
            if problem := community_error(str(member), str(clist.get("community_type") or "standard")):
                errors.append(
                    (f"RP1: community list '{clist.get('name')}': {problem}.", clist["id"], "RoutingCommunityList")
                )
    for policy in cleaned.get("RoutingPolicy") or []:
        for term in policy.get("terms") or []:
            for community in term.get("add_communities") or []:
                if problem := any_community_error(str(community)):
                    errors.append(
                        (
                            f"RP3: policy '{policy.get('name')}' term {term.get('seq')} adds {problem}.",
                            term["id"],
                            "RoutingPolicyTerm",
                        )
                    )
    return errors


class CheckRoutingPolicy(InfrahubCheck):
    """The communities and prefixes of the routing policy objects are well formed (rules RP1 to RP3)."""

    query = "routing_policy"

    def validate(self, data: Any) -> None:
        for message, object_id, object_type in routing_policy_errors(clean_data(data)):
            self.log_error(message=message, object_id=object_id, object_type=object_type)
