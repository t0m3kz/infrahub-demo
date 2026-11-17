"""The NICs of a virtual device hosted on a physical one: which interfaces are base NICs and the host port each rides
on. A pin is the NIC's own ``parent`` (the host port, no tag); the host port carries the cable. ``plan_nic_pins`` is
pure; ``pin_virtual_nics`` reads, plans and applies. The NICs belong to the device's template, so a pin is an untracked
update: written while planned, cleared when the NIC has no host port left.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from netutils.interface import sort_interface_list

from generators.plan import NodeSpec, TopologyPlan
from generators.protocols import DcimPhysicalInterface, DcimVirtualInterface
from utils.limits import WAIT_FOR_PEER_GENERATOR

# The roles of the base NICs a virtual template provides; a context sub-interface (role service) is not one.
PINNED_ROLES = ("management", "uplink", "ha")
VIRTUAL_DEVICE_KIND = "DcimVirtualDevice"
NIC_KIND = "DcimVirtualInterface"


@dataclass(frozen=True)
class HostedDevice:
    """A virtual device whose NICs are pinned: its id and name."""

    id: str
    name: str


def is_host_nic(interface: dict[str, Any]) -> bool:
    """True for a cleaned interface that is a base NIC of a virtual device (the kind this module pins to a host port)."""
    return (interface.get("device") or {}).get("typename") == VIRTUAL_DEVICE_KIND and interface.get(
        "role"
    ) in PINNED_ROLES


def pair_nics_with_host_ports(nics: list[Any], host_ports: list[Any]) -> list[tuple[Any, Any]]:
    """(nic, host port) pairs: per role, the n-th NIC by interface order rides the n-th host port; a NIC with none left
    stays unpaired.
    """
    pairs: list[tuple[Any, Any]] = []
    for role in PINNED_ROLES:
        nic_names = {nic.name.value: nic for nic in nics if nic.role.value == role}
        port_names = {port.name.value: port for port in host_ports if port.role.value == role}
        pairs.extend(
            (nic_names[nic_name], port_names[port_name])
            for nic_name, port_name in zip(
                sort_interface_list(list(nic_names)), sort_interface_list(list(port_names)), strict=False
            )
        )
    return pairs


def plan_nic_pins(
    plan: TopologyPlan,
    *,
    devices: Sequence[HostedDevice],
    nics: Sequence[Any],
    host_ports: Sequence[Any],
) -> list[str]:
    """Pin every NIC of ``devices`` to its host port in ``plan`` and return a line per change for the log. A pin is the
    NIC's own ``parent`` (the template created the NIC, so the write is untracked): a NIC already on its host port is
    left alone, a wrong or missing parent is written, and the parent of a NIC with no host port left is cleared. Uplinks
    ride cabled host ports only (the caller filters ``host_ports``).
    """
    changes: list[str] = []
    for device in devices:
        device_nics = [nic for nic in nics if nic.device.id == device.id]
        parents = {nic.id: port for nic, port in pair_nics_with_host_ports(device_nics, list(host_ports))}
        for nic in device_nics:
            host_port = parents.get(nic.id)
            current = getattr(getattr(nic, "parent", None), "id", None)
            if host_port is None:
                if current is not None:
                    plan.add(_nic_spec(device.name, nic, {"id": nic.id}, clear=("parent",)))
                    changes.append(f"  - Unpinned {device.name} {nic.name.value}: no available host port")
                continue
            if current == host_port.id:
                continue
            plan.add(_nic_spec(device.name, nic, {"id": nic.id, "parent": {"id": host_port.id}}))
            changes.append(f"  - Pinned {device.name} {nic.name.value} to host port {host_port.name.value}")
    return changes


def _nic_spec(device_name: str, nic: Any, data: dict[str, Any], clear: tuple[str, ...] = ()) -> NodeSpec:
    """The update of a template NIC by id: untracked, the template owns the interface."""
    return NodeSpec(f"nic:{device_name}/{nic.name.value}", NIC_KIND, data, ownership="template", clear=clear)


async def pin_virtual_nics(gen: Any, devices: Sequence[HostedDevice], host_id: str) -> None:
    """Pin every base NIC of ``devices`` to a port of the device ``host_id``, as one plan. The server instantiates the
    NICs from the template after the device is saved: they are read again until they all exist (or the run fails). A
    NIC with no host port is left unpinned. ``gen``: ``client``, ``logger``, ``_retry_delay``, ``apply_plan``.
    """
    device_ids = [device.id for device in devices]
    nics: list[Any] = []
    for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
        nics = await gen.client.filters(
            kind=DcimVirtualInterface, device__ids=device_ids, role__values=list(PINNED_ROLES), include=["parent"]
        )
        if {nic.device.id for nic in nics} >= set(device_ids):
            break
        if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
            await asyncio.sleep(gen._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt))
    else:
        gen.fail(f"NICs of {[device.name for device in devices]} not instantiated from the template")
    host_ports = [
        port
        for port in await gen.client.filters(
            kind=DcimPhysicalInterface, device__ids=[host_id], role__values=list(PINNED_ROLES)
        )
        if port.role.value != "uplink" or getattr(getattr(port, "cable", None), "id", None)
    ]
    plan = TopologyPlan(root_id=None)
    for line in plan_nic_pins(plan, devices=devices, nics=nics, host_ports=host_ports):
        gen.logger.info(line)
    if plan.specs:
        await gen.apply_plan(plan)
