"""A customer's dedicated pair of virtual firewalls or load balancers: two instances of the *_CUSTOMER_* virtual template,
one hosted on each physical device of the parent's pair, their NICs pinned to the host ports and the two paired into an
HA domain. Three plans (the devices, the NIC pins, the HA domain), each a pure builder over what exists
(generators/dcim/device_plan.py, generators/dcim/virtual_nics.py, generators/capabilities/ha.py), applied in turn: a
pin needs the NICs the server instantiates once a device is saved, and the HA sync port is one of them. Every object is
the customer's own (tracked).
"""

from __future__ import annotations

from typing import Any

from generators.capabilities.ha import plan_virtual_ha_pair, read_virtual_ha_reads, virtual_ha_lock_keys
from generators.customers.firewall_context_spec import _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE
from generators.customers.firewall_transit import dev_id, dev_name
from generators.dcim.controllers import resolve_role_controller
from generators.dcim.device_plan import plan_virtual_device, read_device_state, role_group_name
from generators.dcim.virtual_nics import HostedDevice, pin_virtual_nics
from generators.plan import TopologyPlan
from generators.protocols import DcimVirtualDevice


async def resolve_virtual_template(gen: Any, *, platform: str | None, role: str, size_suffix: str) -> dict[str, Any]:
    """The {id, device_type, platform} reference of the virtual template "{prefix}_{size_suffix}" matching a physical
    platform and role; fails when the platform and role have no mapping or the template does not exist.
    """
    prefix = _VIRTUAL_TEMPLATE_PREFIX_BY_PLATFORM_AND_ROLE.get((platform or "", role))
    if not prefix:
        gen.fail(f"No virtual template mapping for platform={platform} role={role}.")
    template_name = f"{prefix}_{size_suffix}"
    templates = await gen.client.filters(
        kind="TemplateDcimVirtualDevice", template_name__value=template_name, include=["device_type", "platform"]
    )
    if not templates:
        gen.fail(f"No virtual template '{template_name}' found for platform={platform} role={role}.")
    template = templates[0]
    return {
        "id": template.id,
        "device_type": {"id": template.device_type.peer.id},
        "platform": {"id": template.platform.peer.id},
    }


async def ensure_dedicated_pair(
    gen: Any,
    *,
    role: str,
    ha_kind: str,
    physical_devices: list[Any],
    parent_id: str,
    parent_name: str,
    dc_design: str | None,
    name_parts: tuple[str, str],
    controllers: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    """Provision this customer's virtual HA pair of ``role`` on the two ``physical_devices``; returns the HA domain's name
    and its two members as ``{"id", "name"}``. ``name_parts`` is (owner, environment class): the instances are
    ``{owner}-{host}-{class}``, the domain ``{owner}-{first host}-{second host}-ha-{class}``. Fails when it cannot be built:
    a dedicated request is never moved onto the shared pair.
    """
    owner, env_class = name_parts
    if not dc_design:
        gen.fail(f"{parent_name}: no design set — cannot resolve dedicated {role} template size")
    if len(physical_devices) != 2:
        gen.fail(
            f"{parent_name}: expected 2 physical {role} peers for dedicated provisioning, found {len(physical_devices)}"
        )
    hosts = sorted(physical_devices, key=dev_name)
    template = await resolve_virtual_template(
        gen,
        platform=(hosts[0].get("platform") or {}).get("name"),
        role=role,
        size_suffix=f"CUSTOMER_{dc_design}",
    )
    names = [f"{owner}-{dev_name(host)}-{env_class}" for host in hosts]

    # The devices: hosted on the physical pair, from the template, in the role group or its controller
    controller = resolve_role_controller(controllers, device_role=role, template=template, logger=gen.logger)
    reads = await read_device_state(
        gen.client, names, set() if controller else {role_group_name(role)}, kind=DcimVirtualDevice
    )
    devices = TopologyPlan(root_id=None)
    for host, name in zip(hosts, names, strict=True):
        plan_virtual_device(
            devices,
            reads=reads,
            name=name,
            fabric_name=parent_name.lower(),
            role=role,
            deployment_id=parent_id,
            template=template,
            host_id=dev_id(host),
            controller=controller,
        )
    written = await gen.apply_plan(devices)
    members = [{"id": written.nodes[f"device:{name}"].id, "name": name} for name in names]

    # The NICs the server instantiated, each pinned to a port of the host of its device
    for host, member in zip(hosts, members, strict=True):
        await pin_virtual_nics(gen, [HostedDevice(id=member["id"], name=member["name"])], dev_id(host))

    # The HA domain, found or created by name under its lock
    ha_name = f"{owner}-{dev_name(hosts[0])}-{dev_name(hosts[1])}-ha-{env_class}"
    async with gen.plan_locks(*virtual_ha_lock_keys(ha_name, role=role)):
        ha_reads = await read_virtual_ha_reads(gen.client, ha_name=ha_name, role=role, device_names=names)
        pair = TopologyPlan(root_id=None)
        problems: list[str] = []
        plan_virtual_ha_pair(pair, names=names, role=role, ha_name=ha_name, reads=ha_reads, problems=problems)
        if problems:
            gen.fail("; ".join(problems))
        await gen.apply_plan(pair)
    gen.logger.info(f"Dedicated {role} pair {ha_name} ({ha_kind}) for {owner}-{env_class}")
    return ha_name, members
