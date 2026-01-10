"""Decommission a single device while preserving audit history.

Actions:
- Delete all cables connected to this device's physical interfaces.
- Mark remote interfaces that were connected as free (reusable).
- Mark this device as decommissioned and disable its interfaces.

Idempotent: safe to run multiple times.
"""

from typing import Any, Set

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .schema_protocols import DcimCable, DcimPhysicalDevice, DcimPhysicalInterface


class DeviceDecommissionGenerator(CommonGenerator):
    async def generate(self, data: dict[str, Any]) -> None:
        try:
            devices = clean_data(data).get("DcimPhysicalDevice", [])
            if not devices:
                self.logger.error("No device data found in GraphQL response")
                return

            device_data = devices[0]
            device_id = device_data["id"]
            device_name = device_data["name"]["value"]
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to parse device payload: %s", exc)
            return

        self.logger.info("Decommissioning device %s (%s)", device_name, device_id)

        device = await self.client.get(DcimPhysicalDevice, id=device_id)
        if not device:
            self.logger.error("Device %s not found", device_id)
            return

        # Fetch all physical interfaces for this device (with cables)
        interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[device_id],
            include=["cable"],
        )

        cables_to_delete: Set[str] = set()
        remote_ifaces_to_free: Set[str] = set()

        for iface in interfaces:
            cable = iface.cable.peer if iface.cable else None
            cable_id = cable.id if cable and cable.id else None
            if not cable_id:
                continue

            cables_to_delete.add(cable_id)

            cable_full = await self.client.get(DcimCable, id=cable_id, include=["endpoints"])
            endpoints = cable_full.endpoints.peers if cable_full and cable_full.endpoints else []
            for endpoint in endpoints:
                if endpoint.id and endpoint.id != iface.id:
                    remote_ifaces_to_free.add(endpoint.id)

        # Delete cables
        for cable_id in cables_to_delete:
            cable_obj = await self.client.get(DcimCable, id=cable_id)
            if cable_obj:
                await cable_obj.delete()

        # Free remote interfaces
        for iface_id in remote_ifaces_to_free:
            remote_iface = await self.client.get(DcimPhysicalInterface, id=iface_id)
            if remote_iface and remote_iface.status.value != "free":
                remote_iface.status.value = "free"
                await remote_iface.save(allow_upsert=True)

        # Disable local interfaces on the decommissioned device
        for iface in interfaces:
            if iface.status.value != "disabled":
                iface.status.value = "disabled"
                await iface.save(allow_upsert=True)

        # Mark device as decommissioned
        if device.status.value != "decommissioned":
            device.status.value = "decommissioned"
            await device.save(allow_upsert=True)

        self.logger.info(
            "Device %s decommissioned: removed %s cables, freed %s remote interfaces",
            device_name,
            len(cables_to_delete),
            len(remote_ifaces_to_free),
        )
