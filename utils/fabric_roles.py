"""EVPN fabric device-role groupings shared by generators and checks."""

from __future__ import annotations

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

# Roles that terminate VXLAN. Sourced from the role dropdown descriptions in
# schemas/base/dcim.yml: `l2-leaf` is explicitly "no VXLAN/overlay BGP" (it trunks
# VLANs up to a leaf), while `access-leaf` is a "routed VTEP ... full VXLAN/overlay
# BGP". `border-spine` is a collapsed spine+border-leaf for micro-fabrics, so it
# does terminate VXLAN. Spine/super-spine/hyper-spine are underlay + EVPN route
# reflectors only.
#
# NOTE: do not add role spellings with underscores here. The schema uses hyphens
# (`border-leaf`); accepting both silently hides drift between layers.
#
# `edge` is a VTEP only where it is its site's EVPN Multi-Site border gateway
# (a colocation zone or virtual colocation edge stretching DC segments into the metro); every other
# edge has no stretched segment, so no activations, so get_vxlan_config still
# returns None for it.
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
