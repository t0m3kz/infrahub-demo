"""The records a customer's context flow passes around (firewall_context.py, firewall_transit.py, context_plan.py) and
the load-balancer context as the boarding uses it: a footprint that moves to another load-balancer context re-runs its
load-balanced components (generators/applications/lb_vip.py).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from generators.applications.lb_vip import APP_COMPONENT_GENERATOR, load_balanced_components_in
from generators.capabilities.ha import LOADBALANCER_CONTEXT, ContextSpec
from utils.fabric_roles import INTERNAL_FIREWALL_ROLE

# The VIP and its R_server rule live on the serving context; the firewall's rules are placed at render time, so nothing
# else to re-run
BOARDED_LOADBALANCER_CONTEXT = dataclasses.replace(
    LOADBALANCER_CONTEXT, relink_consumers=load_balanced_components_in, relink_generator=APP_COMPONENT_GENERATOR
)


@dataclass(frozen=True)
class ContextTarget:
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
class TransitMember:
    """An HA member as a context leg sees it. ``offset`` is the /29 host offset (members sorted by name take .5/.6, as
    transforms/helpers/ha.py); ``bl_port`` is the border service port its uplink is cabled to.
    """

    name: str
    device_id: str
    trunk: Any
    bl_port: Any
    offset: int


@dataclass(frozen=True)
class TransitState:
    """What a context already has of its legs (see transit_state)."""

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
