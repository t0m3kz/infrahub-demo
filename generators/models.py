# Combined Pydantic models from dc.py, pod.py, and rack.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, field_validator


def _unwrap_node(value: Any) -> Any:
    """Unwrap GraphQL ``{"node": X}`` wrapper, returning X (or None)."""
    if isinstance(value, dict) and "node" in value:
        return value.get("node")
    return value


def _templates_by_role(templates: list["DeviceRole"], role: str) -> list["DeviceRole"]:
    """Filter a fabric_templates list down to one role's positive-quantity entries."""
    return [t for t in templates if t.role == role and t.quantity > 0]


# Shared models
class Platform(BaseModel):
    id: str
    name: str | None = None


class DeviceType(BaseModel):
    id: str


class Interface(BaseModel):
    name: str
    role: str | None = None


class Template(BaseModel):
    id: str
    platform: Platform | None = None
    device_type: DeviceType | None = None
    interfaces: list[Interface] = []


class DeviceRole(BaseModel):
    role: str
    quantity: int
    template: Template


class DeviceRack(BaseModel):
    """Rack information for device location."""

    id: str
    index: int
    row_index: int


class Device(BaseModel):
    name: str
    role: str | None = None
    rack: DeviceRack | None = None  # For leaf devices in mixed deployment
    interfaces: list[Interface] = []


class SpineCable(BaseModel):
    id: str
    name: str | None = None


class SpineInterface(BaseModel):
    id: str
    name: str
    cable: SpineCable | None = None

    @field_validator("cable", mode="before")
    @classmethod
    def unwrap_cable(cls, v: Any) -> Any:
        if isinstance(v, dict) and "node" in v:
            return v.get("node")
        return v


class SpineDevice(BaseModel):
    id: str
    name: str
    interfaces: list[SpineInterface] = []

    @property
    def cabled_port_names(self) -> set[str]:
        """Return set of interface names that already have a cable attached."""
        return {i.name for i in self.interfaces if i.cable}


class Pool(BaseModel):
    id: str
    name: str | None = None


# Pod Design model (three-layer architecture)
class PodDesign(BaseModel):
    """TopologyPodDesign model for physical floor plan.

    All numeric fields are required in the schema (``optional: false``).
    ``max_*`` fields have schema defaults; ``rows``, ``compute_racks_per_row``,
    and ``network_racks_per_row`` must be set by the user.
    """

    id: str
    name: str | None = None

    # Physical layout — required in schema, no defaults
    rows: int
    compute_racks_per_row: int
    network_racks_per_row: int

    # Device density — required in schema with defaults
    max_leafs_per_network_rack: int = 4
    max_tors_per_network_rack: int = 2
    max_tors_per_compute_rack: int = 1
    max_spines_per_pod: int = 2
    max_border_leafs_per_pod: int = 1

    @property
    def deployment_type(self) -> Literal["middle_rack", "tor", "mixed"]:
        """Derive deployment type from the physical rack layout.

        network_racks_per_row=0 -> tor (all compute racks with ToRs)
        max_tors_per_compute_rack=0 -> middle_rack (leafs+tors in network racks)
        both > 0 -> mixed (leafs in network racks, tors in compute racks)
        """
        if self.network_racks_per_row == 0:
            return "tor"
        if self.max_tors_per_compute_rack == 0:
            return "middle_rack"
        return "mixed"


# Data Center Design model (fabric-wide architectural principles)
class RoutingArchitectureMixin(BaseModel):
    """routing_strategy/underlay_protocol and their derived properties.

    Lives directly on the DC instance (and its Pod/Rack parent projections)
    rather than on TopologyDataCenterDesign — the Design node is a pure
    size/capacity template (S/M/L/XL), routing style is a free per-DC choice
    independent of size. See DCModel/PodParent/RackParent.
    """

    routing_strategy: str = "ebgp-ebgp"
    underlay_protocol: str = "ipv6"

    @property
    def is_ipv6(self) -> bool:
        return self.underlay_protocol == "ipv6"

    @property
    def is_dual_stack(self) -> bool:
        return self.underlay_protocol == "dual_stack"

    @property
    def p2p_ipv6(self) -> bool:
        """Whether P2P fabric links use IPv6 addressing."""
        return self.underlay_protocol in ("ipv6", "dual_stack")

    @property
    def p2p_addressing(self) -> str:
        """P2P link prefix: /31 for IPv4, /127 for IPv6/dual-stack."""
        return "/127" if self.p2p_ipv6 else "/31"


class DataCenterDesignData(BaseModel):
    """Data Center Design model — pure size/capacity template.

    Pool prefix lengths are auto-calculated from max_pods and underlay_protocol
    (which lives on the DC instance, not here — see RoutingArchitectureMixin).
    T-shirt sizing: S(<=4 pods, back-to-back), M(<=4 pods, back-to-back +
    border-leaf), L(<=8 pods, classic 3-tier), XL(<=16 pods, + hyper-spine).
    """

    id: str | None = None

    # Capacity planning
    max_pods: int = 2
    max_super_spines_per_fabric: int = 2
    max_hyper_spines_per_fabric: int = 0
    max_spines_per_pod: int = 4
    max_border_leafs_per_fabric: int = 4

    # Address space sizing — defaults used when DC instance has no pools
    loopback_prefix_length: int = 23
    technical_prefix_length: int = 19
    management_prefix_length: int = 25


