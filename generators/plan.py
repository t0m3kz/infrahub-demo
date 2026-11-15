"""Declarative topology intent specs produced by generators before persistence."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from generators.protocols import LoadbalancerVIP

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
class SecurityTagSpec:
    name: str
    group_id: int | None = None
    tag_id: str | None = None
    pool_id: str | None = None
    consumer_kind: Any = None
    consumer_id: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RoutingTagSpec:
    kind: str
    node_id: str
    pool_name: str
    name: str = ""


@dataclass(frozen=True)
class FirewallContextSpec:
    name: str
    kind: Any = "ManagedFirewallContext"
    firewall_ha_id: str | None = None
    tenant_id: str | None = None
    track: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadBalancerVipSpec:
    hostname: str
    kind: Any = LoadbalancerVIP
    address_id: str | None = None
    load_balancer_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SegmentGatewaySpec:
    segment_kind: str
    segment_id: str
    gateway_id: str


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
class VrfSpec:
    name: str
    rd: str | None = None
    import_targets: tuple[str, ...] = ()
    export_targets: tuple[str, ...] = ()
    tenant_id: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BgpPeeringSpec:
    name: str
    local_device: str = ""
    local_asn: int = 0
    local_ip: str = ""
    remote_asn: int = 0
    remote_ip: str = ""
    remote_device: str | None = None
    peer_group: str | None = None
    vrf: str | None = None
    password: str | None = None
    bfd: bool = False
    ebgp_multihop: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CableSpec:
    a_device: str
    a_interface: str
    b_device: str
    b_interface: str
    cable_type: str | None = None
    label: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


PlanItem = (
    SecurityTagSpec
    | RoutingTagSpec
    | FirewallContextSpec
    | LoadBalancerVipSpec
    | SegmentGatewaySpec
    | DeviceSpec
    | InterfaceSpec
    | IpSpec
    | VrfSpec
    | BgpPeeringSpec
    | CableSpec
)


@dataclass
class TopologyPlan:
    """Ordered collection of declarative specs to be persisted by PlanReconciler."""

    items: list[PlanItem] = field(default_factory=list)

    @property
    def devices(self) -> list[DeviceSpec]:
        return [i for i in self.items if isinstance(i, DeviceSpec)]

    def add(self, item: PlanItem) -> None:
        self.items.append(item)

    def add_device(self, spec: DeviceSpec) -> None:
        self.items.append(spec)

    def add_interface(self, spec: InterfaceSpec) -> None:
        self.items.append(spec)

    def add_ip(self, spec: IpSpec) -> None:
        self.items.append(spec)

    def add_cable(self, spec: CableSpec) -> None:
        self.items.append(spec)

    def extend(self, items: list[PlanItem] | tuple[PlanItem, ...]) -> None:
        self.items.extend(items)

    def __bool__(self) -> bool:
        return bool(self.items)
