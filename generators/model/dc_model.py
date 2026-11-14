"""Pure Data Center fabric model compiler: derives pool sizes, ASN ranges, and role counts without I/O."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from generators.helpers import name_to_asn_range
from generators.helpers.routing import RoutingStrategy, underlay_pool_is_ipv6
from generators.mixins.fabric_sizing import (
    dc_loopback_bits,
    dc_management_bits,
    dc_technical_bits,
    fabric_loopback_bits,
    host_bits_to_prefix_length,
)
from generators.mixins.pod_config import templates_by_role

DC_VALID_FABRIC_ROLES = frozenset(
    {"super-spine", "hyper-spine", "border-leaf", "internal-firewall", "external-firewall", "load-balancer"}
)

_EBGP_STRATEGIES = frozenset({RoutingStrategy.EBGP_EBGP.value, RoutingStrategy.EBGP_IBGP.value})


@dataclass(frozen=True)
class DcFabricModel:
    """Compiled, immutable blueprint of a Data Center's pools, ASN envelope, and device role counts."""

    dc_id: str
    dc_name: str
    fabric_name: str
    dc_index: int
    design_mode: Literal["back-to-back", "super-spine"]
    underlay_protocol: str
    routing_strategy: str
    pools_to_allocate: dict[str, int]
    asn_range: tuple[int, int]
    requires_asn_pool: bool
    role_quantities: dict[str, int]

    @classmethod
    def compile(cls, data: Mapping[str, Any]) -> DcFabricModel:
        """Compile a clean_data() TopologyDataCenter dictionary into a validated DcFabricModel."""
        dc_id: str = data["id"]
        dc_name: str = data["name"]
        dc_index: int = data["index"]
        dc_design: Mapping[str, Any] = data["design"]

        technical_bits = dc_technical_bits(dc_design)
        loopback_bits = dc_loopback_bits(dc_design)
        management_bits = dc_management_bits(dc_design)
        tier_loopback_bits = fabric_loopback_bits(dc_design)

        fabric_templates: list[dict[str, Any]] = list(data.get("fabric_templates") or [])
        role_quantities: dict[str, int] = {}
        for entry in fabric_templates:
            role = entry.get("role")
            if role not in DC_VALID_FABRIC_ROLES:
                raise ValueError(
                    f"DC {dc_name.upper()}: invalid fabric template role {role!r}; "
                    f"valid roles: {sorted(DC_VALID_FABRIC_ROLES)}"
                )
            role_quantities[role] = role_quantities.get(role, 0) + int(entry.get("quantity", 0))

        super_spines = sum(entry["quantity"] for entry in templates_by_role(fabric_templates, "super-spine"))
        max_super_spines = dc_design["max_super_spines_per_fabric"]
        if super_spines > max_super_spines:
            raise RuntimeError(
                f"DC {dc_name.upper()} requests {super_spines} super-spines but the assigned "
                f"design allows at most {max_super_spines}"
            )

        underlay_protocol: str = data.get("underlay_protocol", "ipv6")
        pools_to_allocate: dict[str, int] = {
            "technical": host_bits_to_prefix_length(
                technical_bits, ipv6=underlay_pool_is_ipv6(underlay_protocol, "technical")
            ),
            "loopback": host_bits_to_prefix_length(
                loopback_bits, ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback")
            ),
            "management": host_bits_to_prefix_length(management_bits, ipv6=False),
        }
        if tier_loopback_bits is not None:
            pools_to_allocate["dc-fabric-loopback"] = host_bits_to_prefix_length(
                tier_loopback_bits, ipv6=underlay_pool_is_ipv6(underlay_protocol, "loopback")
            )

        design_mode: Literal["back-to-back", "super-spine"] = "back-to-back" if max_super_spines == 0 else "super-spine"
        routing_strategy: str = data.get("routing_strategy", "ebgp-ebgp")
        asn_range = name_to_asn_range(
            dc_name=dc_name,
            max_pods=dc_design["max_pods"],
            max_border_leafs_per_fabric=dc_design["max_border_leafs_per_fabric"],
        )
        requires_asn_pool = routing_strategy in _EBGP_STRATEGIES

        return cls(
            dc_id=dc_id,
            dc_name=dc_name,
            fabric_name=dc_name.lower(),
            dc_index=dc_index,
            design_mode=design_mode,
            underlay_protocol=underlay_protocol,
            routing_strategy=routing_strategy,
            pools_to_allocate=pools_to_allocate,
            asn_range=asn_range,
            requires_asn_pool=requires_asn_pool,
            role_quantities=role_quantities,
        )
