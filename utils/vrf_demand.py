"""The one derivation of the VRF instances (ManagedVRF) a device needs, and of the segment SVIs it carries.

A device has an instance exactly for the non-``default`` namespaces it routes in (demand-driven, never
pre-provisioned):

- the gateway namespace of each ROUTED segment row on its VLAN domain (a gateway, not ``terminate_inline``: an
  inline-terminated or gateway-less segment is pure L2 on the fabric), border gateways' stretched rows included;
- the VRF of each of its SVI objects (an instance is never removed while an SVI still routes in it);
- the namespace of each address on its own interfaces (handoff /31s, WAN exit sub-interfaces, the upstream
  transit address of a border gateway: a BGP session's local address is one of them);
- the leg namespaces of the firewall and load-balancer contexts tagged on its border service ports (the border leaf
  or edge holds no address of its own on a leg: utils/exchange_transit.py transit_leg_namespace).

Only our routing devices carry instances (``carries_vrfs``): never a spine, an L2 leaf, a firewall or a load balancer
(their VRF is the context), a controller, or a party device (a provider router, an ISP PE, a provider stub).

Readers: the generators (generators/helpers/vrf.py converges the instances and SVIs of the devices a run touched),
checks/vrf_instances.py (demand vs instances, every device) and the transforms (a session, SVI or leg in a VRF the
device lacks is refused). The first two read queries/validation/vrf_instances.gql through ``device_vrf_facts``; the
transform builds ``VrfDemandInputs`` from its own config query.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from utils.exchange_transit import INTERNET_TYPE, service_context_caps, transit_leg_namespace
from utils.limits import DEFAULT_NAMESPACE, GROUP_PROVIDER_STUBS
from utils.segment_addressing import routed_segment_namespace
from utils.vlan_domain import vlan_domain_id

VRF_KIND = "ManagedVRF"
SVI_ROLE = "svi"
"""DcimInterface role of a segment SVI (schemas/base/dcim.yml)."""
SVI_NAME_PREFIX = "Vlan"
"""A segment SVI is named ``Vlan<vlan_id>`` (``svi_name``)."""

VRF_DEVICE_KINDS = frozenset({"DcimPhysicalDevice", "CloudNetworkAppliance"})
"""Device kinds that can carry an instance: a physical switch or router, and our provider-hosted appliance."""

VRF_ROLES = frozenset(
    {"leaf", "tor", "access-leaf", "border-leaf", "border-spine", "edge", "access-switch", "distribution-switch"}
)
"""Device roles that route tenant VRFs. Spines, L2 leaves, firewalls and load balancers never carry one."""

PARTY_DEPLOYMENT_KINDS = frozenset({"TopologyCloudRegion", "TopologyExternalProviders"})
"""A device deployed into one of these is the party's (a cloud provider router, an ISP PE, a partner edge)."""

PARTY_GROUPS = frozenset({GROUP_PROVIDER_STUBS})
"""A provider stub modelled inside our colocation zone (an Equinix Fabric port, a Megaport MCR) is the party's too."""


def carries_vrfs(
    kind: str | None, role: str | None, groups: frozenset[str] = frozenset(), deployment_kind: str | None = None
) -> bool:
    """Whether a device can carry VRF instances: one of our routing devices, not a party's."""
    return (
        kind in VRF_DEVICE_KINDS
        and role in VRF_ROLES
        and groups.isdisjoint(PARTY_GROUPS)
        and deployment_kind not in PARTY_DEPLOYMENT_KINDS
    )


def instance_name(device_name: str, namespace_name: str) -> str:
    """The name (and HFID) of a device's instance of a namespace: ``{device}-vrf-{namespace lower}``."""
    return f"{device_name}-vrf-{namespace_name.lower()}"


def svi_name(vlan_id: int) -> str:
    """The name of a segment SVI: ``Vlan<vlan_id>`` (a row's VLAN is never re-chosen, so the name never changes)."""
    return f"{SVI_NAME_PREFIX}{vlan_id}"


def svi_vlan_id(name: str | None) -> int | None:
    """The VLAN of an SVI name ``Vlan<vlan_id>`` (``svi_name`` reversed), None for any other name."""
    suffix = (name or "").removeprefix(SVI_NAME_PREFIX)
    return int(suffix) if name and name != suffix and suffix.isdigit() else None


