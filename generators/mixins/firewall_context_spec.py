"""What a kind of service context differs in, and the records a context flow passes around. ContextSpec is the per-kind
configuration of FirewallContextMixin (firewall_context.py) and TransitLegsMixin (firewall_transit.py).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from generators.mixins.lb_vip import APP_COMPONENT_GENERATOR, load_balanced_components_in
from generators.protocols import (
    ManagedFirewallContext,
    ManagedFirewallHA,
    ManagedLoadbalancerContext,
    ManagedLoadbalancerHA,
)
from utils.exchange_transit import (
    EXCHANGE_PEERS,
    INTERNET_TYPE,
    lb_leg_pool_name,
    lb_vip_pool_name,
    transit_pool_name,
)
from utils.fabric_roles import EXTERNAL_FIREWALL_ROLE, INTERNAL_FIREWALL_ROLE
from utils.limits import FW_CONTEXT_VLAN_END, FW_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END, LB_CONTEXT_VLAN_START


def _firewall_leg_types(tenant_type: str) -> tuple[str, ...]:
    """The tenant VRF and every VRF it exchanges with (EXCHANGE_PEERS), tenant first."""
    return (tenant_type, *EXCHANGE_PEERS[tenant_type])


def _edge_leg_types(tenant_type: str) -> tuple[str, ...]:
    """The Internet edge context has a leg in the tenant VRF and one in INTERNET, nothing else."""
    return (tenant_type, INTERNET_TYPE)


def _no_skip(_parent: dict[str, Any]) -> str | None:
    """Every parent with devices of the role gets the context."""
    return None


def _inline_parent(parent: dict[str, Any]) -> str | None:
    """Why an inline DC gets no load-balancer context: its load balancers sit in the firewall chain
    (CablingMixin._cable_border_services).
    """
    if parent.get("connectivity_mode") == "inline":
        return "its load balancers are inline behind the firewall (connectivity_mode inline)"
    return None


@dataclass(frozen=True)
class ContextSpec:
    """What a kind of service context differs in; everything else is one flow. Fields: lock key and VLAN pool name,
    tenant leg types, leg identifier, VLAN range, skip reason, VIP pool, serving field(s), relink consumers.
    """

    label: str
    role: str
    devices_key: str
    dedicated_flag: str
    ha_kind: type[Any]
    context_kind: type[Any]
    key: str
    serving_field: str
    serving_many: bool
    leg_types: Callable[[str], tuple[str, ...]]
    leg_pool_name: Callable[[str], str]
    leg_identifier: str
    vlan_range: tuple[int, int]
    skip_reason: Callable[[dict[str, Any]], str | None] = _no_skip
    vip_pool_name: Callable[[str], str] | None = None
    relink_consumers: Callable[[Any, str], Awaitable[list[str]]] | None = None
    relink_generator: str = ""


FIREWALL_CONTEXT = ContextSpec(
    label="FirewallContext",
    role=INTERNAL_FIREWALL_ROLE,
    devices_key="firewall_devices",
    dedicated_flag="dedicated_firewall",
    ha_kind=ManagedFirewallHA,
    context_kind=ManagedFirewallContext,
    key="fw-context",
    serving_field="serving_firewall_contexts",
    serving_many=True,
    leg_types=_firewall_leg_types,
    leg_pool_name=transit_pool_name,
    leg_identifier="transit",
    vlan_range=(FW_CONTEXT_VLAN_START, FW_CONTEXT_VLAN_END),
)

EDGE_CONTEXT = ContextSpec(
    label="FirewallContext",
    role=EXTERNAL_FIREWALL_ROLE,
    devices_key="external_firewall_devices",
    dedicated_flag="",
    ha_kind=ManagedFirewallHA,
    context_kind=ManagedFirewallContext,
    # The VLAN pool and lock prefix are the firewall contexts': an edge context draws from the same DC pool, so no two
    # share a VLAN
    key="fw-context",
    # The footprint lists its Internet edge context next to its internal one (utils/firewall_split.py)
    serving_field="serving_firewall_contexts",
    serving_many=True,
    leg_types=_edge_leg_types,
    leg_pool_name=transit_pool_name,
    leg_identifier="transit",
    vlan_range=(FW_CONTEXT_VLAN_START, FW_CONTEXT_VLAN_END),
)

LOADBALANCER_CONTEXT = ContextSpec(
    label="LoadbalancerContext",
    role="load-balancer",
    devices_key="loadbalancer_devices",
    dedicated_flag="dedicated_loadbalancer",
    ha_kind=ManagedLoadbalancerHA,
    context_kind=ManagedLoadbalancerContext,
    key="lb-context",
    serving_field="serving_loadbalancer_context",
    serving_many=False,
    leg_types=lambda tenant_type: (tenant_type,),
    leg_pool_name=lb_leg_pool_name,
    leg_identifier="lb-leg",
    vlan_range=(LB_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END),
    skip_reason=_inline_parent,
    vip_pool_name=lb_vip_pool_name,
    # The VIP and its R_server rule live on the serving context; the firewall's rules are placed at render time, so
    # nothing to re-run
    relink_consumers=load_balanced_components_in,
    relink_generator=APP_COMPONENT_GENERATOR,
)


@dataclass(frozen=True)
class _ContextTarget:
    """Where a customer's context lives and who owns its writes."""

    spec: ContextSpec
    name_base: str
    # The hosting deployment (a DC, or the metro of a zone): the context's mandatory Parent
    deployment_id: str
    tenant_id: str | None
    track: bool
    devices: list[Any]
    parent_name: str
    # With an external-firewall pair the customer contexts keep no INTERNET leg (the edge context has it), a dedicated
    # one only its own public address
    edge_hosted: bool = False

    def context_name(self, tenant_namespace: str) -> str:
        """The context's name: `{name_base}-{namespace}` (track), else `shared-{name_base}-{namespace}`. Shared is per
        (cluster, tenant namespace) so PROD and NON-PROD never meet; a dedicated pair's HA domain is
        `{name_base}-ha-{namespace}` (a distinct name).
        """
        scope = "" if self.track else "shared-"
        return f"{scope}{self.name_base}-{tenant_namespace.lower()}"


@dataclass(frozen=True)
class _TransitMember:
    """An HA member as a context leg sees it. ``offset`` is the /29 host offset (members sorted by name take .5/.6, as
    transforms/helpers/ha.py); ``bl_port`` is the border service port its uplink is cabled to.
    """

    name: str
    device_id: str
    trunk: Any
    bl_port: Any
    offset: int


@dataclass(frozen=True)
class _TransitState:
    """What a context already has of its legs (see _transit_state)."""

    context: Any | None
    complete: bool


# (physical platform name, device_role) -> virtual template name prefix (data/bootstrap 09_virtual_device_templates_*)
_VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE: dict[tuple[str, str], str] = {
    ("checkpoint_gaia", INTERNAL_FIREWALL_ROLE): "CloudGuard_EDGE",
    ("paloalto_panos", INTERNAL_FIREWALL_ROLE): "PA-VM_EDGE",
    ("juniper_junos", INTERNAL_FIREWALL_ROLE): "vSRX_EDGE",
    ("f5_tmos", "load-balancer"): "BIG-IP-VE_LOAD_BALANCER",
    ("netscaler", "load-balancer"): "NetScaler-ADC-VPX_LOAD_BALANCER",
}
