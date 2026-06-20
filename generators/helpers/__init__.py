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
    ConnectionValidator,
    InterfaceSpeedMatcher,
    IntraRackCablingStrategy,
    IntraRackMiddleCablingStrategy,
    IntraRackMixedCablingStrategy,
    PodCablingStrategy,
    RackCablingStrategy,
)
from .naming import DeviceNamingConfig
from .pools import (
    DEFAULT_ASN_BASE_START,
    calculate_fabric_asn_block_size,
    calculate_pod_pools,
    calculate_super_spine_loopback_prefix,
    name_to_asn_range,
)
from .routing import RoutingPlan, RoutingPlanInput, RoutingPlanner, RoutingStrategy

__all__ = [
    # Routing
    "RoutingPlan",
    "RoutingPlanInput",
    "RoutingPlanner",
    "RoutingStrategy",
    # Cabling
    "CablingPlanner",
    "CablingStrategy",
    "PodCablingStrategy",
    "RackCablingStrategy",
    "IntraRackCablingStrategy",
    "IntraRackMiddleCablingStrategy",
    "IntraRackMixedCablingStrategy",
    # Naming
    "DeviceNamingConfig",
    # Pools
    "calculate_pod_pools",
    "DEFAULT_ASN_BASE_START",
    "calculate_fabric_asn_block_size",
    "calculate_super_spine_loopback_prefix",
    "name_to_asn_range",
    # Interfaces
    "InterfaceSpeedMatcher",
    "CableTypeDetector",
    "ConnectionValidator",
]
