"""Validate the routing policy objects (rules RP1 to RP6).

The schema accepts any text in a list and any prefix in an entry; these rules hold what it cannot.

RP1  a community of a community list is a community of the list's type (standard: ASN:value or a well-known one;
     large: ASN:value:value)
RP2  a prefix list entry is a prefix of the family of its list, and its length range lies between its own length and the
     size of the address, minimum not above maximum
RP3  a community a policy term adds is a standard, well-known or large community
RP4  a term after a term that matches every route is never reached (an ERROR: the policy does not do what it reads)
RP5  a policy that no peering imports or exports (bgp), or no interface applies (pbr), applies to nothing (WARNING)
RP6  a prefix list or community list that no term names is not used (WARNING)

The guard derived from the model is not a rule here: it is added to every user map by the transform, enforced or not
(`transforms/helpers/routing_policy.py`), so a policy cannot lack it.
"""

from __future__ import annotations

from typing import Any

from checks.base import WarningCheck
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
    for policy in cleaned.get("RoutingPolicy") or []:
        catch_all: Any = None
        for term in sorted(policy.get("terms") or [], key=lambda t: t.get("seq") or 0):
            if catch_all is not None:
                errors.append(
                    (
                        f"RP4: policy '{policy.get('name')}' term {term.get('seq')} is never reached: term "
                        f"{catch_all.get('seq')} matches every route.",
                        term["id"],
                        "RoutingPolicyTerm",
                    )
                )
            elif not term.get("match_prefix_list") and not term.get("match_community_list"):
                catch_all = term
    return errors


def routing_policy_warnings(cleaned: dict[str, Any]) -> list[str]:
    """The unused policies (RP5) and lists (RP6) of one cleaned answer of ``routing_policy.gql``."""
    warnings: list[str] = []
    used_prefix: set[str] = set()
    used_community: set[str] = set()
    for policy in cleaned.get("RoutingPolicy") or []:
        if policy.get("kind") == "pbr":
            if not policy.get("interfaces"):
                warnings.append(f"RP5: policy '{policy.get('name')}' is applied on no interface.")
        elif not policy.get("import_peerings") and not policy.get("export_peerings"):
            warnings.append(f"RP5: policy '{policy.get('name')}' is attached to no peering.")
        for term in policy.get("terms") or []:
            if term.get("match_prefix_list"):
                used_prefix.add(term["match_prefix_list"]["id"])
            for key in ("match_community_list", "delete_community_list"):
                if term.get(key):
                    used_community.add(term[key]["id"])
    for kind, used in (("RoutingPrefixList", used_prefix), ("RoutingCommunityList", used_community)):
        warnings.extend(
            f"RP6: {'prefix' if kind == 'RoutingPrefixList' else 'community'} list '{item.get('name')}' is named by no term."
            for item in cleaned.get(kind) or []
            if item["id"] not in used
        )
    return warnings


class CheckRoutingPolicy(WarningCheck):
    """The routing policy objects are well formed and used (rules RP1 to RP6)."""

    query = "routing_policy"

    def validate(self, data: Any) -> None:
        cleaned = clean_data(data)
        for message, object_id, object_type in routing_policy_errors(cleaned):
            self.log_error(message=message, object_id=object_id, object_type=object_type)
        for message in routing_policy_warnings(cleaned):
            self.log_warning(message=message)
