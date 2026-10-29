"""Pins the NICs of a virtual instance to the ports of the physical device hosting it.

A virtual machine has no cable of its own: its traffic leaves through the physical device that hosts
it. Each base NIC the machine's template provides (a DcimVirtualInterface) therefore points, through
``parent_interface``, to the host port it rides on, and the host port carries the cable. A context
sub-interface sits on the NIC, so border leaf -> host port -> NIC -> sub-interface is a path in the graph.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from netutils.interface import sort_interface_list

from utils.limits import WAIT_FOR_PEER_GENERATOR

from .protocols import DcimPhysicalInterface, DcimVirtualInterface

if TYPE_CHECKING:
    import logging

    from .common import CommonGenerator

# The roles of the base NICs a virtual template provides; a context sub-interface (role service) is not one.
PINNED_ROLES = ("management", "uplink", "ha")


def pair_nics_with_host_ports(nics: list[Any], host_ports: list[Any]) -> list[tuple[Any, Any]]:
    """(nic, host port) pairs: per role, the n-th NIC by interface order rides the n-th host port.

    A NIC with no host port of its role left stays unpaired (a machine can have more uplinks than its host).
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
    """Mixin providing the NIC pinning for DeviceMixin.

    Expects the host class to provide: ``client``, ``logger`` and ``_retry_delay``.
    """

    client: Any
    logger: logging.Logger
    # CommonGenerator._retry_delay — annotation only, no method body.
    if TYPE_CHECKING:
        _retry_delay = staticmethod(CommonGenerator._retry_delay)

    async def _pin_virtual_nics(self, devices: list[Any], host_id: str) -> None:
        """Set parent_interface of every base NIC of devices to a port of the device host_id.

        The server instantiates the template's NICs after the device is saved, so the read is retried
        until every device has some. Idempotent; the NICs belong to the template, not to this run, so
        they are saved untracked and never become a delete_unused_nodes candidate.
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
        for device in devices:
            device_nics = [nic for nic in nics if nic.device.id == device.id]
            for nic, host_port in pair_nics_with_host_ports(device_nics, host_ports):
                if getattr(nic.parent_interface, "id", None) == host_port.id:
                    continue
                pinned = await self.client.create(
                    kind=DcimVirtualInterface,
                    data={
                        "id": nic.id,
                        "name": nic.name.value,
                        "device": {"id": device.id},
                        "parent_interface": {"id": host_port.id},
                    },
                )
                await pinned.save(allow_upsert=True, update_group_context=False)
                self.logger.info(f"  - Pinned {device.name.value} {nic.name.value} to host port {host_port.name.value}")
