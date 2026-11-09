"""The one derivation of the VRF instances (ManagedVRF) a device needs, and of the segment SVIs it carries.

An instance is one namespace realized in one topology: the device's hosting deployment (a DC, a colocation metro or
an office, utils/segment_addressing.py ``hosting_deployment``). A device is a member of its topology's instance of
exactly the non-``default`` namespaces it routes in (demand-driven, never pre-provisioned):

- the gateway namespace of each ROUTED segment row of the device (its ``vlans``; a
  gateway-less or firewall-terminated segment, ``terminated_on``, is pure L2 on the fabric, and so is a stretched
  segment on a border leaf, utils/segment_addressing.py ``l2_only_on``), border gateways' stretched rows included;
- the VRF of each of its SVI objects (an instance is never removed while an SVI still routes in it);
- the namespace of each address on its own interfaces (handoff /31s, WAN exit sub-interfaces, the upstream
  transit address of a border gateway: a BGP session's local address is one of them);
- the leg namespaces of the firewall and load-balancer contexts tagged on its border service ports (the border leaf
  or edge holds no address of its own on a leg: utils/exchange_transit.py transit_leg_namespace).

Only our routing devices are members (``carries_vrfs``): never a spine, an L2 leaf, a firewall or a load balancer
(their VRF is the context), a controller, or a party device (a provider router, an ISP PE, a provider stub).

Readers: the generators (generators/helpers/vrf.py converges the instances and SVIs of the devices a run touched),
checks/vrf_instances.py (demand vs instances, every device) and the transforms (a session, SVI or leg in a VRF the
device lacks is refused). The first two read queries/validation/vrf_instances.gql through ``device_vrf_facts``; the
transform builds ``VrfDemandInputs`` from its own config query.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

from utils.exchange_transit import (
    INTERNET_TYPE,
    l3vni_svi_vlan,
    transit_leg_namespace,
)
from utils.fabric_roles import BORDER_ROLES
from utils.limits import DEFAULT_NAMESPACE, GROUP_PROVIDER_STUBS
from utils.routing_domain import VRF_KIND, interface_security_zone, interface_vrfs
from utils.segment_addressing import hosting_deployment, l2_only_on, routed_segment_namespace
from utils.service_contexts import border_contexts, service_footprints

PBR_KIND = "ManagedPBR"
"""The individual device capability whose presence says the device steers its tenant SVIs to the firewall."""
SVI_ROLE = "svi"
L3VNI_ROLE = "l3vni"
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
"""A device deployed into one of these is the party's (a cloud provider's service-edge appliance, an ISP PE)."""

PARTY_GROUPS = frozenset({GROUP_PROVIDER_STUBS})
"""A provider stub modelled inside our colocation zone (an Equinix Fabric port, a Megaport MCR) is the party's too."""


DECOMMISSIONED_STATUS = "decommissioned"
"""DcimDevice status of a device taken out of service (generators/decommission/pod.py): it carries nothing."""


def carries_vrfs(
    kind: str | None,
    role: str | None,
    groups: frozenset[str] = frozenset(),
    deployment_kind: str | None = None,
    status: str | None = None,
) -> bool:
    """Whether a device can carry VRF instances: one of our routing devices in service, not a party's.

    A decommissioned device is no carrier: it leaves its instances, its SVIs go, and nothing is recreated for it even
    though its VLAN-domain rows still exist (the config queries do not select ``status``: a rendered device is live).
    """
    return (
        status != DECOMMISSIONED_STATUS
        and kind in VRF_DEVICE_KINDS
        and role in VRF_ROLES
        and groups.isdisjoint(PARTY_GROUPS)
        and deployment_kind not in PARTY_DEPLOYMENT_KINDS
    )


def instance_name(hosting_name: str, namespace_name: str) -> str:
    """The name of a topology's instance of a namespace: ``{hosting deployment lower}-vrf-{namespace lower}``
    (dc10-vrf-prod). Its HFID is [deployment name, namespace name]."""
    return f"{hosting_name.lower()}-vrf-{namespace_name.lower()}"


