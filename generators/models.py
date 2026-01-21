# Combined Pydantic models from dc.py, pod.py, and rack.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from pydantic import BaseModel, field_validator


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


class Pool(BaseModel):
    id: str


# Site Layout model (three-layer architecture)
class SiteLayout(BaseModel):
    """TopologySiteLayout model for physical floor plan."""

    id: str
    name: Optional[str] = None
    number_of_rows: Optional[int] = 1
    racks_per_row: Optional[int] = None
    rack_numbering_scheme: Optional[str] = None
    naming_convention: Optional[str] = None


# Pod Design model (three-layer architecture)
class PodDesign(BaseModel):
    """TopologyPodDesign model for logical topology design."""

    id: str
    name: Optional[str] = None
    deployment_type: Optional[str] = None
    spine_count: Optional[int] = None  # Deprecated: use max_spine_count
    max_spine_count: Optional[int] = None
    max_leafs_per_row: Optional[int] = None
    max_tors_per_row: Optional[int] = None
    maximum_spines: Optional[int] = None  # Deprecated: use max_spine_count
    technical_pool_size: Optional[int] = None  # CIDR prefix length (e.g., 24 for /24)
    loopback_pool_size: Optional[int] = None  # CIDR prefix length (e.g., 28 for /28)
    naming_convention: Optional[str] = None


# Data Center Design model (fabric-wide architectural principles)
class DataCenterDesignData(BaseModel):
    """Data Center Design model for architectural principles."""
    strategy: Optional[str] = None
    underlay: Optional[bool] = False
    max_super_spine_count: Optional[int] = None
    max_pod_count: Optional[int] = None
    naming_convention: Optional[str] = None
    dc_loopback_parent_size: Optional[int] = None
    dc_technical_parent_size: Optional[int] = None
    loopback_prefix_size: Optional[int] = None
    p2p_prefix_size: Optional[int] = None


# DC model
class DCPod(BaseModel):
    id: str
    checksum: Optional[str] = None


class DCModel(BaseModel):
    id: str
    name: str
    index: int
    design: Optional[DataCenterDesignData] = None
    amount_of_super_spines: int
    super_spine_template: Template
    children: List[DCPod] = []


# Pod model
class PodParent(BaseModel):
    id: str
    devices: List[Device]
    name: str
    index: Optional[int] = None
    super_spine_template: Optional[Template] = None


class PodModel(BaseModel):
    id: str
    name: str
    checksum: Optional[str] = None
    index: int
    # New three-layer architecture
    site_layout: Optional[SiteLayout] = None
    design: Optional[PodDesign] = None
    # Legacy attributes (backward compatibility)
    deployment_type: Optional[str] = None
    amount_of_spines: Optional[int] = None
    number_of_rows: Optional[int] = 1
    maximum_leafs_per_row: Optional[int] = None
    maximum_tors_per_row: Optional[int] = None
    leaf_interface_sorting_method: Optional[str] = None
    spine_interface_sorting_method: Optional[str] = None
    spine_template: Optional[Template] = None
    spine_count: Optional[int] = 0
    leaf_count: Optional[int] = 0
    tor_count: Optional[int] = 0
    parent: PodParent
    loopback_pool: Optional[Pool] = None
    prefix_pool: Optional[Pool] = None

    @field_validator("site_layout", "design", "spine_template", "loopback_pool", "prefix_pool", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        """Convert {'node': None} to None, or extract node if present."""
        if isinstance(v, dict) and "node" in v:
            # Only process if it's a GraphQL node wrapper
            node = v.get("node")
            if node is None:
                return None
            return node
        # Return as-is if not a node wrapper (already cleaned by clean_data)
        return v


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
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    loopback_pool: Optional[Pool] = None
    prefix_pool: Optional[Pool] = None
    # New three-layer architecture
    site_layout: Optional[SiteLayout] = None
    design: Optional[PodDesign] = None
    # Legacy attributes (backward compatibility)
    deployment_type: str
    spine_template: Optional[Template] = None
    maximum_leafs_per_row: Optional[int] = None
    maximum_tors_per_row: Optional[int] = None

    @field_validator("site_layout", "design", "spine_template", "loopback_pool", "prefix_pool", mode="before")
    @classmethod
    def handle_empty_node(cls, v: Any) -> Any:
        """Convert {'node': None} to None, or extract node if present."""
        if isinstance(v, dict) and "node" in v:
            # Only process if it's a GraphQL node wrapper
            node = v.get("node")
            if node is None:
                return None
            # Return the node content (which should have id, etc.)
            return node
        # Return as-is if not a node wrapper (already cleaned by clean_data)
        return v


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
    pod: RackPod


# Endpoint connectivity model
class CableEndpoint(BaseModel):
    """Cable endpoint information for idempotency checks."""

    id: str
    name: str
    interface_type: Optional[str] = None
    device_id: str
    device_name: str
    device_role: Optional[str] = None


class Cable(BaseModel):
    """Cable information including both endpoints."""

    id: str
    endpoints: List[CableEndpoint] = []


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
        """Handle GraphQL response where cable is {node: None} for uncabled interfaces."""
        if isinstance(v, dict) and v.get("node") is None:
            return None
        return v


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
