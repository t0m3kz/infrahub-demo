"""EVPN fabric device-role groupings shared by generators and checks."""

from __future__ import annotations

RR_ROLES = frozenset({"spine", "border-spine", "super-spine", "hyper-spine"})
"""Roles that act as overlay route reflectors."""

RR_CLIENT_ROLES = frozenset({"leaf", "border-leaf", "tor", "access-leaf"})
"""Roles that peer overlay EVPN as route-reflector clients."""

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
# (a colocation metro edge stretching DC segments into the cage); every other
# edge has no stretched segment, so no activations, so get_vxlan_config still
# returns None for it.
VTEP_ROLES = frozenset({"leaf", "border-leaf", "tor", "access-leaf", "border-spine", "edge"})
"""Roles that terminate VXLAN (transforms/helpers/vxlan.py)."""

DC_VTEP_ROLES = VTEP_ROLES - {"edge"}
"""The VTEPs of a data center: an edge is a VTEP only as a colocation metro's
border gateway, never part of a data center's fabric (utils/group_policy.py)."""
