# Combined Pydantic models from dc.py, pod.py, and rack.py

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, field_validator


# Shared models
class Platform(BaseModel):
    id: str
    name: str | None = None


class DeviceType(BaseModel):
    id: str


class Interface(BaseModel):
    name: str
    role: str | None = None


class DesignPattern(BaseModel):
    """Data Center design pattern - DC-level parameters only."""

    maximum_super_spines: int | None = None
    maximum_pods: int | None = None
    maximum_spines: int | None = None
    maximum_switches: int | None = None
    naming_convention: str = "standard"


class Template(BaseModel):
    id: str
    platform: Platform | None = None
    device_type: DeviceType | None = None
    interfaces: list[Interface] = []


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


class Pool(BaseModel):
    id: str


# DC model
class DCPod(BaseModel):
    id: str
    checksum: str | None = None


class DCModel(BaseModel):
    id: str
    name: str
    index: int
    underlay: bool | None = False
    design_pattern: DesignPattern
    amount_of_super_spines: int
    super_spine_template: Template
    children: list[DCPod] = []


# Pod model
class PodParent(BaseModel):
    id: str
    devices: list[Device]
    name: str
    index: int | None = None
    design_pattern: DesignPattern | None = None
    super_spine_template: Template | None = None


class PodModel(BaseModel):
    id: str
    name: str
    checksum: str | None = None
    index: int
    deployment_type: str
    amount_of_spines: int
    number_of_rows: int | None = 1
    maximum_leafs_per_row: int | None = None
    maximum_tors_per_row: int | None = None
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    spine_template: Template
    spine_count: int | None = 0
    leaf_count: int | None = 0
    tor_count: int | None = 0
    parent: PodParent


# Rack model
class DeviceRole(BaseModel):
    name: str
    role: str
    quantity: int
    template: Template


class RackParent(BaseModel):
    id: str
    name: str
    index: int
    design_pattern: DesignPattern


class QuantityOnly(BaseModel):
    """Minimal model for offset calculation - only quantity needed."""

    quantity: int


class SimpleRack(BaseModel):
    """Simplified rack data for offset calculation."""

    id: str
    index: int
    row_index: int
    leafs: list[QuantityOnly] | None = []
    tors: list[QuantityOnly] | None = []


class RackPod(BaseModel):
    id: str
    name: str
    index: int
    parent: RackParent
    amount_of_spines: int
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    loopback_pool: Pool
    prefix_pool: Pool
    deployment_type: str
    spine_template: Template
    maximum_leafs_per_row: int | None = None
    maximum_tors_per_row: int | None = None
    # Spine and leaf devices queried separately when needed (on-demand for specific deployment types)


class RackModel(BaseModel):
    id: str
    name: str
    checksum: str
    index: int
    rack_type: str
    row_index: int
    leafs: list[DeviceRole] | None = []
    tors: list[DeviceRole] | None = []
    pod: RackPod


# Endpoint connectivity model
class CableEndpoint(BaseModel):
    """Cable endpoint information for idempotency checks."""

    id: str
    name: str
    interface_type: str | None = None
    device_id: str
    device_name: str
    device_role: str | None = None


class Cable(BaseModel):
    """Cable information including both endpoints."""

    id: str
    endpoints: list[CableEndpoint] = []


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
        """Handle GraphQL response where cable is {node: None} for uncabled interfaces."""
        if isinstance(v, dict) and v.get("node") is None:
            return None
        return v


class RackDevice(BaseModel):
    """Device in rack (ToR or Leaf) with interfaces."""

    id: str
    name: str
    role: str | None = None
    rack_row_index: int | None = None
    interfaces: list[EndpointInterface] = []


class EndpointPod(BaseModel):
    """Pod information for deployment context."""

    id: str
    name: str
    deployment_type: str
    index: int


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
