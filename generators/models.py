# Combined Pydantic models from dc.py, pod.py, and rack.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, field_validator


def _unwrap_node(value: Any) -> Any:
    """Unwrap GraphQL ``{"node": X}`` wrapper, returning X (or None)."""
    if isinstance(value, dict) and "node" in value:
        return value.get("node")
    return value


# Shared models
class Platform(BaseModel):
    id: str
    name: Optional[str] = None


class DeviceType(BaseModel):
    id: str


class Interface(BaseModel):
    name: str
    role: Optional[str] = None


class Template(BaseModel):
    id: str
    platform: Optional[Platform] = None
    device_type: Optional[DeviceType] = None
    interfaces: List[Interface] = []


class DeviceRack(BaseModel):
    """Rack information for device location."""

    id: str
    index: int
    row_index: int


class Device(BaseModel):
    name: str
    role: Optional[str] = None
    rack: Optional[DeviceRack] = None  # For leaf devices in mixed deployment
    interfaces: List[Interface] = []


class SpineCable(BaseModel):
    id: str
    name: Optional[str] = None


class SpineInterface(BaseModel):
    id: str
    name: str
    cable: Optional[SpineCable] = None

    @field_validator("cable", mode="before")
    @classmethod
    def unwrap_cable(cls, v: Any) -> Any:
        if isinstance(v, dict) and "node" in v:
            return v.get("node")
        return v


class SpineDevice(BaseModel):
    id: str
    name: str
    interfaces: List[SpineInterface] = []

    @property
    def cabled_port_names(self) -> set[str]:
        """Return set of interface names that already have a cable attached."""
        return {i.name for i in self.interfaces if i.cable}


class Pool(BaseModel):
    id: str
    name: Optional[str] = None


# Pod Design model (three-layer architecture)
class PodDesign(BaseModel):
    """TopologyPodDesign model for physical floor plan.

    All numeric fields are required in the schema (``optional: false``).
    ``max_*`` fields have schema defaults; ``rows``, ``compute_racks_per_row``,
    and ``network_racks_per_row`` must be set by the user.
    """

    id: str
    name: Optional[str] = None

    # Physical layout — required in schema, no defaults
    rows: int
    compute_racks_per_row: int
    network_racks_per_row: int

    # Device density — required in schema with defaults
    max_leafs_per_network_rack: int = 4
    max_tors_per_network_rack: int = 2
    max_tors_per_compute_rack: int = 1
    max_spines_per_pod: int = 2

    @property
    def derived_deployment_type(self) -> str:
        """Derive deployment type from rack layout when not explicitly set."""
        if self.network_racks_per_row == 0:
            return "tor"
        if self.max_tors_per_compute_rack == 0:
            return "middle_rack"
        return "mixed"


# Data Center Design model (fabric-wide architectural principles)
class DataCenterDesignData(BaseModel):
    """Data Center Design model for architectural principles.

    Pool prefix lengths are auto-calculated from max_pods and underlay_protocol.
    T-shirt sizing: S(<=2 pods), M(<=4), L(<=8), XL(<=16).
    """

    id: Optional[str] = None

    # Routing architecture
    routing_strategy: str = "ebgp-ebgp"
    underlay_protocol: str = "ipv6"

    # Capacity planning
    max_pods: int = 2
    max_super_spines_per_fabric: int = 2
    max_spines_per_pod: int = 4

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


# DC model
class DCPod(BaseModel):
    id: str
    checksum: Optional[str] = None


class DCModel(BaseModel):
    id: str
    name: str
    index: int
    design: Optional[DataCenterDesignData] = None
    naming_convention: str = "standard"
    overlay_technology: str = "vxlan_evpn"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    loopback_prefix_length: int = 23
    technical_prefix_length: int = 19
    management_prefix_length: int = 25
    amount_of_super_spines: int = 0
    super_spine_template: Optional[Template] = None
    loopback_pool: Optional[Pool] = None
    technical_pool: Optional[Pool] = None
    management_pool: Optional[Pool] = None
    super_spine_asn_pool: Optional[Pool] = None
    children: List[DCPod] = []

    @field_validator(
        "loopback_pool",
        "technical_pool",
        "management_pool",
        "super_spine_asn_pool",
        "design",
        "super_spine_template",
        mode="before",
    )
    @classmethod
    def extract_node(cls, value: Any) -> Optional[Any]:
        unwrapped = _unwrap_node(value)
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped


# Pod model
class PodParent(BaseModel):
    id: str
    devices: List[Device]
    name: str
    index: int
    # Schema: optional — only set for fabrics with super-spine tier
    super_spine_template: Optional[Template] = None
    amount_of_super_spines: int = 0
    design: Optional[DataCenterDesignData] = None
    naming_convention: str = "standard"
    fabric_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    super_spine_asn_pool: Optional[Pool] = None
    management_pool: Optional[Pool] = None

    @field_validator("management_pool", "super_spine_asn_pool", "design", "super_spine_template", mode="before")
    @classmethod
    def extract_parent_node(cls, value: Any) -> Optional[Any]:
        unwrapped = _unwrap_node(value)
        # super_spine_template node wrapper may resolve to {"id": null} when not set
        if isinstance(unwrapped, dict) and unwrapped.get("id") is None:
            return None
        return unwrapped


