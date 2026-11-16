"""What the rack generator waits for and which roles a rack can cable: the generators it waits on, the pod pools it needs,
the roles per deployment type."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

from generators.helpers.routing import RoutingStrategy

# The pod's own generators: a rack waits for an in-flight one before reading pod-level data
POD_GENERATORS = ("add_pod", "pod_rack_cascade")

# Pod-level pools this generator needs. The ASN pool is conditional (the parent DC's fabric_asn_pool), see
# _wait_for_pools
POD_POOL_FIELDS = ("loopback_pool", "prefix_pool")

# Underlays that allocate a per-device ASN from the DC's fabric_asn_pool (OSPF never does)
EBGP_UNDERLAY_STRATEGIES = frozenset({RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value})


def spine_bgp_readiness_role(dc: Mapping[str, Any]) -> str:
    """The ManagedBGP process role the pod's spines must own before a rack bootstraps. Only a role the strategy creates
    may be waited for: ``underlay`` for eBGP underlays (pre-seeded by add_pod), the shared-AS ``overlay`` for ospf-ibgp.
    """
    strategy = dc.get("routing_strategy", RoutingStrategy.EBGP_EBGP.value)
    return "underlay" if strategy in EBGP_UNDERLAY_STRATEGIES else "overlay"


# Roles that read _spine_device_names/_spine_interfaces: "leaf" and "tor" cable to the spines, "access_leaf" peers EVPN
# to them; an l2-leaf-only rack needs neither
SPINE_DEPENDENT_ROLES: frozenset[str] = frozenset({"leaf", "tor", "access_leaf"})

# fabric_templates roles a pod's deployment_type can cable: a tor pod has no leafs, so no target for
# l2_leaf/access_leaf.
ROLES_BY_DEPLOYMENT_TYPE: dict[str, frozenset[str]] = {
    "tor": frozenset({"tor"}),
    "mixed": frozenset({"leaf", "tor", "l2_leaf", "access_leaf"}),
    "middle_rack": frozenset({"leaf", "l2_leaf", "access_leaf"}),
}

# "tor" cables to a spine, "l2_leaf"/"access_leaf" to a local leaf pair: mutually exclusive on one rack
MUTUALLY_EXCLUSIVE_ROLE_GROUPS: tuple[frozenset[str], ...] = (frozenset({"tor", "l2_leaf", "access_leaf"}),)

# rack_type values with no leaf template of their own: they wait for, and cable to, their row's leaf pair. "tor" holds
# real ToRs and/or l2-leaf/access-leaf switches; "compute" is the same shape with l2-leaf/access-leaf only. Only the
# cabling target (spine vs row leaf) differs.
ROW_DEPENDENT_RACK_TYPES: frozenset[str] = frozenset({"tor", "compute"})
