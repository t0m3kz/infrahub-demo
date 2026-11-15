"""Declarative topology intent specs produced by generators before persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

InterfaceFlavor = Literal["physical", "loopback", "lag", "svi", "subinterface", "virtual"]

INTERFACE_KIND_BY_FLAVOR: dict[InterfaceFlavor, str] = {
    "physical": "DcimPhysicalInterface",
    "loopback": "DcimVirtualInterface",
    "lag": "DcimLagInterface",
    "svi": "DcimVirtualInterface",
    "subinterface": "DcimSubInterface",
    "virtual": "DcimVirtualInterface",
}


@dataclass(frozen=True)
class DeviceSpec:
    name: str
    kind: Any = "DcimPhysicalDevice"
    template_name: str | None = None
    discover_interface_kind: Any = None
    discover_interface_role: str | None = None
    device_type: str | None = None
    role: str | None = None
    status: str = "active"
    owner_id: str | None = None
    deployment_id: str | None = None
    rack_id: str | None = None
    rack_unit: int | None = None
    platform_id: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InterfaceSpec:
    device: str
    name: str
    flavor: InterfaceFlavor = "physical"
    role: str | None = None
    mtu: int | None = None
    enabled: bool = True
    vlan_id: int | None = None
    parent_interface: str | None = None
    lag_parent: str | None = None
    vrf_id: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IpSpec:
    device: str
    interface: str
    pool: str | None = None
    address: str | None = None
    prefix_length: int | None = None
    namespace_id: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class CableSpec:
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str
    cable_type: str | None = None


@dataclass
class TopologyPlan:
    devices: list[DeviceSpec] = field(default_factory=list)
    interfaces: list[InterfaceSpec] = field(default_factory=list)
    ips: list[IpSpec] = field(default_factory=list)
    cables: list[CableSpec] = field(default_factory=list)

    def add_device(self, spec: DeviceSpec) -> str:
        self.devices.append(spec)
        return spec.name

    def add_interface(self, spec: InterfaceSpec) -> str:
        self.interfaces.append(spec)
        return f"{spec.device}:{spec.name}"

    def add_ip(self, spec: IpSpec) -> None:
        self.ips.append(spec)

    def add_cable(self, spec: CableSpec) -> None:
        self.cables.append(spec)
