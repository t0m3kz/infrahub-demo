"""The plan of a group of physical devices of one role: a pure function of the cleaned query data and what already
exists (``DeviceReads``), with no write and no read of anything this run creates (generators/plan.py).

It plans a physical device: the device (role group or controller, a
management address, the template on first creation), and with ``allocate_loopback`` its loopback interface and address.
The MLAG and HA pairs of the devices are planned by ``generators/capabilities/mlag.py`` and ``ha.py``. A virtual device
hosted on a physical one (``plan_virtual_device``) is planned the same way, with its ``hosting_device`` and no template
ports (its NICs are instantiated by the server, read back by ``generators/dcim/virtual_nics.py``).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from typing_extensions import TypedDict

from generators.dcim.template_interfaces import TemplatePort
from generators.helpers.interface_naming import get_loopback_name
from generators.helpers.naming import DeviceNameContext, DeviceNamingConfig, NamingConvention
from generators.plan import (
    AddressAllocationSpec,
    EdgesSpec,
    Hfid,
    NodeRef,
    NodeSpec,
    TopologyPlan,
)
from generators.protocols import DcimVirtualInterface, ManagedController
from utils.limits import ROLE_GROUP_NAMES

DEVICE_KIND = "DcimPhysicalDevice"
VIRTUAL_DEVICE_KIND = "DcimVirtualDevice"
APPLIANCE_KIND = "CloudNetworkAppliance"
INTERFACE_KIND = "DcimPhysicalInterface"
LOOPBACK_KIND = "DcimVirtualInterface"
GROUP_KIND = "CoreStandardGroup"


class DeviceOptions(TypedDict, total=False):
    """What the device group of a plan is told beyond its role, template and quantity (``plan_devices``)."""

    indexes: list[int]
    """Device indexes for hierarchical naming."""
    role_index_start: int
    """role_index of the first device (default 1): a role split over several
    fabric_templates entries continues its numbering across them."""
    allocate_loopback: bool
    """Create Loopback0 interface with IP from loopback pool (default: False)."""
    loopback_pool: Any
    """SDK pool object or pool ID string for loopback IPs."""
    loopback_prefix_length: int
    """Prefix length for loopback IPs: 32 (IPv4) or 128 (IPv6). Default: 32."""
    management_pool: Any
    """SDK pool object or pool ID string for management IPs."""
    rack: str
    """Rack ID for device placement."""
    ha_kind: str
    """When set, create_devices() pairs the created devices two-at-a-time into this HA node kind
    ("ManagedFirewallHA"/"ManagedLoadbalancerHA").
    """
    mlag_create: Literal["no", "back-to-back", "virtual"]
    """Pod-wide MLAG pairing mode for leaf/tor/l2-leaf/access-leaf roles —
    passed through from TopologyPod.mlag_create; the rack plan pairs the devices into domains
    (generators/capabilities/mlag.py). "no" (default) never pairs."""
    mlag_supports_virtual: bool
    """Whether this role can use mlag_create="virtual" (anchors on a loopback —
    only L3/routed roles). False forces back-to-back instead of erroring out.
    Default: True."""


# device_role -> HA node kind (the pairs are planned by generators/capabilities/ha.py)
HA_KIND_BY_ROLE: dict[str, str] = {
    "internal-firewall": "ManagedFirewallHA",
    "external-firewall": "ManagedFirewallHA",
    "load-balancer": "ManagedLoadbalancerHA",
}


def planned_device_names(
    *,
    fabric_name: str,
    pod_name: str | None,
    device_role: str,
    quantity: int,
    naming_convention: NamingConvention,
    options: DeviceOptions,
) -> list[str]:
    """The sorted names a device group is given ``quantity`` devices of ``device_role``: pure, so a plan names its
    devices before they exist.
    """
    naming = DeviceNamingConfig(strategy=naming_convention)
    first_role_index = options.get("role_index_start", 1)
    return sorted(
        naming.format_device_name(
            DeviceNameContext.from_indexes(
                fabric_name=fabric_name,
                device_role=device_role,
                role_index=idx,
                indexes=options.get("indexes", None) or [],
            )
        )
        for idx in range(first_role_index, first_role_index + quantity)
    )


def default_management_pool_name(fabric_name: str) -> str:
    """The management pool a device group falls back to when it is given no pool."""
    return f"{fabric_name}-management-pool"


def default_loopback_pool_name(fabric_name: str, pod_name: str | None, device_role: str) -> str:
    """The loopback pool a device group falls back to when it is given no pool."""
    if device_role in ("super-spine", "border-leaf"):
        # Both are DC-level fabric tiers sharing one fabric-scoped loopback pool (dc.py "dc-fabric-loopback")
        return f"{fabric_name}-dc-fabric-loopback-pool"
    # Other devices use the pod-level loopback pool
    return f"{pod_name or fabric_name}-loopback-pool"


def object_id(value: Any | None) -> Any | None:
    """id of a relationship reference given as None, a ``{"id": ...}`` dict or an SDK node."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", value)