# DC model
class DCPod(BaseModel):
    id: str


class DCModel(RoutingArchitectureMixin):
    id: str
    name: str
    index: int
    design: DataCenterDesignData
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    connectivity_mode: Literal["pbr", "inline"] = "pbr"
    management_mode: Literal["fully_managed", "managed_by_controller"] = "fully_managed"
    fabric_templates: list[DeviceRole] = []
    loopback_pool: Pool | None = None
    technical_pool: Pool | None = None
    management_pool: Pool | None = None
    fabric_asn_pool: Pool | None = None
    children: list[DCPod] = []

    @property
    def is_managed_by_controller(self) -> bool:
        return self.management_mode == "managed_by_controller"

    @field_validator(
        "loopback_pool",
        "technical_pool",
        "management_pool",
        "fabric_asn_pool",
        "design",
        mode="before",
    )
    @classmethod
    def extract_node(cls, value: Any) -> Any | None:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped

    @property
    def super_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "super-spine")

    @property
    def hyper_spine_templates(self) -> list[DeviceRole]:
        """DC-level hyper-spine tier — only present in XL fabrics with
        design.max_hyper_spines_per_fabric > 0 (see role: hyper-spine in
        schemas/extensions/topology/topology_dc.yml). Sits above super-spine,
        full-mesh cabled/routed by dc.py itself (unlike super-spine, which is
        cabled by pod.py against its own pod-scoped spines)."""
        return _templates_by_role(self.fabric_templates, "hyper-spine")

    @property
    def border_leaf_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-leaf")

    @property
    def firewall_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "firewall")

    @property
    def load_balancer_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "load-balancer")


# Pod model
class PodParent(RoutingArchitectureMixin):
    id: str
    devices: list[Device]
    name: str
    index: int
    # DC may have zero super-spine fabric_templates entries, never a null list
    fabric_templates: list[DeviceRole] = []
    design: DataCenterDesignData
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    connectivity_mode: Literal["pbr", "inline"] = "pbr"
    management_mode: Literal["fully_managed", "managed_by_controller"] = "fully_managed"
    fabric_asn_pool: Pool | None = None
    management_pool: Pool | None = None

    @property
    def is_managed_by_controller(self) -> bool:
        return self.management_mode == "managed_by_controller"

    @field_validator("management_pool", "fabric_asn_pool", "design", mode="before")
    @classmethod
    def extract_parent_node(cls, value: Any) -> Any | None:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped

    @property
    def super_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "super-spine")

    @property
    def border_leaf_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-leaf")

    @property
    def firewall_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "firewall")

    @property
    def load_balancer_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "load-balancer")


class PodModel(BaseModel):
    """Pod instance model with capacity calculated from PodDesign.

    Pod makes DEPLOYMENT DECISIONS within constraints:
    - amount_of_spines: Actual spine count (default=4, constrained by design.max_spines_per_pod)

    Everything else is CALCULATED from design:
    - deployment_type: design.deployment_type (derived from rack layout)
    - max_leafs_per_row: design.network_racks_per_row × max_leafs_per_network_rack
    - max_tors_per_row: design.compute_racks_per_row × max_tors_per_compute_rack
    """

    id: str
    name: str
    index: int
    design: PodDesign

    leaf_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"

    @property
    def deployment_type(self) -> Literal["middle_rack", "tor", "mixed"]:
        return self.design.deployment_type

    @property
    def max_leafs_per_row(self) -> int:
        """Calculate maximum leafs per row from design physical capacity."""
        return self.design.network_racks_per_row * self.design.max_leafs_per_network_rack

    @property
    def max_tors_per_row(self) -> int:
        """Calculate maximum ToRs per row from design physical capacity."""
        return self.design.compute_racks_per_row * self.design.max_tors_per_compute_rack

    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    # pod.py validates non-empty at generate() time; the list itself is never null
    fabric_templates: list[DeviceRole] = []
    parent: PodParent
    loopback_pool: Pool | None = None
    prefix_pool: Pool | None = None
    asn_pool: Pool | None = None

    @field_validator("design", "loopback_pool", "prefix_pool", "asn_pool", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        return _unwrap_node(v)

    @property
    def spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "spine")

    @property
    def border_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-spine")

    @property
    def firewall_templates(self) -> list[DeviceRole]:
        """This pod's own firewall(s) — only used in border-spine micro-fabric
        mode (see border_spine_templates); a pod with a plain spine tier has
        no firewall of its own, that lives DC-wide on dc.py's border-leaf."""
        return _templates_by_role(self.fabric_templates, "firewall")

    @property
    def load_balancer_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "load-balancer")

    @property
    def spine_slot_templates(self) -> list[DeviceRole]:
        """Whichever of spine/border-spine fills this pod's spine slot — the
        two are mutually exclusive per pod (see role: border-spine in
        schemas/extensions/topology/topology_dc.yml)."""
        return self.spine_templates or self.border_spine_templates

    @property
    def spine_slot_role(self) -> Literal["spine", "border-spine"]:
        return "border-spine" if self.border_spine_templates else "spine"


