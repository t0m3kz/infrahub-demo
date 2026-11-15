"""Self-registering declarative topology specs package."""

from __future__ import annotations

from generators.specs import cables, devices, nodes, routing  # noqa: F401
from generators.specs.cables import CableSpec
from generators.specs.devices import INTERFACE_KIND_BY_FLAVOR, DeviceSpec, InterfaceFlavor, InterfaceSpec, IpSpec
from generators.specs.nodes import FirewallContextSpec, LoadBalancerVipSpec, RoutingTagSpec, SecurityTagSpec
from generators.specs.registry import (
    PlanReconciler,
    ReconcileResult,
    RegisteredStep,
    TopologyPlan,
    imperative_flow,
    register_spec,
    registered_steps,
)
from generators.specs.routing import BgpPeeringSpec, VrfSpec

__all__ = [
    "BgpPeeringSpec",
    "CableSpec",
    "DeviceSpec",
    "FirewallContextSpec",
    "INTERFACE_KIND_BY_FLAVOR",
    "InterfaceFlavor",
    "InterfaceSpec",
    "IpSpec",
    "LoadBalancerVipSpec",
    "PlanReconciler",
    "ReconcileResult",
    "RegisteredStep",
    "RoutingTagSpec",
    "SecurityTagSpec",
    "TopologyPlan",
    "VrfSpec",
    "imperative_flow",
    "register_spec",
    "registered_steps",
]