def device_hfid(name: str, kind: str = DEVICE_KIND) -> Hfid:
    return Hfid(kind, (name,))


def port_hfid(device_name: str, port_name: str) -> Hfid:
    return Hfid(INTERFACE_KIND, (device_name, port_name))


def role_group_name(role: str) -> str:
    """The standard group the devices of a role belong to when no controller manages them."""
    return ROLE_GROUP_NAMES.get(role) or f"{role}s"


@dataclass(frozen=True)
class ExistingDevice:
    """A device of an earlier run: its id is not needed (the server finds it by name), its groups and primary address
    are kept.
    """

    group_ids: tuple[str, ...] = ()
    primary_address_id: str | None = None


@dataclass(frozen=True)
class DeviceReads:
    """What exists before the run, by name: devices, the address id of each loopback that has one, the standard groups
    (name -> id). The builder reads nothing else.
    """

    devices: Mapping[str, ExistingDevice] = field(default_factory=dict)
    loopback_address_ids: Mapping[str, str] = field(default_factory=dict)
    groups: Mapping[str, str] = field(default_factory=dict)


async def read_device_state(
    client: Any,
    device_names: Sequence[str],
    group_names: Collection[str],
    *,
    kind: str | type[Any] = DEVICE_KIND,
) -> DeviceReads:
    """Read the existing devices of ``kind`` (``APPLIANCE_KIND`` for a provider-hosted appliance), their loopback
    addresses and the role groups: three reads.
    """
    devices = await client.filters(
        kind=kind,
        name__values=list(device_names),
        include=["member_of_groups", "primary_address"],
    )
    loopbacks = await client.filters(
        kind=DcimVirtualInterface,
        device__name__values=list(device_names),
        role__value="loopback",
        include=["device", "ip_address"],
    )
    groups = await client.filters(kind=GROUP_KIND, name__values=sorted(group_names)) if group_names else []
    return DeviceReads(
        devices={
            device.name.value: ExistingDevice(
                group_ids=tuple(peer.id for peer in device.member_of_groups.peers),
                primary_address_id=_peer_id(getattr(device, "primary_address", None)),
            )
            for device in devices
        },
        loopback_address_ids={
            loopback.device.peer.name.value: address_id
            for loopback in loopbacks
            if (address_id := _peer_id(getattr(loopback, "ip_address", None)))
        },
        groups={group.name.value: group.id for group in groups},
    )


def _peer_id(rel: Any) -> str | None:
    """Peer id of a RelatedNode, None when it is missing or unset (``.peer`` raises when unset)."""
    return rel.peer.id if rel is not None and rel.initialized else None