class PodModel(BaseModel):
    """Pod instance model with capacity calculated from PodDesign.

    Pod makes DEPLOYMENT DECISIONS within constraints:
    - deployment_type: Deployment decision (from TopologyPod, default=tor)
    - amount_of_spines: Actual spine count (default=4, constrained by design.max_spines_per_pod)

    Capacity is CALCULATED from design:
    - max_leafs_per_row: design.network_racks_per_row × max_leafs_per_network_rack
    - max_tors_per_row: design.compute_racks_per_row × max_tors_per_compute_rack
    """

    id: str
    name: str
    checksum: Optional[str] = None
    index: int
    # Design relationship is optional in schema
    design: Optional[PodDesign] = None
    # Schema: optional with defaults — always provided by Infrahub
    deployment_type: Literal["middle_rack", "tor", "mixed"] = "tor"
    amount_of_spines: int = 4

    leaf_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"

    @property
    def max_leafs_per_row(self) -> int:
        """Calculate maximum leafs per row from design physical capacity."""
        if self.design:
            return self.design.network_racks_per_row * self.design.max_leafs_per_network_rack
        return 0

    @property
    def max_tors_per_row(self) -> int:
        """Calculate maximum ToRs per row from design physical capacity."""
        if self.design:
            return self.design.compute_racks_per_row * self.design.max_tors_per_compute_rack
        return 0

    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    # Schema: required relationship (optional: false)
    spine_template: Template
    parent: PodParent
    loopback_pool: Optional[Pool] = None
    prefix_pool: Optional[Pool] = None
    asn_pool: Optional[Pool] = None

    @field_validator("design", "loopback_pool", "prefix_pool", "asn_pool", "spine_template", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        return _unwrap_node(v)


# Rack model
class DeviceRole(BaseModel):
    # name: str
    role: str
    quantity: int
    template: Template


class RackParent(BaseModel):
    id: str
    name: str
    index: int
    design: Optional[DataCenterDesignData] = None
    naming_convention: str = "standard"
    management_pool: Optional[Pool] = None

    @field_validator("management_pool", "design", mode="before")
    @classmethod
    def extract_rack_parent_node(cls, value: Any) -> Optional[Any]:
        return _unwrap_node(value)


class QuantityOnly(BaseModel):
    """Minimal model for offset calculation - only quantity needed."""

    quantity: int


class SimpleRack(BaseModel):
    """Simplified rack data for offset calculation."""

    id: str
    index: int
    row_index: int
    leafs: Optional[List[QuantityOnly]] = []
    tors: Optional[List[QuantityOnly]] = []


class RackPod(BaseModel):
    id: str
    name: str
    index: int
    parent: RackParent
    amount_of_spines: int
    leaf_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    spine_interface_sorting_method: Literal["top_down", "bottom_up"] = "bottom_up"
    loopback_pool: Optional[Pool] = None
    prefix_pool: Optional[Pool] = None
    asn_pool: Optional[Pool] = None
    design: Optional[PodDesign] = None
    deployment_type: str = "tor"
    # Schema: required relationship (optional: false)
    spine_template: Template
    # Spine devices with cable info (from GQL query)
    devices: List[SpineDevice] = []

    @field_validator("design", "loopback_pool", "prefix_pool", "asn_pool", "spine_template", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        return _unwrap_node(v)


# Spine and leaf devices queried separately when needed (on-demand for specific deployment types)


class LocationSuiteModel(BaseModel):
    """LocationSuite model for rack parent hierarchy."""

    index: int  # Required for device naming
    id: Optional[str] = None
    name: Optional[str] = None
    shortname: Optional[str] = None
    suite_name: Optional[str] = None


class RackModel(BaseModel):
    id: str
    name: str
    checksum: Optional[str] = None
    index: int
    rack_type: str
    row_index: int
    parent: LocationSuiteModel
    leafs: Optional[List[DeviceRole]] = []
    tors: Optional[List[DeviceRole]] = []
    border_leafs: Optional[List[DeviceRole]] = []
    pod: RackPod


# Endpoint connectivity model
class Cable(BaseModel):
    """Cable reference (just ID — detailed data queried dynamically)."""

    id: str


class EndpointInterface(BaseModel):
    """Interface on endpoint device."""

    id: str
    name: str
    interface_type: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None
    cable: Optional[Cable] = None

    @field_validator("cable", mode="before")
    @classmethod
    def handle_null_cable(cls, v: Any) -> Any:
        return _unwrap_node(v)


class RackDevice(BaseModel):
    """Device in rack (ToR or Leaf) with interfaces."""

    id: str
    name: str
    role: Optional[str] = None
    rack_row_index: Optional[int] = None
    interfaces: List[EndpointInterface] = []


class EndpointDataCenter(BaseModel):
    """Data center information for deployment context."""

    id: str
    name: str


class EndpointPod(BaseModel):
    """Pod information for deployment context."""

    id: str
    name: str
    deployment_type: str
    index: int
    parent: EndpointDataCenter


class EndpointRack(BaseModel):
    """Rack containing endpoint device."""

    id: str
    name: str
    index: int
    row_index: int
    rack_type: str
    pod: EndpointPod
    devices: List[RackDevice] = []  # Leafs and ToRs in same rack


class EndpointDevice(BaseModel):
    """Endpoint device (server) to be connected."""

    id: str
    name: str
    role: Optional[str] = None
    rack: Optional[EndpointRack] = None
    interfaces: List[EndpointInterface] = []


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
