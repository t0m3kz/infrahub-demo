"""Decommission a pod's devices without deleting them.

Sets all devices associated with the pod deployment to status "decommissioned".
Idempotent: re-running on the same pod simply reaffirms the status.
"""

from typing import Any, Set

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .models import PodModel
from .schema_protocols import DcimCable, DcimPhysicalDevice, DcimPhysicalInterface


class PodDecommissionGenerator(CommonGenerator):
    async def generate(self, data: dict[str, Any]) -> None:
        try:
            deployment_list = clean_data(data).get("TopologyPod", [])
            if not deployment_list:
                self.logger.error("No Pod data found in GraphQL response")
                return

            self.data = PodModel(**deployment_list[0])
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error("Generation failed due to %s", exc)
            return

        pod_id = self.data.id
        pod_name = self.data.name
        self.logger.info("Decommissioning pod %s (%s)", pod_name, pod_id)

        # Fetch all devices tied to this pod via deployment relation (spines, leafs, tors, etc.)
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            deployment__ids=[pod_id],
        )

        if not devices:
            self.logger.info("No devices found for pod %s", pod_name)
            return

        device_ids = [d.id for d in devices]

        # Fetch all physical interfaces for these devices (with cable relationship)
        interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=device_ids,
            include=["cable"],
        )

        cables_to_delete: Set[str] = set()
        remote_interfaces_to_free: Set[str] = set()

        # Collect cables and remote interfaces
        for iface in interfaces:
            cable = iface.cable.peer if iface.cable else None
            cable_id = cable.id if cable and cable.id else None
            if not cable_id:
                continue

            cables_to_delete.add(cable_id)

            # Fetch cable endpoints to find remote interface
            cable_full = await self.client.get(DcimCable, id=cable_id, include=["endpoints"])
            endpoints = cable_full.endpoints.peers if cable_full and cable_full.endpoints else []
            for endpoint in endpoints:
                if endpoint.id and endpoint.id != iface.id:
                    remote_interfaces_to_free.add(endpoint.id)

        # Delete cables
        for cable_id in cables_to_delete:
            cable_obj = await self.client.get(DcimCable, id=cable_id)
            if cable_obj:
                await cable_obj.delete()

        # Free remote interfaces that were connected to decommissioned pod devices
        for iface_id in remote_interfaces_to_free:
            remote_iface = await self.client.get(DcimPhysicalInterface, id=iface_id)
            if remote_iface and remote_iface.status.value != "free":
                remote_iface.status.value = "free"
                await remote_iface.save(allow_upsert=True)

        # Finally, mark pod devices as decommissioned
        updated = 0
        for device in devices:
            if device.status.value != "decommissioned":
                device.status.value = "decommissioned"
                await device.save(allow_upsert=True)
                updated += 1

        self.logger.info(
            "Decommissioned %s devices for pod %s, removed %s cables, freed %s remote interfaces",
            updated,
            pod_name,
            len(cables_to_delete),
            len(remote_interfaces_to_free),
        )
