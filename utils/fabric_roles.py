"""EVPN fabric device-role groupings shared by generators and checks."""

from __future__ import annotations

INTERNAL_FIREWALL_ROLE = "internal-firewall"
"""The role of an internal firewall: east-west and the customers' contexts."""

EXTERNAL_FIREWALL_ROLE = "external-firewall"
"""The role of an Internet-facing firewall: the Internet edge context its footprints list next to their internal one
(``serving_firewall_contexts``; utils/firewall_split.py). No dedicated customer context is built on it."""

FIREWALL_ROLES = frozenset({INTERNAL_FIREWALL_ROLE, EXTERNAL_FIREWALL_ROLE})
"""Any firewall pair, for code that means both."""

RR_ROLES = frozenset({"spine", "border-spine", "super-spine", "hyper-spine"})
"""Roles that act as overlay route reflectors."""

RR_CLIENT_ROLES = frozenset({"leaf", "border-leaf", "tor", "access-leaf"})
"""Roles that peer overlay EVPN as route-reflector clients."""

BORDER_ROLES = frozenset({"border-leaf", "border-spine"})
"""Roles rendered from the border_leafs/* templates: the DC border and its collapsed spine+border-leaf
(micro-fabric) form. They carry the tenant SVIs, the firewall transit legs and the same refusals/checks."""

CARRIER_ROLES = BORDER_ROLES | {"edge"}
"""Devices that terminate the firewall transit legs (get_exchange_transits): the DC border and, in a colocation
zone, the edge. A collapsed border-spine carries them like a border-leaf."""

OVERLAY_ROLES = RR_ROLES | RR_CLIENT_ROLES
"""Roles that run an overlay BGP process."""

# Roles per the dropdown in schemas/base/dcim.yml; hyphens only, underscore spellings would hide drift. `edge` is a VTEP
# only as a Multi-Site border gateway; other edges have no activations.
VTEP_ROLES = frozenset({"leaf", "border-leaf", "tor", "access-leaf", "border-spine", "edge"})
"""Roles that terminate VXLAN (transforms/helpers/vxlan.py)."""

DC_VTEP_ROLES = VTEP_ROLES - {"edge"}
"""The VTEPs of a data center: an edge is a VTEP only as the border gateway of a
colocation zone or virtual colocation, never part of a data center's fabric (utils/group_policy.py)."""

CAMPUS_FABRIC_ROLES = frozenset({"access-switch"})
"""Fabric edge of an office LISP site (SD-Access style): the nodes hosts attach to. Deliberately not in
DC_VTEP_ROLES or BORDER_ROLES: those drive the DC-only SVI refusals, the VXLAN rendering and the group policy."""

LISP_BORDER_ROLES = frozenset({"distribution-switch"})
"""Co-located border and control-plane (map-server/map-resolver) node of an office LISP site."""

OFFICE_LISP_ROLES = CAMPUS_FABRIC_ROLES | LISP_BORDER_ROLES
"""Every role of an office LISP site. They always render as LISP: an office has no other overlay, so the role is
the whole gate."""
