# Combined Pydantic models from dc.py, pod.py, and rack.py

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


# Shared models
class Platform(BaseModel):
    id: str
    name: Optional[str] = None


class DeviceType(BaseModel):
    id: str


class Interface(BaseModel):
    name: str
    role: Optional[str] = None


class DesignPattern(BaseModel):
    maximum_super_spines: Optional[int] = None
    maximum_pods: Optional[int] = None
    maximum_spines: Optional[int] = None
    maximum_leafs: Optional[int] = None
    maximum_middle_racks: Optional[int] = None
    maximum_tors: Optional[int] = None
    naming_convention: str = "flat"
    maximum_rack_leafs: Optional[int] = None


class Template(BaseModel):
    id: str
    platform: Optional[Platform] = None
    device_type: Optional[DeviceType] = None
    interfaces: List[Interface] = []


class Device(BaseModel):
    name: str


class Pool(BaseModel):
    id: str


# DC model
class DCModel(BaseModel):
    id: str
    name: str
    index: int
    underlay: Optional[bool] = False
    design_pattern: DesignPattern
    amount_of_super_spines: int
    super_spine_template: Template


# Pod model
class PodParent(BaseModel):
    id: str
    devices: List[Device]
    name: str
    index: Optional[int] = None
    design_pattern: Optional[DesignPattern] = None
    super_spine_template: Optional[Template] = None


class PodModel(BaseModel):
    id: str
    name: str
    checksum: str
    index: int
    deployment_type: str
    amount_of_spines: int
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    spine_template: Template
    spine_count: int = 0
    leaf_count: int = 0
    tor_count: int = 0
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


class RackPod(BaseModel):
    id: str
    name: str
    index: int
    devices: List[Device]
    parent: RackParent
    amount_of_spines: int
    leaf_interface_sorting_method: str
    spine_interface_sorting_method: str
    loopback_pool: Pool
    prefix_pool: Pool
    deployment_type: str
    spine_template: Template


class RackModel(BaseModel):
    id: str
    name: str
    checksum: str
    index: int
    rack_type: str
    row: str
    leafs: Optional[List[DeviceRole]] = []
    tors: Optional[List[DeviceRole]] = []
    pod: RackPod