def address_namespaces(interfaces: Iterable[dict[str, Any]]) -> frozenset[str]:
    """The namespace names of the addresses on a device's own interfaces (cleaned query data).

    A BGP session's local address is one of them, so a session's VRF needs no source of its own.
    """
    return frozenset(
        name
        for iface in interfaces
        if (name := ((iface.get("ip_address") or {}).get("ip_namespace") or {}).get("name"))
    )


def leg_namespaces(interfaces: list[dict[str, Any]]) -> frozenset[str]:
    """The namespace names of the transit legs of the contexts tagged on a device's service ports."""
    return frozenset(
        namespace["name"]
        for ctx in service_context_caps(interfaces)
        for leg in ctx.get("capabilities") or []
        if (namespace := transit_leg_namespace(ctx.get("vlan_id"), leg))
    )


def realized_l3(namespace: Mapping[str, Any], routed: Collection[str], exits: Collection[str]) -> bool:
    """Whether a device's instance of ``namespace`` is realized as an L3 instance (L3 VNI / LISP instance-id).

    The one realization rule, for the overlay renderer (transforms/helpers/overlay.py) and checks/evpn_fabric.py:
    not ``default``, an ``l3_vni``, and the device either routes a segment or a transit leg in it (``routed``: it has
    the anycast SVI) or it is one of the device's default ``exits`` (carried with no SVI of its own). Any other
    instance is VRF-lite.
    """
    name = namespace.get("name")
    return (
        bool(name) and name != DEFAULT_NAMESPACE and bool(namespace.get("l3_vni")) and (name in routed or name in exits)
    )


def internet_exits(namespaces: Iterable[Mapping[str, Any]], *, border_gateway: bool) -> frozenset[str]:
    """The VXLAN default exits: an EVPN Multi-Site border gateway's internet-type instances (none elsewhere)."""
    if not border_gateway:
        return frozenset()
    return frozenset(ns["name"] for ns in namespaces if ns.get("namespace_type") == INTERNET_TYPE and ns.get("name"))


@dataclass(frozen=True)
class VrfDemandInputs:
    """The facts the demand is computed from; every namespace set holds namespace names."""

    kind: str | None
    role: str | None
    groups: frozenset[str] = frozenset()
    deployment_kind: str | None = None
    row_namespaces: frozenset[str] = frozenset()
    svi_namespaces: frozenset[str] = frozenset()
    address_namespaces: frozenset[str] = frozenset()
    leg_namespaces: frozenset[str] = frozenset()


def vrf_demand(inputs: VrfDemandInputs) -> frozenset[str]:
    """The namespace names the device needs an instance of; empty for a device that never carries one."""
    if not carries_vrfs(inputs.kind, inputs.role, inputs.groups, inputs.deployment_kind):
        return frozenset()
    used = inputs.row_namespaces | inputs.svi_namespaces | inputs.address_namespaces | inputs.leg_namespaces
    return frozenset(name for name in used if name and name != DEFAULT_NAMESPACE)


@dataclass(frozen=True)
class RoutedRow:
    """A routed segment row of the device's VLAN domain: the device needs its SVI and (unless default) its VRF."""

    id: str
    vlan_id: int
    namespace: str
    segment_name: str | None


@dataclass(frozen=True)
class SviFacts:
    """One role-svi interface of the device."""

    id: str
    name: str
    vrf_id: str | None
    vrf_namespace: str | None
    row_id: str | None
    has_address: bool
    has_capabilities: bool


@dataclass(frozen=True)
class InstanceFacts:
    """One ManagedVRF of the device."""

    id: str
    name: str | None
    namespace: str | None
    l3_vni: int | None
    namespace_type: str | None


@dataclass(frozen=True)
class DeviceVrfFacts:
    """Everything one device of queries/validation/vrf_instances.gql says about its VRFs and SVIs."""

    id: str
    name: str
    inputs: VrfDemandInputs
    domain_row_ids: frozenset[str]
    routed_rows: tuple[RoutedRow, ...]
    unrouted_row_ids: frozenset[str]
    svis: tuple[SviFacts, ...]
    instances: tuple[InstanceFacts, ...] = field(default=())
    # Interface name -> role of every interface that is not a segment SVI: an SVI name must not collide with one
    # (the MLAG control SVI Vlan4094, a hand-made VlanN).
    other_interfaces: Mapping[str, str | None] = field(default_factory=dict)

    @property
    def demand(self) -> frozenset[str]:
        """The namespace names the device needs an instance of."""
        return vrf_demand(self.inputs)

    @property
    def routed_namespaces(self) -> frozenset[str]:
        """The namespaces the device has an anycast SVI in: its routed rows and its transit legs."""
        return self.inputs.row_namespaces | self.inputs.leg_namespaces

    @property
    def carries_vrfs(self) -> bool:
        """Whether the device can carry instances and SVIs at all."""
        inputs = self.inputs
        return carries_vrfs(inputs.kind, inputs.role, inputs.groups, inputs.deployment_kind)

    def desired_svis(self) -> dict[str, RoutedRow]:
        """``Vlan<id>`` -> the routed row it realizes; empty for a device that carries no VRF."""
        if not self.carries_vrfs:
            return {}
        return {svi_name(row.vlan_id): row for row in sorted(self.routed_rows, key=lambda row: row.vlan_id)}


