"""EVPN fabric device-role groupings shared by generators and checks."""

from __future__ import annotations

RR_ROLES = frozenset({"spine", "border-spine", "super-spine", "hyper-spine"})
"""Roles that act as overlay route reflectors."""

RR_CLIENT_ROLES = frozenset({"leaf", "border-leaf", "tor", "access-leaf"})
"""Roles that peer overlay EVPN as route-reflector clients."""

OVERLAY_ROLES = RR_ROLES | RR_CLIENT_ROLES
"""Roles that run an overlay BGP process."""
