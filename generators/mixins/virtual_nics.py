"""Pins the NICs of a virtual instance to the ports of the physical device hosting it. Each base NIC points through
``ManagedInterfaceEncapsulation`` (no tag) to its host port; the host port carries the cable.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from netutils.interface import sort_interface_list

from generators.protocols import DcimPhysicalInterface, DcimVirtualInterface, ManagedInterfaceEncapsulation
from utils.interface_encapsulation import encapsulation_name
from utils.limits import WAIT_FOR_PEER_GENERATOR

if TYPE_CHECKING:
    import logging

    from generators.mixins.common import CommonGenerator

# The roles of the base NICs a virtual template provides; a context sub-interface (role service) is not one.
PINNED_ROLES = ("management", "uplink", "ha")
VIRTUAL_DEVICE_KIND = "DcimVirtualDevice"


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


class VirtualNicMixin:
    """Mixin providing the NIC pinning for DeviceMixin; the host provides ``client``, ``logger`` and ``_retry_delay``."""

    client: Any
    logger: logging.Logger
    # CommonGenerator._retry_delay — annotation only, no method body.
    if TYPE_CHECKING:
        _retry_delay = staticmethod(CommonGenerator._retry_delay)

    async def _pin_virtual_nics(self, devices: list[Any], host_id: str) -> None:
        """Pin every base NIC of devices to a port of the device host_id (its encapsulation, with that port as parent),
        untracked. Retried until the server has instantiated the template's NICs; uplinks ride cabled host ports only, a
        NIC with no port is unpinned.
        """
        device_ids = [device.id for device in devices]
        nics: list[Any] = []
        for attempt in range(WAIT_FOR_PEER_GENERATOR.attempts):
            nics = await self.client.filters(
                kind=DcimVirtualInterface, device__ids=device_ids, role__values=list(PINNED_ROLES)
            )
            if {nic.device.id for nic in nics} >= set(device_ids):
                break
            if attempt < WAIT_FOR_PEER_GENERATOR.attempts - 1:
                await asyncio.sleep(self._retry_delay(WAIT_FOR_PEER_GENERATOR.delay, attempt))
        else:
            self.logger.error(f"NICs of {[device.name.value for device in devices]} not instantiated from the template")
            return

        host_ports = await self.client.filters(
            kind=DcimPhysicalInterface, device__ids=[host_id], role__values=list(PINNED_ROLES)
        )
        host_ports = [
            port
            for port in host_ports
            if port.role.value != "uplink" or getattr(getattr(port, "cable", None), "id", None)
        ]
        names = {
            nic.id: encapsulation_name(device.name.value, nic.name.value)
            for device in devices
            for nic in nics
            if nic.device.id == device.id
        }
        pins = {
            pin.name.value: pin
            for pin in await self.client.filters(kind=ManagedInterfaceEncapsulation, name__values=list(names.values()))
        }
        for device in devices:
            device_nics = [nic for nic in nics if nic.device.id == device.id]
            parents = {nic.id: host_port for nic, host_port in pair_nics_with_host_ports(device_nics, host_ports)}
            for nic in device_nics:
                host_port = parents.get(nic.id)
                parent_id = host_port.id if host_port is not None else None
                pin = pins.get(names[nic.id])
                if getattr(getattr(pin, "parent", None), "id", None) == parent_id:
                    continue
                if host_port is None:
                    # The parent is mandatory: a stale pin goes with its object.
                    if pin is not None:
                        await pin.delete()
                    self.logger.info(f"  - Unpinned {device.name.value} {nic.name.value}: no available host port")
                    continue
                encapsulation = await self.client.create(
                    kind=ManagedInterfaceEncapsulation,
                    data={"name": names[nic.id], "parent": {"id": parent_id}, "capabilities": [{"id": nic.id}]},
                )
                await encapsulation.save(allow_upsert=True, update_group_context=False)
                self.logger.info(f"  - Pinned {device.name.value} {nic.name.value} to host port {host_port.name.value}")
