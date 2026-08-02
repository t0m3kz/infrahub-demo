"""Helper utilities for generators - organized by responsibility.

This module provides reusable utilities for generator implementations:

- cabling: Connection planning strategies and interface organization
- naming: Device naming configuration and formatting
- pools: IP address pool and prefix calculation
- interfaces: Speed matching, cable type detection, and validation
- routing: Routing protocol generation with strategy pattern
"""

# Re-export all public APIs for backward compatibility
from .cabling import (
    CableTypeDetector,
    CablingPlanner,
    CablingStrategy,
    ChainCablingStrategy,
    ConnectionValidator,
    InterfaceSpeedMatcher,
    IntraRackCablingStrategy,
    IntraRackMiddleCablingStrategy,
    IntraRackMixedCablingStrategy,
    PodCablingStrategy,
    RackCablingStrategy,
    pick_matched_switch_port_name,
)
from .common import retry_delay
from .interface_naming import get_lag_name, get_loopback_name
from .naming import DeviceNameContext, DeviceNamingConfig
from .pools import (
    DEFAULT_ASN_BASE_START,
    calculate_dc_fabric_loopback_prefix,
    calculate_fabric_asn_block_size,
    calculate_loopback_prefix,
    calculate_pod_pools,
    name_to_asn_range,
)
from .ports import PortProfileHelper, PortsPlanner
from .quotation import (
    PORT_SPEEDS,
    DeviceTemplate,
    PortGroup,
    Recommender,
    TierResult,
    assign_racks_to_rooms,
    build_fabric_tiers,
    build_multi_speed_fabric,
    build_multi_speed_leaf_tiers,
    build_proposed_pods,
    build_room_pods,
    build_switch_fabric,
    device_templates_from_graphql,
    distribute_evenly,
    max_fabric_capacity,
    recommend_design,
    recommend_pod_design,
    validate_room_capacity,
)
from .routing import PendingASRef, RoutingPlan, RoutingPlanInput, RoutingPlanner, RoutingStrategy
from .rules import RulePlanningHelper, RulesPlanner

__all__ = [
    # Routing
    "PendingASRef",
    "RoutingPlan",
    "RoutingPlanInput",
    "RoutingPlanner",
    "RoutingStrategy",
    # Cabling
    "CablingPlanner",
    "CablingStrategy",
    "PodCablingStrategy",
    "RackCablingStrategy",
    "ChainCablingStrategy",
    "IntraRackCablingStrategy",
    "IntraRackMiddleCablingStrategy",
    "IntraRackMixedCablingStrategy",
    # Naming
    "DeviceNameContext",
    "DeviceNamingConfig",
    # Pools
    "calculate_pod_pools",
    "DEFAULT_ASN_BASE_START",
    "calculate_fabric_asn_block_size",
    "calculate_dc_fabric_loopback_prefix",
    "calculate_loopback_prefix",
    "name_to_asn_range",
    "retry_delay",
    # Interfaces
    "InterfaceSpeedMatcher",
    "CableTypeDetector",
    "ConnectionValidator",
    "pick_matched_switch_port_name",
    # Interface naming
    "get_lag_name",
    "get_loopback_name",
    "PortProfileHelper",
    "PortsPlanner",
    "RulePlanningHelper",
    "RulesPlanner",
    # Quotation
    "PORT_SPEEDS",
    "DeviceTemplate",
    "PortGroup",
    "Recommender",
    "TierResult",
    "assign_racks_to_rooms",
    "build_fabric_tiers",
    "build_multi_speed_fabric",
    "build_multi_speed_leaf_tiers",
    "build_proposed_pods",
    "build_room_pods",
    "build_switch_fabric",
    "device_templates_from_graphql",
    "distribute_evenly",
    "max_fabric_capacity",
    "recommend_design",
    "recommend_pod_design",
    "validate_room_capacity",
]
