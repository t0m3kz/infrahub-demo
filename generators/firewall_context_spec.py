"""What a kind of service context differs in, and the records a context flow passes around.

ContextSpec (FIREWALL_CONTEXT, LOADBALANCER_CONTEXT) is the per-kind configuration of
generators/firewall_context.py's FirewallContextMixin and generators/firewall_transit.py's
TransitLegsMixin; _ContextTarget, _TransitMember and _TransitState are the values they hand to
each other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from utils.exchange_transit import EXCHANGE_PEERS, lb_leg_pool_name, lb_vip_pool_name, transit_pool_name
from utils.limits import FW_CONTEXT_VLAN_END, FW_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END, LB_CONTEXT_VLAN_START

from .lb_vip import APP_COMPONENT_GENERATOR, load_balanced_components_in
from .protocols import ManagedFirewallContext, ManagedFirewallHA, ManagedLoadbalancerContext, ManagedLoadbalancerHA


def _firewall_leg_types(tenant_type: str) -> tuple[str, ...]:
    """The tenant VRF and every VRF it exchanges with (EXCHANGE_PEERS), tenant first."""
    return (tenant_type, *EXCHANGE_PEERS[tenant_type])


def _no_skip(_parent: dict[str, Any]) -> str | None:
    """Every parent with devices of the role gets the context."""
    return None


def _inline_parent(parent: dict[str, Any]) -> str | None:
    """Why an inline DC gets no load-balancer context: its load balancers sit in the firewall chain
    (CablingMixin._cable_border_services), so no uplink faces a border load-balancer service port."""
    if parent.get("connectivity_mode") == "inline":
        return "its load balancers are inline behind the firewall (connectivity_mode inline)"
    return None


@dataclass(frozen=True)
class ContextSpec:
    """What a kind of service context differs in; everything else is one flow.

    key prefixes the context's resource lock (`{key}-{context}`) and names the
    parent's VLAN pool (`{parent}-{key}-vlan-pool`, PoolMixin). leg_types maps
    the tenant namespace type to the namespace types of the context's legs,
    tenant first; the others are exchanged with it. leg_identifier ends a
    leg's allocation identifier (`{context_id}-{namespace}-{leg_identifier}`).
    shared_suffix names a shared context from the tenant namespace name
    (`{cluster}-{suffix}`); a dedicated one is always `{cluster}-context`.
    vlan_range bounds the parent's VLAN pool. skip_reason reads the parent
    (customer.parent of the boarding query) and returns why it gets no
    context of this kind, or None. vip_pool_name, when set, gives the context
    a VIP prefix from that pool of the tenant namespace. relink_consumers,
    when set, lists the nodes whose generated output sits on the context a
    deployment is served by (client, deployment id -> node ids):
    link_serving_context runs relink_generator for them when the deployment
    moves from one context to another.
    """

    label: str
    role: str
    devices_key: str
    dedicated_flag: str
    ha_kind: type[Any]
    context_kind: type[Any]
    key: str
    serving_field: str
    leg_types: Callable[[str], tuple[str, ...]]
    leg_pool_name: Callable[[str], str]
    leg_identifier: str
    shared_suffix: Callable[[str], str]
    vlan_range: tuple[int, int]
    skip_reason: Callable[[dict[str, Any]], str | None] = _no_skip
    vip_pool_name: Callable[[str], str] | None = None
    relink_consumers: Callable[[Any, str], Awaitable[list[str]]] | None = None
    relink_generator: str = ""


FIREWALL_CONTEXT = ContextSpec(
    label="FirewallContext",
    role="firewall",
    devices_key="firewall_devices",
    dedicated_flag="dedicated_firewall",
    ha_kind=ManagedFirewallHA,
    context_kind=ManagedFirewallContext,
    key="fw-context",
    serving_field="serving_firewall_context",
    leg_types=_firewall_leg_types,
    leg_pool_name=transit_pool_name,
    leg_identifier="transit",
    shared_suffix=lambda _namespace: "shared",
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
    leg_types=lambda tenant_type: (tenant_type,),
    leg_pool_name=lb_leg_pool_name,
    leg_identifier="lb-leg",
    shared_suffix=lambda namespace: namespace,
    vlan_range=(LB_CONTEXT_VLAN_START, LB_CONTEXT_VLAN_END),
    skip_reason=_inline_parent,
    vip_pool_name=lb_vip_pool_name,
    # A VIP and its R_server rule live on the serving context and the callers'
    # R_client rules name the VIP; the firewall's rules are placed by the
    # transform at render time, so FIREWALL_CONTEXT has nothing to re-run.
    relink_consumers=load_balanced_components_in,
    relink_generator=APP_COMPONENT_GENERATOR,
)


@dataclass(frozen=True)
class _ContextTarget:
    """Where a customer's context lives and who owns its writes."""

    spec: ContextSpec
    cluster_name: str
    cluster_id: str
    tenant_id: str | None
    track: bool
    devices: list[Any]
    parent_name: str

    def context_name(self, tenant_namespace: str) -> str:
        """The context's name: `{cluster}-context` when dedicated (track), else the spec's shared name."""
        if self.track:
            return f"{self.cluster_name}-context"
        return f"{self.cluster_name}-{self.spec.shared_suffix(tenant_namespace)}"


@dataclass(frozen=True)
class _TransitMember:
    """An HA member as a context leg sees it.

    offset is the member's /29 host offset: members sorted by name take
    .5 / .6, the order transforms/helpers/ha.py uses. bl_port is the border
    service port its uplink is cabled to, fetched with its capabilities.
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
    untagged_port_ids: list[str]


# (physical template's platform name, device_role) -> matching virtual
# template's name prefix — see data/bootstrap's 09_virtual_device_templates_
# *.yaml, where every platform/role combination provides a _CUSTOMER_<design>
# variant per DC fabric design (e.g. "CloudGuard_EDGE_CUSTOMER_L").
_VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE: dict[tuple[str, str], str] = {
    ("checkpoint_gaia", "firewall"): "CloudGuard_EDGE",
    ("paloalto_panos", "firewall"): "PA-VM_EDGE",
    ("juniper_junos", "firewall"): "vSRX_EDGE",
    ("f5_tmos", "load-balancer"): "BIG-IP-VE_LOAD_BALANCER",
    ("netscaler", "load-balancer"): "NetScaler-ADC-VPX_LOAD_BALANCER",
}