def _domain_rows(device: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows of the device's VLAN domain: its MLAG's when paired, else its own (utils/vlan_domain.py)."""
    capabilities = device.get("capabilities") or []
    domain = vlan_domain_id(device.get("id"), ((cap.get("typename"), cap.get("id")) for cap in capabilities))
    if domain != device.get("id"):
        mlag = next((cap for cap in capabilities if cap.get("id") == domain), {})
        return mlag.get("vlan_domain_segments") or []
    return device.get("vlan_domain_segments") or []


def _routed_row(row: dict[str, Any]) -> RoutedRow | None:
    """The row as a RoutedRow, or None when the fabric does not route its segment (no gateway, inline)."""
    segment = (row.get("segment_deployment") or {}).get("segment") or {}
    namespace = routed_segment_namespace(segment).get("name")
    if not namespace or not row.get("id") or not isinstance(row.get("vlan_id"), int):
        return None
    return RoutedRow(id=row["id"], vlan_id=row["vlan_id"], namespace=namespace, segment_name=segment.get("name"))


def _svi(iface: dict[str, Any]) -> SviFacts:
    vrf = iface.get("vrf") or {}
    return SviFacts(
        id=iface.get("id") or "",
        name=iface.get("name") or "",
        vrf_id=vrf.get("id"),
        vrf_namespace=(vrf.get("namespace") or {}).get("name"),
        row_id=(iface.get("vlan_domain_segment") or {}).get("id"),
        has_address=bool((iface.get("ip_address") or {}).get("address")),
        has_capabilities=bool(iface.get("capabilities")),
    )


def _instance(cap: dict[str, Any]) -> InstanceFacts:
    namespace = cap.get("namespace") or {}
    return InstanceFacts(
        id=cap.get("id") or "",
        name=cap.get("name"),
        namespace=namespace.get("name"),
        l3_vni=namespace.get("l3_vni"),
        namespace_type=namespace.get("namespace_type"),
    )


def device_vrf_facts(device: dict[str, Any]) -> DeviceVrfFacts:
    """The facts of one cleaned ``DcimDevice`` node of queries/validation/vrf_instances.gql."""
    interfaces = device.get("interfaces") or []
    rows = _domain_rows(device)
    routed = [routed_row for row in rows if (routed_row := _routed_row(row))]
    svis = tuple(_svi(iface) for iface in interfaces if iface.get("role") == SVI_ROLE)
    inputs = VrfDemandInputs(
        kind=device.get("typename"),
        role=device.get("role"),
        groups=frozenset(group["name"] for group in device.get("member_of_groups") or [] if group.get("name")),
        deployment_kind=(device.get("deployment") or {}).get("typename"),
        row_namespaces=frozenset(row.namespace for row in routed),
        svi_namespaces=frozenset(svi.vrf_namespace for svi in svis if svi.vrf_namespace),
        address_namespaces=address_namespaces(interfaces),
        leg_namespaces=leg_namespaces(interfaces),
    )
    return DeviceVrfFacts(
        id=device.get("id") or "",
        name=device.get("name") or "",
        inputs=inputs,
        domain_row_ids=frozenset(row["id"] for row in rows if row.get("id")),
        routed_rows=tuple(routed),
        unrouted_row_ids=frozenset(row["id"] for row in rows if row.get("id") and _routed_row(row) is None),
        svis=svis,
        other_interfaces={
            iface["name"]: iface.get("role")
            for iface in interfaces
            if iface.get("name") and iface.get("role") != SVI_ROLE
        },
        instances=tuple(_instance(cap) for cap in device.get("capabilities") or [] if cap.get("typename") == VRF_KIND),
    )


def namespaces_by_name(cleaned: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ``IpamNamespace`` root of the query, by name."""
    return {ns["name"]: ns for ns in cleaned.get("IpamNamespace") or [] if ns.get("name")}
