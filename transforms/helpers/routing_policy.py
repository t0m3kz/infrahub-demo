"""Routing policies of a BGP session, composed with the guard derived from the model (neutral, no vendor syntax).

A person or a generator attaches ``RoutingPolicy`` objects to a peering, one set per direction. This module turns them
into the maps the templates render, under one rule that no policy can break: **a policy sets and tightens, it never
widens.** The prefix filter derived by ``utils/bgp_expectation.py`` (``session["policy"]["filters"]``) is the guard of
its direction and family. The first term of a user map denies everything OUTSIDE the guard, so the terms after it only
ever see a route the model allows (a route-map is evaluated in order; two matches of one type in a term are an OR on
every platform, so the guard cannot be a second match of a term).

``normalize_policies`` reads the cleaned ``import_policies`` / ``export_policies`` of a peering. ``compose_user_maps``
makes, per direction, one map per address family that has a guard (a map names one address family's lists), or a
single map when the direction has none:

- when there is a guard, the term ``outside_guard`` (deny) first;
- the terms of the attached policies in order (priority, name, seq), renumbered in the map;
- a permit term inherits the session's own sets (local-preference inbound, MED outbound, no-export) unless it sets its own;
- a closing term, a permit with the session's sets, when the policies' combined default is ``permit`` (``deny`` as soon
  as one policy says so, then nothing: a route-map ends in an implicit deny).

A term whose prefix list is of another family than the map's is left out of that map; a term with no prefix list
belongs to every family map. A direction that already names a map a template defines (``RM-FABRIC-OUT``, ``RM-DCI-IN``:
the internal fabric) cannot name a second one, and an EVPN-only session has no policy match on any platform, so their policies are
reported in ``skipped`` and not composed.
"""

from __future__ import annotations

from typing import Any

IN, OUT = "in", "out"
PERMIT, DENY = "permit", "deny"
SHARED_MAP_FLAGS = {IN: "dci_inbound", OUT: "export_local_only"}


