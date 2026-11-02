"""The routing policy of a BGP session, composed once for every platform.

A neighbour direction carries ONE route-map on every vendor, so everything that shapes what a session sends or
accepts (the local-preference and MED of the peering, the shared maps of a border-gateway pair, and later the
protection derived by ``utils/bgp_expectation.py``) is composed here into ``session["policy"]`` and the templates only
render that structure. A template never decides policy from the raw ``local_pref`` / ``med`` / ``export_local_only``
flags.

``policy`` is neutral (no vendor syntax):

- ``inbound.own`` / ``outbound.own``: the session's own map, ``{"name": ..., "local_pref": n}`` or ``{"name": ..., "med": n}``,
  None when the peering sets neither. The template defines it before ``router bgp`` and names it on the neighbour.
- ``attach``: the maps the neighbour names, in render order, each ``{"map": name, "direction": "in"|"out", "own": bool}``.
  ``own`` is False for a map another template defines (``RM-FABRIC-OUT``, ``RM-DCI-IN``).
- ``max``: the maximum-prefix per address family, ``{"ipv4": {"limit": n, "warning_only": bool}}``, derived from the
  model (utils/bgp_expectation.py); empty when nothing could be derived. ``max_total`` is the same summed over the
  unicast families, for a vendor whose command is per neighbour and not per family. A limit warns only
  (``warning_only``) while the hosting deployment stages the protection (``routing_filter_enforce`` false), an external
  or transit session included: its former typed limit (100) is gone and the derived one (the size of its allowed set
  plus the slack) must not reset a live session before the owner has seen it. ``enforced`` is the switch itself, for the
  lines that are rendered only when it is on.
- ``filters``: the prefix filters derived for the session, one per direction and address family, all families of a
  direction or none (a direction with a family that cannot be derived stays unfiltered and the check reports it):
  ``{"direction", "family", "base", "entries", "sets", "attached"}``. ``entries`` are ``{"prefix", "le", "family"}``
  (the default route included when the class may carry one) and ``sets`` what the same map also sets
  (``local_pref``, ``med``, ``no_export``). ``base`` names the objects (``RM-<base>``, ``PL-<base>`` for IPv4 entries,
  ``PL6-<base>`` for IPv6 ones); ``name_filters`` makes it unique per content on a device. ``attached`` is False while
  the deployment stages: the objects are rendered, the neighbour does not name them, and the maps they replace stay
  attached. Once attached, a filter map REPLACES the own map and the shared map of its direction (it carries their
  ``set`` lines), so a neighbour still names one map per direction.
"""

from __future__ import annotations

import hashlib
from ipaddress import ip_network
from typing import Any

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
# platforms, common/sonic_bgp_policy.j2 on SONiC) and for an EVPN one (NX-OS only: no keyword is known elsewhere, U).
# checks/bgp_closure.py reports a session on any other platform as unprotected.
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
    """The neutral policy of a built session (peer groups already assigned).

    ``expectation`` is utils/bgp_expectation.py's answer for the session, ``enforce`` the hosting deployment's
    ``routing_filter_enforce``. Without an expectation the policy carries no limit.
    """
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
    return {
        "inbound": {"own": own_in if "in" not in replaced else None},
        "outbound": {"own": own_out if "out" not in replaced else None},
        "attach": attach,
        "max": limits,
        "max_total": total,
        "enforced": enforce,
        "filters": filters,
    }


def name_filters(sessions: list[dict[str, Any]]) -> None:
    """Give every filter of a device its object names, unique per content.

    Filters with the same ``base`` and the same content share one route-map (a spine's hundred underlay neighbours
    name ``RM-UNDERLAY-IPV4-IN``); the same ``base`` with different content (the overlay default route comes from a
    relay but not from a leaf, a local-preference differs) gets a short digest of the content, so a name never means
    two things on one device.
    """
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