# Rack model
class RackParent(RoutingArchitectureMixin):
    id: str
    name: str
    index: int
    design: DataCenterDesignData
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    management_pool: Pool | None = None
    connectivity_mode: Literal["pbr", "inline"] = "pbr"
    management_mode: Literal["fully_managed", "managed_by_controller"] = "fully_managed"

    @property
    def is_managed_by_controller(self) -> bool:
        return self.management_mode == "managed_by_controller"

    @field_validator("management_pool", "design", mode="before")
    @classmethod
    def extract_rack_parent_node(cls, value: Any) -> Any | None:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped


class RackPod(BaseModel):
    id: str
    name: str
    index: int
    parent: RackParent
    leaf_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    mlag_create: Literal["no", "back-to-back", "virtual"] = "no"
    loopback_pool: Pool | None = None
    prefix_pool: Pool | None = None
    asn_pool: Pool | None = None
    design: PodDesign
    # pod.py validates non-empty at generate() time; the list itself is never null
    fabric_templates: list[DeviceRole] = []
    # Spine devices with cable info (from GQL query)
    devices: list[SpineDevice] = []

    @property
    def deployment_type(self) -> Literal["middle_rack", "tor", "mixed"]:
        return self.design.deployment_type

    @property
    def spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "spine")

    @property
    def border_spine_templates(self) -> list[DeviceRole]:
        return _templates_by_role(self.fabric_templates, "border-spine")

    @property
    def spine_slot_templates(self) -> list[DeviceRole]:
        """Whichever of spine/border-spine fills this pod's spine slot — see
        PodModel.spine_slot_templates for why the two are mutually exclusive."""
        return self.spine_templates or self.border_spine_templates

    @property
    def spine_slot_role(self) -> Literal["spine", "border-spine"]:
        return "border-spine" if self.border_spine_templates else "spine"

    @field_validator("design", "loopback_pool", "prefix_pool", "asn_pool", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        return _unwrap_node(v)


# Spine and leaf devices queried separately when needed (on-demand for specific deployment types)


class LocationSuiteModel(BaseModel):
    """LocationSuite model for rack parent hierarchy."""

    index: int  # Required for device naming
    id: str | None = None
    name: str | None = None
    shortname: str | None = None
    suite_name: str | None = None


class RackModel(BaseModel):
    id: str
    name: str
    index: int
    rack_type: str
    row_index: int
    parent: LocationSuiteModel
    leafs: list[DeviceRole] = []
    tors: list[DeviceRole] = []
    l2_leafs: list[DeviceRole] = []
    access_leafs: list[DeviceRole] = []
    pod: RackPod


# Endpoint connectivity model
class Cable(BaseModel):
    """Cable reference (just ID — detailed data queried dynamically)."""

    id: str


class EndpointInterface(BaseModel):
    """Interface on endpoint device."""

    id: str
    name: str
    interface_type: str | None = None
    role: str | None = None
    status: str | None = None
    cable: Cable | None = None

    @field_validator("cable", mode="before")
    @classmethod
    def handle_null_cable(cls, v: Any) -> Any:
        return _unwrap_node(v)


class RackDevice(BaseModel):
    """Device in rack (ToR or Leaf) with interfaces."""

    id: str
    name: str
    role: str | None = None
    rack_row_index: int | None = None
    interfaces: list[EndpointInterface] = []


class EndpointDataCenter(BaseModel):
    """Data center information for deployment context."""

    id: str
    name: str


class EndpointPod(BaseModel):
    """Pod information for deployment context."""

    id: str
    name: str
    design: PodDesign
    index: int
    parent: EndpointDataCenter

    @field_validator("design", mode="before")
    @classmethod
    def handle_empty_design(cls, v: Any) -> Any:
        return _unwrap_node(v)

    @property
    def deployment_type(self) -> Literal["middle_rack", "tor", "mixed"]:
        return self.design.deployment_type


class EndpointRack(BaseModel):
    """Rack containing endpoint device."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str
    pod: EndpointPod
    devices: list[RackDevice] = []  # Leafs and ToRs in same rack


class EndpointDevice(BaseModel):
    """Endpoint device (server) to be connected."""

    id: str
    name: str
    role: str | None = None
    rack: EndpointRack | None = None
    interfaces: list[EndpointInterface] = []


class EndpointModel(BaseModel):
    """Complete endpoint connectivity model."""

    endpoint: EndpointDevice


# Endpoint connectivity models
@dataclass(frozen=True)
class ConnectionFingerprint:
    """Unique identifier for a server-to-switch connection.

    Provides idempotency by uniquely identifying each connection regardless
    of execution order or multiple generator runs.
    """

    server_name: str
    server_interface: str
    switch_name: str
    switch_interface: str

    def __hash__(self) -> int:
        return hash((self.server_name, self.server_interface, self.switch_name, self.switch_interface))

    def __repr__(self) -> str:
        return f"{self.server_name}:{self.server_interface} → {self.switch_name}:{self.switch_interface}"