def plan_physical_devices(
    plan: TopologyPlan,
    *,
    reads: DeviceReads,
    fabric_name: str,
    pod_name: str,
    role: str,
    quantity: int,
    deployment_id: str,
    template: Mapping[str, Any],
    template_ports: Sequence[TemplatePort],
    template_owner_id: str | None,
    naming_convention: NamingConvention,
    options: DeviceOptions,
    controller: Mapping[str, Any] | None,
    kind: str = DEVICE_KIND,
    account_id: str | None = None,
) -> list[str]:
    """Add the devices of one role to ``plan`` and return their names (sorted, as ``create_devices`` names them).
    ``kind`` ``APPLIANCE_KIND``: a provider-hosted appliance, ordered under the account ``account_id`` (none: no
    account); it has no template ports, so ``template_ports`` is empty.

    Per device: its management address (an allocation, unless the device has one), the device itself (tracked; every
    port of its template is ``provides``), its loopback (tracked) with an allocated address, and the role group it
    joins (created, untracked, when it does not exist) or, with a ``controller``, the controller's ``managed_devices``.
    """
    names = planned_device_names(
        fabric_name=fabric_name,
        pod_name=pod_name,
        device_role=role,
        quantity=quantity,
        naming_convention=naming_convention,
        options=options,
    )
    # The payload's owner, else the object template's: a create from the template copies it, an HFID upsert must send it
    owner_id = object_id(template.get("owner")) or template_owner_id
    group_ref = _plan_role_group(plan, reads, role) if controller is None else None
    for name in names:
        existing = reads.devices.get(name)
        primary_address: str | NodeRef
        if existing is not None and existing.primary_address_id:
            primary_address = existing.primary_address_id
        else:
            primary_address = _plan_address(
                plan,
                key=f"management:{name}",
                identifier=name,
                prefix_length=32,
                description=f"Management IP for {name}",
                pool_id=options.get("management_pool"),
                pool_name=default_management_pool_name(fabric_name),
            )
        groups: list[str | NodeRef] = []
        if group_ref is not None:
            groups = [*(existing.group_ids if existing is not None else ())]
            if group_ref not in groups:
                groups.append(group_ref)
        rack = options.get("rack", "")
        plan.add(
            NodeSpec(
                key=f"device:{name}",
                kind=kind,
                data={
                    "name": name,
                    # object_template only on first creation: re-sending it on an existing device fails server-side
                    **({} if existing is not None else {"object_template": {"id": template.get("id")}}),
                    "status": "active",
                    "role": role,
                    "deployment": {"id": deployment_id} if deployment_id else None,
                    "device_type": template.get("device_type"),
                    "platform": template.get("platform"),
                    **({"owner": {"id": owner_id}} if owner_id else {}),
                    **({"account": {"id": account_id} if account_id else None} if kind == APPLIANCE_KIND else {}),
                    "primary_address": primary_address,
                    # An appliance has no rack: the provider hosts it
                    **({} if kind == APPLIANCE_KIND else {"rack": {"id": rack} if rack else None}),
                    "member_of_groups": [{"id": group} for group in groups],
                },
                ownership="owned",
                hfid=device_hfid(name, kind),
                provides=tuple(port_hfid(name, port.name) for port in template_ports),
            )
        )
        if options.get("allocate_loopback"):
            loopback_name = get_loopback_name(template.get("platform"), 0)
            loopback_address = reads.loopback_address_ids.get(name) or _plan_address(
                plan,
                key=f"loopback-address:{name}",
                identifier=name,
                prefix_length=options.get("loopback_prefix_length", 32),
                description=f"Loopback IP for {name}",
                pool_id=options.get("loopback_pool"),
                pool_name=default_loopback_pool_name(fabric_name, pod_name, role),
            )
            plan.add(
                NodeSpec(
                    key=f"loopback:{name}",
                    kind=LOOPBACK_KIND,
                    data={
                        "name": loopback_name,
                        "description": "Loopback interface",
                        "device": device_hfid(name, kind),
                        "status": "active",
                        "role": "loopback",
                        "ip_address": loopback_address,
                    },
                    ownership="owned",
                    hfid=Hfid(LOOPBACK_KIND, (name, loopback_name)),
                )
            )
    if controller is not None:
        plan.add(
            EdgesSpec(
                node=controller["id"],
                relation="managed_devices",
                peers=tuple(NodeRef(f"device:{name}") for name in names),
                kind=ManagedController,
            )
        )
    return names


