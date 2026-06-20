"""Generator: set interface descriptions based on the remote device name.

For every cable in a deployment, each endpoint interface gets its description
set to the name of the device on the opposite end of the cable.

Example: DC1-POD1-SP1:Ethernet1/1 → description "DC1-SS1"
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator


class InterfaceDescriptionGenerator(CommonGenerator):
    """Set interface description = remote device name for every cabled interface."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        deployments = cleaned.get("TopologyPhysicalDeployment", [])
        if not deployments:
            self.logger.error("No TopologyPhysicalDeployment data found")
            return

        deployment = deployments[0]
        deployment_name = deployment.get("name", "unknown")
        cables = deployment.get("cables", [])

        self.logger.info(f"Setting interface descriptions for {deployment_name} ({len(cables)} cables)")

        updated = 0
        for cable in cables:
            endpoints = cable.get("endpoints", [])
            if len(endpoints) != 2:
                continue

            ep_a, ep_b = endpoints[0], endpoints[1]

            dev_a = ep_a.get("device", {}).get("name", "")
            dev_b = ep_b.get("device", {}).get("name", "")

            if not dev_a or not dev_b:
                continue

            for iface_id, remote_device in [(ep_a["id"], dev_b), (ep_b["id"], dev_a)]:
                iface = await self.client.get(kind="DcimPhysicalInterface", id=iface_id)
                if iface.description.value != remote_device:
                    iface.description.value = remote_device
                    await iface.save(allow_upsert=True)
                    updated += 1

        self.logger.info(f"Updated {updated} interface descriptions in {deployment_name}")