def svi_name(vlan_id: int) -> str:
    """The name of a segment SVI: ``Vlan<vlan_id>`` (a row's VLAN is never re-chosen, so the name never changes)."""
    return f"{SVI_NAME_PREFIX}{vlan_id}"


def svi_vlan_id(name: str | None) -> int | None:
    """The VLAN of an SVI name ``Vlan<vlan_id>`` (``svi_name`` reversed), None for any other name."""
    suffix = (name or "").removeprefix(SVI_NAME_PREFIX)
    return int(suffix) if name and name != suffix and suffix.isdigit() else None


def address_namespaces(interfaces: Iterable[dict[str, Any]]) -> frozenset[str]:
    """The namespace names of the addresses on a device's own interfaces (cleaned query data).

    A BGP session's local address is one of them, so a session's VRF needs no source of its own. A segment SVI is not
    read: its address is the segment's anycast gateway, and its VRF is a source of its own (``svi_namespaces``).
    """
    return frozenset(
        name
        for iface in interfaces
        if iface.get("role") != SVI_ROLE and (iface.get("ip_address") or {}).get("address")
        if (name := interface_security_zone(iface).get("name"))
    )


def leg_namespaces(contexts: list[dict[str, Any]]) -> frozenset[str]:
    """The namespace names of the transit legs of the contexts tagged on a device's service ports.

    ``contexts`` are the device's ``utils.service_contexts.border_contexts`` (each with its ``legs``)."""
    return frozenset(
        namespace["name"]
        for ctx in contexts
        for leg in ctx.get("legs") or []
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
    return frozenset(ns["name"] for ns in namespaces if ns.get("vrf_type") == INTERNET_TYPE and ns.get("name"))


@dataclass(frozen=True)
class VrfDemandInputs:
    """The facts the demand is computed from; every namespace set holds namespace names."""

    kind: str | None
    role: str | None
    groups: frozenset[str] = frozenset()
    deployment_kind: str | None = None
    status: str | None = None
    row_namespaces: frozenset[str] = frozenset()
    svi_namespaces: frozenset[str] = frozenset()
    address_namespaces: frozenset[str] = frozenset()
    leg_namespaces: frozenset[str] = frozenset()


def vrf_demand(inputs: VrfDemandInputs) -> frozenset[str]:
    """The namespace names the device needs an instance of; empty for a device that never carries one."""
    if not carries_vrfs(inputs.kind, inputs.role, inputs.groups, inputs.deployment_kind, inputs.status):
        return frozenset()
    used = inputs.row_namespaces | inputs.svi_namespaces | inputs.address_namespaces | inputs.leg_namespaces
    return frozenset(name for name in used if name and name != DEFAULT_NAMESPACE)


@dataclass(frozen=True)
class Footprint:
    """A customer footprint of a segment: where it stands (``parent_id``, its DC or metro) and whether a firewall
    context serves it (an SVI of the segment is steered to that context)."""

    id: str
    name: str | None
    parent_id: str | None
    has_context: bool


@dataclass(frozen=True)
class RoutedRow:
    """A routed segment row of the device: the device needs its SVI and (unless default) its VRF."""

    id: str
    vlan_id: int
    namespace: str
    segment_name: str | None
    footprints: tuple[Footprint, ...] = ()
    # The segment's anycast gateway (``segment.gateway``, an IpamIPAddress): the address its SVI carries.
    gateway_id: str | None = None


@dataclass(frozen=True)
class SviFacts:
    """One role-svi interface of the device. It has no link to its row: the row it realizes is the routed row of
    the device whose ``svi_name`` it bears (``DeviceVrfFacts.desired_svis``)."""

    id: str
    name: str
    vrf_id: str | None
    vrf_namespace: str | None
    vrf_deployment_id: str | None
    address_id: str | None
    has_capabilities: bool


@dataclass(frozen=True)
class InstanceFacts:
    """One ManagedVRF instance: a namespace realized in one topology (``deployment_id``)."""

    id: str
    name: str | None
    namespace: str | None
    l3_vni: int | None
    namespace_type: str | None
    deployment_id: str | None = None
    member_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DeviceVrfFacts:
    """Everything one device of queries/validation/vrf_instances.gql says about its VRFs and SVIs."""

    id: str
    name: str
    inputs: VrfDemandInputs
    routed_rows: tuple[RoutedRow, ...]
    svis: tuple[SviFacts, ...]
    # The instances the device is a member of (``device.shared_capabilities``, the shared device edge), of its own topology or, after a move, another one.
    instances: tuple[InstanceFacts, ...] = field(default=())
    # The device's hosting deployment (utils/segment_addressing.py hosting_deployment): where its instances live.
    hosting_id: str | None = None
    hosting_name: str | None = None
    # The footprints served by the firewall contexts tagged on the device's service ports, per tenant VRF of their legs:
    # (namespace name, namespace type, footprint ids). The L3 VNI SVI of that VRF steers the traffic of each.
    service_footprints: tuple[tuple[str, str, frozenset[str]], ...] = field(default=())
    # The role-l3vni interfaces of the device (the L3 VNI SVI of each VRF it terminates; none when the writer has not run).
    l3vni_svis: tuple[SviFacts, ...] = field(default=())
    # Interface name -> role of every interface that is not a segment SVI: an SVI name must not collide with one
    # (the MLAG control SVI Vlan4094, a hand-made VlanN).
    other_interfaces: Mapping[str, str | None] = field(default_factory=dict)
    # The ids of the device's ManagedPBR capabilities (its individual ``capabilities``): one when it steers.
    pbr_ids: tuple[str, ...] = field(default=())
    # Interface name -> its instances, for an interface in more than one VRF (``vrf_conflicts``).
    vrf_conflicts: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @cached_property
    def demand(self) -> frozenset[str]:
        """The namespace names the device needs an instance of (computed once)."""
        return vrf_demand(self.inputs)

    @property
    def carries_vrfs(self) -> bool:
        """Whether the device can carry instances and SVIs at all (in service, one of our routing devices)."""
        inputs = self.inputs
        return carries_vrfs(inputs.kind, inputs.role, inputs.groups, inputs.deployment_kind, inputs.status)

    def desired_l3vni_svis(self, namespaces: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
        """``Vlan<slot>`` -> the namespace whose L3 VNI the device terminates, for a device that carries VRFs.

        A VRF is terminated (symmetric IRB: an L3 VNI SVI with ``ip forward``) where the device routes a segment or a
        transit leg in it, the same realization the overlay renders (transforms/helpers/overlay.py ``_l3_instances``); a
        namespace with no ``l3_vni`` or no slot for its type has none. The name is the fixed slot of the namespace type
        (``utils.exchange_transit.l3vni_svi_vlan``), so a VRF keeps it whatever else the device carries."""
        if not self.carries_vrfs:
            return {}
        desired: dict[str, str] = {}
        for name in sorted((self.inputs.row_namespaces | self.inputs.leg_namespaces) - {DEFAULT_NAMESPACE}):
            namespace = namespaces.get(name) or {}
            vlan = l3vni_svi_vlan(namespace.get("vrf_type"))
            if namespace.get("l3_vni") and vlan is not None:
                desired[svi_name(vlan)] = name
        return desired

    def pbr_overlay_footprints_by_name(self) -> dict[str, frozenset[str]]:
        """``Vlan<slot>`` of an L3 VNI SVI -> the footprints whose traffic enters the VRF through it and is steered to
        their firewall (PBR of the overlay ingress), for a border leaf or border spine.

        Only a border role has the firewall contexts on its service ports; the footprints are the ones its contexts
        serve. A device with no L3 VNI slot for the namespace type steers nothing there."""
        if self.inputs.role not in BORDER_ROLES:
            return {}
        result: dict[str, frozenset[str]] = {}
        for _name, kind, footprints in self.service_footprints:
            vlan = l3vni_svi_vlan(kind)
            if vlan is not None and footprints:
                result[svi_name(vlan)] = result.get(svi_name(vlan), frozenset()) | footprints
        return result

    def pbr_footprints_by_name(self) -> dict[str, str | None]:
        """``Vlan<id>`` -> the footprint the SVI of that name is steered for (to its firewall), None when it is not.

        An SVI is steered for the footprint of its segment that stands in the device's hosting deployment (its DC or
        metro) and has a firewall context; none, or no such footprint, means no steering. The lowest id decides when a
        segment has two footprints there (the same every run). Keyed by name because an SVI the run has just created
        has no id in these facts.

        A border leaf or border spine steers nothing: its templates render no PBR (and its transform refuses a PBR rule,
        transforms/common.py), so it gets no ``ManagedPBR``. The segment SVI of a border leaf is steered once those
        templates render it."""
        if self.inputs.role in BORDER_ROLES:
            return dict.fromkeys(self.desired_svis())
        return {
            name: next(
                iter(sorted(fp.id for fp in row.footprints if fp.has_context and fp.parent_id == self.hosting_id)), None
            )
            for name, row in self.desired_svis().items()
        }

    def steers(self) -> bool:
        """Whether the device steers a tenant SVI to a firewall, so it must have a ``ManagedPBR``."""
        return any(self.pbr_footprints_by_name().values())

    def desired_svis(self) -> dict[str, RoutedRow]:
        """``Vlan<id>`` -> the routed row it realizes; empty for a device that carries no VRF."""
        if not self.carries_vrfs:
            return {}
        return {svi_name(row.vlan_id): row for row in sorted(self.routed_rows, key=lambda row: row.vlan_id)}


def _routed_row(row: dict[str, Any], role: str | None) -> RoutedRow | None:
    """The row as a RoutedRow, or None when the device of ``role`` does not route its segment (no gateway, terminated,
    or a stretched segment on a border leaf: ``l2_only_on``)."""
    segment = (row.get("segment_deployment") or {}).get("segment") or {}
    if l2_only_on(role, segment):
        return None
    namespace = routed_segment_namespace(segment).get("name")
    if not namespace or not row.get("id") or not isinstance(row.get("vlan_id"), int):
        return None
    return RoutedRow(
        id=row["id"],
        vlan_id=row["vlan_id"],
        namespace=namespace,
        segment_name=segment.get("name"),
        gateway_id=(segment.get("gateway") or {}).get("id"),
        footprints=tuple(
            Footprint(
                id=fp["id"],
                name=fp.get("name"),
                parent_id=(fp.get("parent") or {}).get("id"),
                has_context=bool(fp.get("serving_firewall_contexts")),
            )
            for fp in segment.get("customer_deployments") or []
            if fp.get("id")
        ),
    )


def _vrf_label(vrf: Mapping[str, Any]) -> str:
    """An instance as a person reads it: ``<deployment>/<zone>`` (its HFID), else its id."""
    deployment, zone = (vrf.get("deployment") or {}).get("name"), (vrf.get("security_zone") or {}).get("name")
    return f"{deployment}/{zone}" if deployment and zone else str(vrf.get("id") or "?")


def vrf_conflicts(interfaces: Iterable[dict[str, Any]]) -> dict[str, tuple[str, ...]]:
    """Interface name -> the instances of an interface that is in more than one VRF (an interface is in at most one)."""
    return {
        str(iface.get("name")): tuple(sorted(_vrf_label(vrf) for vrf in vrfs))
        for iface in interfaces
        if len(vrfs := interface_vrfs(iface)) > 1
    }


def _svi(iface: dict[str, Any]) -> SviFacts:
    vrf = next(iter(interface_vrfs(iface)), {})
    capabilities = iface.get("capabilities") or []
    return SviFacts(
        id=iface.get("id") or "",
        name=iface.get("name") or "",
        vrf_id=vrf.get("id"),
        vrf_namespace=(vrf.get("security_zone") or {}).get("name"),
        vrf_deployment_id=(vrf.get("deployment") or {}).get("id"),
        address_id=(iface.get("ip_address") or {}).get("id"),
        # Its VRF instance is the only capability an SVI may have (rule S4 judges the rest).
        has_capabilities=any(cap.get("typename") != VRF_KIND for cap in capabilities),
    )


def device_memberships(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The ManagedVRF nodes of a cleaned device: its ``shared_capabilities`` (the shared device edge) of kind ManagedVRF.

    The one filter on the typename, for the transforms' ``device_vrfs`` and the generators' and checks' facts: another
    shared kind on the same edge (an SNMP profile, later) is never a VRF.
    """
    return [cap for cap in node.get("shared_capabilities") or [] if cap.get("typename") == VRF_KIND]


def instance_facts(vrf: dict[str, Any]) -> InstanceFacts:
    """One cleaned ManagedVRF node (a ``shared_capabilities`` entry of a device, or the ``ManagedVRF`` root of vrf_instances.gql)."""
    namespace = vrf.get("security_zone") or {}
    return InstanceFacts(
        id=vrf.get("id") or "",
        name=vrf.get("name"),
        namespace=namespace.get("name"),
        l3_vni=namespace.get("l3_vni"),
        namespace_type=namespace.get("vrf_type"),
        deployment_id=(vrf.get("deployment") or {}).get("id"),
        member_ids=frozenset(device["id"] for device in vrf.get("shared_devices") or [] if device.get("id")),
    )


def device_vrf_facts(device: dict[str, Any]) -> DeviceVrfFacts:
    """The facts of one cleaned ``DcimDevice`` node of queries/validation/vrf_instances.gql."""
    interfaces = device.get("interfaces") or []
    conflicts = vrf_conflicts(interfaces)
    rows = device.get("vlans") or []
    routed = [routed_row for row in rows if (routed_row := _routed_row(row, device.get("role")))]
    svis = tuple(_svi(iface) for iface in interfaces if iface.get("role") == SVI_ROLE)
    l3vni_svis = tuple(_svi(iface) for iface in interfaces if iface.get("role") == L3VNI_ROLE)
    hosting = hosting_deployment(device.get("deployment")) or {}
    inputs = VrfDemandInputs(
        kind=device.get("typename"),
        role=device.get("role"),
        groups=frozenset(group["name"] for group in device.get("member_of_groups") or [] if group.get("name")),
        deployment_kind=(device.get("deployment") or {}).get("typename"),
        status=device.get("status"),
        row_namespaces=frozenset(row.namespace for row in routed),
        svi_namespaces=frozenset(svi.vrf_namespace for svi in svis if svi.vrf_namespace),
        # An interface in two VRFs is reported (checks/vrf_instances.py), not read: its routing table is ambiguous.
        address_namespaces=address_namespaces(iface for iface in interfaces if str(iface.get("name")) not in conflicts),
        leg_namespaces=leg_namespaces(border_contexts(device)),
    )
    return DeviceVrfFacts(
        id=device.get("id") or "",
        name=device.get("name") or "",
        inputs=inputs,
        routed_rows=tuple(routed),
        svis=svis,
        l3vni_svis=l3vni_svis,
        service_footprints=service_footprints(device),
        other_interfaces={
            iface["name"]: iface.get("role")
            for iface in interfaces
            if iface.get("name") and iface.get("role") not in (SVI_ROLE, L3VNI_ROLE)
        },
        instances=tuple(instance_facts(vrf) for vrf in device_memberships(device)),
        vrf_conflicts=conflicts,
        pbr_ids=tuple(
            sorted(
                cap["id"]
                for cap in device.get("capabilities") or []
                if cap.get("typename") == PBR_KIND and cap.get("id")
            )
        ),
        hosting_id=hosting.get("id"),
        hosting_name=hosting.get("name"),
    )


def all_instances(cleaned: dict[str, Any]) -> tuple[InstanceFacts, ...]:
    """The ``ManagedVRF`` root of the query: every instance, with its topology and members."""
    return tuple(instance_facts(vrf) for vrf in cleaned.get(VRF_KIND) or [])


def namespaces_by_name(cleaned: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The authoritative ``SecurityZone`` root of the query, by name."""
    return {ns["name"]: ns for ns in cleaned.get("SecurityZone") or [] if ns.get("name")}