def _plan_role_group(plan: TopologyPlan, reads: DeviceReads, role: str) -> str | NodeRef:
    """The standard group the devices of ``role`` join: its id when it exists, else a reference to the shared group the
    plan creates (once).
    """
    group_name = role_group_name(role)
    existing = reads.groups.get(group_name)
    if existing is not None:
        return existing
    ref = NodeRef(f"group:{group_name}")
    if not any(isinstance(spec, NodeSpec) and spec.key == ref.key for spec in plan.specs):
        plan.add(
            NodeSpec(
                key=ref.key,
                kind=GROUP_KIND,
                data={"name": group_name, "description": f"Auto-created by generator for role {role}"},
                hfid=Hfid(GROUP_KIND, (group_name,)),
                ownership="shared",
            )
        )
    return ref


def plan_virtual_device(
    plan: TopologyPlan,
    *,
    reads: DeviceReads,
    name: str,
    fabric_name: str,
    role: str,
    deployment_id: str,
    template: Mapping[str, Any],
    host_id: str,
    controller: Mapping[str, Any] | None,
) -> None:
    """Add one virtual device named ``name``, hosted on the physical device ``host_id``, to ``plan``: tracked, from its
    virtual ``template`` on first creation, with a management address from the fabric's pool (an allocation, unless it
    has one) and the role group it joins or, with a ``controller``, the controller's ``managed_devices``. No loopback.
    """
    existing = reads.devices.get(name)
    group_ref = _plan_role_group(plan, reads, role) if controller is None else None
    groups: list[str | NodeRef] = []
    if group_ref is not None:
        groups = [*(existing.group_ids if existing is not None else ())]
        if group_ref not in groups:
            groups.append(group_ref)
    owner_id = object_id(template.get("owner"))
    primary_address: str | NodeRef = (
        existing.primary_address_id
        if existing is not None and existing.primary_address_id
        else _plan_address(
            plan,
            key=f"management:{name}",
            identifier=name,
            prefix_length=32,
            description=f"Management IP for {name}",
            pool_id=None,
            pool_name=default_management_pool_name(fabric_name),
        )
    )
    plan.add(
        NodeSpec(
            key=f"device:{name}",
            kind=VIRTUAL_DEVICE_KIND,
            data={
                "name": name,
                **({} if existing is not None else {"object_template": {"id": template.get("id")}}),
                "status": "active",
                "role": role,
                "deployment": {"id": deployment_id} if deployment_id else None,
                "device_type": template.get("device_type"),
                "platform": template.get("platform"),
                **({"owner": {"id": owner_id}} if owner_id else {}),
                "hosting_device": {"id": host_id},
                "primary_address": primary_address,
                "member_of_groups": [{"id": group} for group in groups],
            },
            ownership="owned",
            hfid=device_hfid(name, VIRTUAL_DEVICE_KIND),
        )
    )
    if controller is not None:
        plan.add(
            EdgesSpec(
                node=controller["id"],
                relation="managed_devices",
                peers=(NodeRef(f"device:{name}"),),
                kind=ManagedController,
            )
        )


def _plan_address(
    plan: TopologyPlan,
    *,
    key: str,
    identifier: str,
    prefix_length: int,
    description: str,
    pool_id: str | NodeRef | None,
    pool_name: str,
) -> NodeRef:
    """Add the allocation of one address and return the reference to it."""
    plan.add(
        AddressAllocationSpec(
            key=key,
            identifier=identifier,
            prefix_length=prefix_length,
            description=description,
            ownership="shared",
            pool_id=pool_id,
            pool_name=None if pool_id else pool_name,
        )
    )
    return NodeRef(key)
