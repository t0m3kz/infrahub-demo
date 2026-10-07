"""Helper utilities for generators - organized by responsibility."""

from .cabling import (
    CableTypeDetector,
    CablingPlanError,
    CablingPlanner,
    ChainCablingStrategy,
    ConnectionValidator,
    InterfaceSpeedMatcher,
    IntraRackMiddleCablingStrategy,
    PodCablingStrategy,
    RackCablingStrategy,
)
from .interface_naming import get_lag_name, get_loopback_name
from .naming import DeviceNameContext, DeviceNamingConfig
from .pools import name_to_asn_range
from .routing import PendingASRef, RoutingPlanInput, RoutingPlanner, RoutingStrategy

__all__ = [
    "CableTypeDetector",
    "CablingPlanError",
    "CablingPlanner",
    "ChainCablingStrategy",
    "ConnectionValidator",
    "DeviceNameContext",
    "DeviceNamingConfig",
    "InterfaceSpeedMatcher",
    "IntraRackMiddleCablingStrategy",
    "PendingASRef",
    "PodCablingStrategy",
    "RackCablingStrategy",
    "RoutingPlanInput",
    "RoutingPlanner",
    "RoutingStrategy",
    "get_lag_name",
    "get_loopback_name",
    "name_to_asn_range",
]
