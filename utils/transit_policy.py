"""The routing policies of an upstream transit session, derived once (the generator writes, a test compares).

Default route in only, public INTERNET space out, tagged with ``TRANSIT_COMMUNITY`` at lower local-pref.
"""

from __future__ import annotations

from typing import Any

# RFC 5398 documentation ASN, next after utils/routing_identity.py.
TRANSIT_COMMUNITY_ASN = 64498
TRANSIT_COMMUNITY = f"{TRANSIT_COMMUNITY_ASN}:1"
TRANSIT_LOCAL_PREF = 80

DEFAULT_IN_LIST = "TRANSIT-DEFAULT-IN"
PUBLIC_OUT_LIST = "TRANSIT-OUT-PUBLIC"
LEARNED_LIST = "CL-LEARNED-FROM-TRANSIT"
IN_POLICY = "transit-in"
OUT_POLICY = "transit-out"


def _entries(prefixes: list[str]) -> list[dict[str, Any]]:
    return [
        {"seq": 10 * (index + 1), "action": "permit", "prefix": prefix, "min_length": None, "max_length": None}
        for index, prefix in enumerate(prefixes)
    ]


def transit_spec(public_prefixes: list[str]) -> dict[str, list[dict[str, Any]]]:
    """The prefix lists, community list and two policies of a transit session; with no public prefixes nothing is
    announced.
    """
    prefix_lists = [{"name": DEFAULT_IN_LIST, "family": "ipv4", "entries": _entries(["0.0.0.0/0"])}]
    out_terms: list[dict[str, Any]] = [{"seq": 10, "action": "deny", "community_list": LEARNED_LIST}]
    if public_prefixes:
        prefix_lists.append({"name": PUBLIC_OUT_LIST, "family": "ipv4", "entries": _entries(public_prefixes)})
        out_terms.append({"seq": 20, "action": "permit", "prefix_list": PUBLIC_OUT_LIST})
    return {
        "prefix_lists": prefix_lists,
        "community_lists": [
            {"name": LEARNED_LIST, "community_type": "standard", "members": [TRANSIT_COMMUNITY], "match": "any"}
        ],
        "policies": [
            {
                "name": IN_POLICY,
                "default_action": "deny",
                "terms": [
                    {
                        "seq": 10,
                        "action": "permit",
                        "prefix_list": DEFAULT_IN_LIST,
                        "local_pref": TRANSIT_LOCAL_PREF,
                        "add_communities": [TRANSIT_COMMUNITY],
                    }
                ],
            },
            {"name": OUT_POLICY, "default_action": "deny", "terms": out_terms},
        ],
    }