def _list_dict(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """A cleaned prefix list or community list as the neutral dict, None when the term has none."""
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    if "entries" in raw:
        entries = [
            {
                "seq": entry.get("seq"),
                "action": entry.get("action") or PERMIT,
                "prefix": entry.get("prefix"),
                "min_length": entry.get("min_length"),
                "max_length": entry.get("max_length"),
            }
            for entry in sorted(raw.get("entries") or [], key=lambda e: e.get("seq") or 0)
        ]
        return {"name": raw["name"], "family": raw.get("family"), "entries": entries}
    return {
        "name": raw["name"],
        "community_type": raw.get("community_type") or "standard",
        "members": list(raw.get("members") or []),
        "match": raw.get("match") or "any",
    }


def normalize_policies(raw: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The attached policies of one direction, ordered (priority, name), each with its terms ordered by seq."""
    policies: list[dict[str, Any]] = []
    for policy in raw or []:
        if not isinstance(policy, dict) or not policy.get("name"):
            continue
        terms = [
            {
                "policy": policy["name"],
                "seq": term.get("seq"),
                "action": term.get("action") or PERMIT,
                "prefix_list": _list_dict(term.get("match_prefix_list")),
                "community_list": _list_dict(term.get("match_community_list")),
                "local_pref": term.get("set_local_pref"),
                "med": term.get("set_med"),
                "add_communities": list(term.get("add_communities") or []),
                "delete_communities": _list_dict(term.get("delete_community_list")),
                "prepend": term.get("prepend_count"),
            }
            for term in sorted(policy.get("terms") or [], key=lambda t: t.get("seq") or 0)
            if isinstance(term, dict)
        ]
        policies.append(
            {
                "name": policy["name"],
                "priority": policy.get("priority") if policy.get("priority") is not None else 100,
                "default_action": policy.get("default_action") or PERMIT,
                "origin": policy.get("origin") or "manual",
                "terms": terms,
            }
        )
    return sorted(policies, key=lambda p: (p["priority"], p["name"]))


def _base_sets(session: dict[str, Any], direction: str) -> dict[str, Any]:
    """What the session itself sets in a direction: local-preference and no-export inbound, MED outbound."""
    if direction == IN:
        return {"local_pref": session.get("local_pref"), "no_export": bool(session.get("dci_inbound"))}
    return {"med": session.get("med")}


def _term_sets(term: dict[str, Any], base: dict[str, Any], direction: str) -> dict[str, Any]:
    """The sets of a permit term: the session's own, overridden by what the term sets; a deny term sets nothing."""
    if term["action"] == DENY:
        return {}
    sets: dict[str, Any] = dict(base)
    if term["local_pref"] is not None:
        sets["local_pref"] = term["local_pref"]
    if term["med"] is not None:
        sets["med"] = term["med"]
    # a large community has two colons (ASN:value:value): a template sets the two kinds with two commands
    sets["add_communities"] = [c for c in term["add_communities"] if str(c).count(":") != 2]
    sets["add_large_communities"] = [c for c in term["add_communities"] if str(c).count(":") == 2]
    sets["delete_communities"] = term["delete_communities"]
    sets["prepend"] = term["prepend"]
    return sets


def _family_map(
    session_name: str,
    direction: str,
    family: str | None,
    policies: list[dict[str, Any]],
    base: dict[str, Any],
    guard: dict[str, Any] | None,
) -> dict[str, Any]:
    """One map: the guard term, the terms of the policies (those of the family), then the closing term."""
    name = f"RM-USER-{session_name}-{direction.upper()}" + (f"-{family.upper()}" if family else "")
    terms: list[dict[str, Any]] = []
    if guard:
        terms.append(
            {
                "seq": 10,
                "policy": None,
                "source_seq": None,
                "action": DENY,
                "outside_guard": True,
                "match": {},
                "set": {},
            }
        )
    for policy in policies:
        for term in policy["terms"]:
            plist = term["prefix_list"]
            if family and plist and plist["family"] != family:
                continue
            terms.append(
                {
                    "seq": 10 * (len(terms) + 1),
                    "policy": policy["name"],
                    "source_seq": term["seq"],
                    "action": term["action"],
                    "outside_guard": False,
                    "match": {"prefix_list": plist, "community_list": term["community_list"]},
                    "set": _term_sets(term, base, direction),
                }
            )
    permit = all(policy["default_action"] == PERMIT for policy in policies)
    closing = {"seq": 10 * (len(terms) + 1), "action": PERMIT, "match": {}, "set": dict(base)} if permit else None
    return {
        "name": name,
        "direction": direction,
        "family": family,
        "filter": {"direction": direction, "family": family} if guard else None,
        "terms": terms,
        "closing": closing,
    }


def compose_user_maps(
    session: dict[str, Any], direction: str, policies: list[dict[str, Any]], filters: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The user maps of one direction of a session, None when no policy is attached.

    ``policies`` are normalized; ``filters`` the derived filters of ``session["policy"]``. A filter is a guard only when
    it is attached (``attached``): while the deployment stages the protection the guard is not enforced, so it is not
    put in the terms either.
    """
    if not policies:
        return None
    if session.get(SHARED_MAP_FLAGS[direction]):
        return {"direction": direction, "maps": [], "skipped": SHARED_MAP_FLAGS[direction], "lists": _lists([])}
    families = {f.get("family") for f in filters if f.get("direction") == direction}
    if families == {"evpn"}:  # an overlay session carries EVPN only: no platform has a policy match for it
        return {"direction": direction, "maps": [], "skipped": "evpn", "lists": _lists([])}
    base = _base_sets(session, direction)
    # The EVPN filter is not a unicast guard: a policy has no EVPN match on any platform, the filter stays as it is.
    guards = {
        f["family"]: f
        for f in filters
        if f.get("direction") == direction and f.get("attached") and f.get("family") != "evpn"
    }
    name = str(session.get("name"))
    if guards:
        maps = [_family_map(name, direction, family, policies, base, guard) for family, guard in sorted(guards.items())]
    else:
        maps = [_family_map(name, direction, None, policies, base, None)]
    return {"direction": direction, "maps": maps, "skipped": None, "lists": _lists(policies)}


def _lists(policies: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """The prefix lists and community lists the terms name, once each, by name (what a template defines)."""
    prefix: dict[str, dict[str, Any]] = {}
    community: dict[str, dict[str, Any]] = {}
    for policy in policies:
        for term in policy["terms"]:
            if term["prefix_list"]:
                prefix[term["prefix_list"]["name"]] = term["prefix_list"]
            for key in ("community_list", "delete_communities"):
                if term[key]:
                    community[term[key]["name"]] = term[key]
    return {"prefix": [prefix[n] for n in sorted(prefix)], "community": [community[n] for n in sorted(community)]}
