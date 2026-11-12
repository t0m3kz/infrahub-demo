"""The routing policy of a BGP session, composed once for every platform."""

from __future__ import annotations

import hashlib
from ipaddress import ip_network
from typing import Any

from transforms.helpers.routing_policy import IN, OUT, compose_user_maps
from utils.bgp_expectation import (
    DCI,
    DCI_UNDERLAY,
    DEFAULT_ROUTES,
    EVPN,
    FABRIC,
    IPV4,
    IPV6,
    OVERLAY,
    UNDERLAY,
    VRF_INTERNAL,
    Entry,
    Expectation,
)

# The platforms whose templates render a prefix filter for a unicast session (common/bgp_filter_objects.j2 on the CLI
# platforms, common/sonic_bgp_policy.j2 on SONiC) and for an EVPN one.
UNICAST_FILTER_PLATFORMS = frozenset({"cisco_nxos", "arista_eos", "cisco_xe", "cisco_ios", "sonic", "dell_sonic"})
EVPN_FILTER_PLATFORMS = frozenset({"cisco_nxos"})

FABRIC_OUT_MAP = "RM-FABRIC-OUT"
DCI_IN_MAP = "RM-DCI-IN"


def _maximums(
    expectation: Expectation | None, enforce: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """The maximum-prefix per family and the unicast total of an expectation."""
    if expectation is None:
        return {}, None
    warning_only = not enforce
    limits = {
        family: {"limit": policy.max_prefix, "warning_only": warning_only}
        for family, policy in expectation.families.items()
        if policy.max_prefix
    }
    unicast = [entry["limit"] for family, entry in limits.items() if family != EVPN]
    total = {"limit": sum(unicast), "warning_only": warning_only} if unicast else None
    return limits, total


_FILTERED = (UNDERLAY, FABRIC, DCI_UNDERLAY, OVERLAY, DCI, VRF_INTERNAL)
# A session of these classes is named after itself: its allowed set is the far end's, not the class's.
_PER_SESSION = (FABRIC, DCI_UNDERLAY)


def _entry_dicts(entries: frozenset[Entry], default_families: tuple[str, ...]) -> list[dict[str, Any]]:
    """The entries as sorted dicts, with the default route of each family in ``default_families`` added."""
    wanted = set(entries) | {Entry(DEFAULT_ROUTES[family]) for family in default_families}
    ordered = sorted(wanted, key=lambda e: (ip_network(e.prefix).version, ip_network(e.prefix)))
    return [{"prefix": e.prefix, "le": e.le, "family": e.family} for e in ordered]


def _base(session: dict[str, Any], klass: str, family: str, direction: str) -> str:
    """The name stem of a filter: class level when every session of the class has the same set, else per session."""
    stem = f"{family.upper()}-{direction.upper()}"
    if klass in _PER_SESSION:
        return f"{session.get('name')}-{stem}"
    if klass == VRF_INTERNAL:
        return f"VRF-{session.get('vrf')}-{stem}"
    return f"{klass.upper()}-{stem}"


def _filters(session: dict[str, Any], expectation: Expectation, enforce: bool) -> list[dict[str, Any]]:
    """The derived filters of a session: per direction, all of its families or none."""
    if expectation.klass not in _FILTERED:
        return []
    found: list[dict[str, Any]] = []
    for direction in ("in", "out"):
        per_family: list[dict[str, Any]] = []
        for family, policy in expectation.families.items():
            entries = policy.inbound if direction == "in" else policy.outbound
            if entries is None:
                per_family = []
                break
            default = policy.default_in if direction == "in" else policy.default_out
            defaults = ((IPV4, IPV6) if family == EVPN else (family,)) if default else ()
            sets: dict[str, Any] = {}
            if direction == "in":
                sets = {"local_pref": session.get("local_pref"), "no_export": bool(session.get("dci_inbound"))}
            else:
                sets = {"med": session.get("med")}
            per_family.append(
                {
                    "direction": direction,
                    "family": family,
                    "base": _base(session, expectation.klass, family, direction),
                    "entries": _entry_dicts(entries, defaults),
                    "sets": sets,
                    "attached": enforce,
                }
            )
        found.extend(per_family)
    return found


def compose_policy(
    session: dict[str, Any], expectation: Expectation | None = None, *, enforce: bool = False
) -> dict[str, Any]:
    """The neutral policy of a built session (peer groups already assigned)."""
    name = session.get("name")
    own_in = (
        {"name": f"{name}-LOCAL-PREF-IN", "local_pref": session["local_pref"]} if session.get("local_pref") else None
    )
    own_out = {"name": f"{name}-MED-OUT", "med": session["med"]} if session.get("med") else None
    filters = _filters(session, expectation, enforce) if expectation else []
    # A direction whose filters are attached carries one map, the filter's, which sets what the maps it replaces set.
    replaced = {f["direction"] for f in filters if f["attached"]}
    attach: list[dict[str, Any]] = []
    if own_in and "in" not in replaced:
        attach.append({"map": own_in["name"], "direction": "in", "own": True})
    if own_out and "out" not in replaced:
        attach.append({"map": own_out["name"], "direction": "out", "own": True})
    if session.get("export_local_only") and "out" not in replaced:
        attach.append({"map": FABRIC_OUT_MAP, "direction": "out", "own": False})
    if session.get("dci_inbound") and "in" not in replaced:
        attach.append({"map": DCI_IN_MAP, "direction": "in", "own": False})
    limits, total = _maximums(expectation, enforce)
    attached = session.get("routing_policies") or {}
    user = [
        composed
        for direction in (IN, OUT)
        if (composed := compose_user_maps(session, direction, attached.get(direction) or [], filters))
    ]
    # A direction with user maps names those (common/bgp_filter_objects.j2 bgp_user_attachments, per address family) and
    # not its own, the filter's or a shared one: the user map holds the session's sets and the guard.
    for composed in user:
        if composed["maps"]:
            attach[:] = [a for a in attach if a["direction"] != composed["direction"]]
        # The user map of a family holds the guard (its first term), so the filter map of that direction and family is
        # not attached: a neighbour names one map per direction and family.
        for rmap in composed["maps"]:
            for flt in filters:
                if (
                    rmap["filter"]
                    and flt["direction"] == rmap["filter"]["direction"]
                    and flt["family"] == rmap["filter"]["family"]
                ):
                    flt["replaced_by"] = rmap["name"]
    return {
        "inbound": {"own": own_in if "in" not in replaced else None},
        "outbound": {"own": own_out if "out" not in replaced else None},
        "attach": attach,
        "max": limits,
        "max_total": total,
        "enforced": enforce,
        "filters": filters,
        "user": user,
        # The directions that name a routing policy map: a template's own map for them (RM-TRANSIT-*) is not used.
        "user_directions": [composed["direction"] for composed in user if composed["maps"]],
    }


def name_filters(sessions: list[dict[str, Any]]) -> None:
    """Give every filter of a device its object names, unique per content."""
    contents: dict[str, set[str]] = {}
    for session in sessions:
        for flt in session.get("policy", {}).get("filters", []):
            flt["digest"] = hashlib.sha1(
                repr((flt["entries"], sorted((k, str(v)) for k, v in flt["sets"].items()))).encode()
            ).hexdigest()[:6]
            contents.setdefault(flt["base"], set()).add(flt["digest"])
    for session in sessions:
        for flt in session.get("policy", {}).get("filters", []):
            base = flt["base"] if len(contents[flt["base"]]) == 1 else f"{flt['base']}-{flt['digest']}"
            flt["map"] = f"RM-{base}"
            flt["list"] = f"PL-{base}"
            flt["list6"] = f"PL6-{base}"
