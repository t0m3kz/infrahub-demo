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
"""

from typing import Any

FABRIC_OUT_MAP = "RM-FABRIC-OUT"
DCI_IN_MAP = "RM-DCI-IN"


def compose_policy(session: dict[str, Any]) -> dict[str, Any]:
    """The neutral policy of a built session (peer groups already assigned)."""
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
    return {"inbound": {"own": own_in}, "outbound": {"own": own_out}, "attach": attach}
