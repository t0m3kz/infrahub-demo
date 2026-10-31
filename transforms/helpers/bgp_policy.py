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
"""

from typing import Any

from utils.bgp_expectation import EVPN, Expectation

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
    attach: list[dict[str, Any]] = []
    if own_in:
        attach.append({"map": own_in["name"], "direction": "in", "own": True})
    if own_out:
        attach.append({"map": own_out["name"], "direction": "out", "own": True})
    if session.get("export_local_only"):
        attach.append({"map": FABRIC_OUT_MAP, "direction": "out", "own": False})
    if session.get("dci_inbound"):
        attach.append({"map": DCI_IN_MAP, "direction": "in", "own": False})
    limits, total = _maximums(expectation, enforce)
    return {
        "inbound": {"own": own_in},
        "outbound": {"own": own_out},
        "attach": attach,
        "max": limits,
        "max_total": total,
        "enforced": enforce,
    }
