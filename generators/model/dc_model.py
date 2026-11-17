"""The Data Center fabric model: pool prefix lengths, ASN range, tier quantities (pure, no I/O)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from generators.helpers.routing import RoutingStrategy, underlay_pool_is_ipv6
from generators.ipam.pools import name_to_asn_range
from generators.model.pod_profile import templates_by_role
from generators.model.sizing import (
    dc_loopback_bits,
    dc_management_bits,
    dc_technical_bits,
    fabric_loopback_bits,
    host_bits_to_prefix_length,
)

DC_VALID_FABRIC_ROLES = frozenset(
    {"super-spine", "hyper-spine", "border-leaf", "internal-firewall", "external-firewall", "load-balancer"}
)

# The pool slot of the DC-scoped tiers' loopbacks (super-spine, hyper-spine, border-leaf)
DC_FABRIC_LOOPBACK = "dc-fabric-loopback"

_EBGP_STRATEGIES = frozenset({RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value})


@dataclass(frozen=True)
class DcFabricModel:
    """What a TopologyDataCenter declares, compiled: the pools to slice, the ASN range, the DC-tier quantities."""

    dc_id: str
    dc_name: str
    fabric_name: str
    dc_index: int
    design_mode: Literal["back-to-back", "super-spine"]
    underlay_protocol: str
    routing_strategy: str
    # pool slot -> prefix length, in allocation order (technical, loopback, management, then dc-fabric-loopback)
    pools_to_allocate: dict[str, int]
    asn_start: int
    asn_end: int
    requires_asn_pool: bool
    max_super_spines: int
    max_hyper_spines: int
    super_spine_quantity: int
    hyper_spine_quantity: int
    # fabric_templates roles no DC tier can place, in entry order (the generator warns and skips them)
    invalid_roles: tuple[Any, ...]

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> DcFabricModel:
        """Compile one clean_data() TopologyDataCenter entry; a missing required key raises KeyError."""
        dc_name: str = data["name"]
        dc_design: Mapping[str, Any] = data["design"]
        underlay_protocol: str = data.get("underlay_protocol", "ipv6")
        routing_strategy: str = data.get("routing_strategy", "ebgp-ebgp")
        fabric_templates: list[dict[str, Any]] = data.get("fabric_templates", [])

        # Host bits become a prefix length in the family each pool is sliced from; management is always IPv4.
        pools: dict[str, int] = {
            "technical": host_bits_to_prefix_length(
                dc_technical_bits(dc_design), ipv6=underlay_pool_is_ipv6(underlay_protocol, "technical")
            ),
            "loopback": host_bits_to_prefix_length(
                dc_loopback_bits(dc_design), ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback")
            ),
            "management": host_bits_to_prefix_length(dc_management_bits(dc_design), ipv6=False),
        }
        # One DC-scoped loopback pool for the DC-level tiers, sized from the design's max caps.
        tier_loopback_bits = fabric_loopback_bits(dc_design)
        if tier_loopback_bits is not None:
            pools[DC_FABRIC_LOOPBACK] = host_bits_to_prefix_length(
                tier_loopback_bits, ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback")
            )

        max_super_spines = dc_design["max_super_spines_per_fabric"]
        asn_start, asn_end = name_to_asn_range(
            dc_name=dc_name,
            max_pods=dc_design["max_pods"],
            max_border_leafs_per_fabric=dc_design["max_border_leafs_per_fabric"],
        )
        return cls(
            dc_id=data["id"],
            dc_name=dc_name,
            fabric_name=dc_name.lower(),
            dc_index=data["index"],
            design_mode="back-to-back" if max_super_spines == 0 else "super-spine",
            underlay_protocol=underlay_protocol,
            routing_strategy=routing_strategy,
            pools_to_allocate=pools,
            asn_start=asn_start,
            asn_end=asn_end,
            requires_asn_pool=routing_strategy in _EBGP_STRATEGIES,
            max_super_spines=max_super_spines,
            max_hyper_spines=dc_design["max_hyper_spines_per_fabric"],
            super_spine_quantity=_quantity(fabric_templates, "super-spine"),
            hyper_spine_quantity=_quantity(fabric_templates, "hyper-spine"),
            invalid_roles=tuple(
                entry["role"] for entry in fabric_templates if entry["role"] not in DC_VALID_FABRIC_ROLES
            ),
        )

    def super_spine_error(self) -> str | None:
        """Why the requested super-spines exceed the design, None when they fit."""
        if self.super_spine_quantity > self.max_super_spines:
            return (
                f"DC {self.fabric_name.upper()} requests {self.super_spine_quantity} super-spines but the assigned "
                f"design allows at most {self.max_super_spines}"
            )
        return None

    def hyper_spine_error(self) -> str | None:
        """Why the requested hyper-spines exceed the design, None when they fit."""
        if self.hyper_spine_quantity > self.max_hyper_spines:
            return (
                f"DC {self.fabric_name.upper()} requests {self.hyper_spine_quantity} hyper-spines but the assigned "
                f"design allows at most {self.max_hyper_spines}"
            )
        return None


def _quantity(fabric_templates: list[dict[str, Any]], role: str) -> int:
    return sum(entry["quantity"] for entry in templates_by_role(fabric_templates, role))
