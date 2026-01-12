"""Decommission a pod's devices without deleting them.

Sets all devices associated with the pod deployment to status "decommissioned".
Idempotent: re-running on the same pod simply reaffirms the status.
"""

from typing import Any

from infrahub_sdk.batch import InfrahubBatch

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .schema_protocols import (
    DcimCable,
    DcimPhysicalDevice,
    DcimPhysicalInterface,
    DcimVirtualInterface,
    IpamIPAddress,
    IpamPrefix,
)


class PodDecommissionGenerator(CommonGenerator):
    async def generate(self, data: dict[str, Any]) -> None:
        try:
            pod_list_clean = clean_data(data).get("TopologyPod", [])
            if not pod_list_clean:
                self.logger.error("No Pod data found in GraphQL response")
                return

            raw_pod = pod_list_clean[0]
            # Keep raw pod dict; only id/name are required for decommission workflow.
            self.data = raw_pod
        except (ValueError, KeyError, IndexError) as exc:
            self.logger.error("Generation failed due to %s", exc)
            return

        # self.logger.info("Decommissioning pod: %s", json.dumps(self.data, indent=2))

        devices: list = [device.get("id") for device in raw_pod.get("devices", []) if device.get("status") == "active"]

        physical_interfaces: list[str] = [
            interface.get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("status", {}) == "active" and interface.get("typename") == "DcimPhysicalInterface"
        ]

        super_spine_interfaces: list[str] = [
            endpoint.get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimPhysicalInterface"
            for endpoint in (interface.get("cable", {}) or {}).get("endpoints", []) or []
            if endpoint.get("id") and endpoint.get("device", {}).get("role") == "super-spine"
        ]

        virtual_interfaces: list[str] = [
            interface.get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("status", {}) == "active" and interface.get("typename") == "DcimVirtualInterface"
        ]

        p2p_addresses: list[str] = [
            id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimPhysicalInterface"
            for id in [interface.get("ip_address", {}).get("id")]
            if isinstance(id, str)
        ]

        super_spine_p2p_addresses: list[str] = [
            endpoint_ip_id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("status") == "active" and interface.get("typename") == "DcimPhysicalInterface"
            for endpoint in (interface.get("cable", {}) or {}).get("endpoints", []) or []
            if endpoint.get("device", {}).get("role") == "super-spine"
            for endpoint_ip_id in [endpoint.get("ip_address", {}).get("id")]
            if isinstance(endpoint_ip_id, str)
        ]

        virtual_interface_addresses: list[str] = [
            id
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimVirtualInterface"
            for interface_ip in interface.get("ip_addresses", []) or []
            for id in [interface_ip.get("id")]
            if isinstance(id, str)
        ]

        device_primary_addresses: list[str] = [
            id
            for device in self.data.get("devices", []) or []
            for id in [device.get("primary_address", {}).get("id")]
            if isinstance(id, str)
        ]

        cables: list[str] = [
            interface.get("cable", {}).get("id")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("cable", {}).get("id")
        ]

        p2p_preffixes: list[str] = [
            interface.get("ip_address", {}).get("ip_prefix", {}).get("prefix")
            for device in self.data.get("devices", []) or []
            for interface in device.get("interfaces", []) or []
            if interface.get("typename") == "DcimPhysicalInterface"
            and interface.get("ip_address", {}).get("ip_prefix", {}).get("id")
        ]

        # loopback_pool_prefixes: list[str] = list(
        #     {prefix.get("prefix", {}) for prefix in self.data.get("loopback_pool", {}).get("resources", []) or []}
        # )

        # technical_pool_prefixes: list[str] = list(
        #     {prefix.get("prefix", {}) for prefix in self.data.get("prefix_pool", {}).get("resources", []) or []}
        # )

        execute_batch: InfrahubBatch = await self.client.create_batch()

        device_objs: list[DcimPhysicalDevice] = await self.client.filters(
            kind=DcimPhysicalDevice,
            ids=devices,
        )

        for device_obj in device_objs:
            device_obj.status.value = "decommissioned"
            execute_batch.add(task=device_obj.save, allow_upsert=True, node=device_obj)

        async for _node, _ in execute_batch.execute():
            self.logger.info(f"Decomissioned {_node.name.value}")

        # Set all physical interfaces to free and remove descriptions
        execute_batch: InfrahubBatch = await self.client.create_batch()

        interface_objs: list[DcimPhysicalInterface] = await self.client.filters(
            kind=DcimPhysicalInterface,
            ids=list(physical_interfaces + super_spine_interfaces),
        )

        for interface_obj in interface_objs:
            interface_obj.status.value = "free"
            interface_obj.description.value = ""
            execute_batch.add(task=interface_obj.save, allow_upsert=True, node=interface_obj)

        async for _node, _ in execute_batch.execute():
            self.logger.info(
                f"Cleaned interface {_node.device.display_label} - {_node.name.value} status and description"
            )

        # Remove all virtual interfaces
        intefaces_objs: list[DcimVirtualInterface] = await self.client.filters(
            kind=DcimVirtualInterface,
            ids=virtual_interfaces,
        )
        for interface_obj in intefaces_objs:
            await interface_obj.delete()
            self.logger.info(f"deleted virtual interface {interface_obj.hfid}")

        # Wait for other branch tasks triggered by this event before deleting IPs/prefixes
        await self.wait_for_branch_tasks_since_now()

        # Remove all ip addresses
        address_objs: list[IpamIPAddress] = await self.client.filters(
            kind=IpamIPAddress,
            ids=list(
                p2p_addresses + super_spine_p2p_addresses + virtual_interface_addresses + device_primary_addresses
            ),
        )
        for address_obj in address_objs:
            await address_obj.delete()
            self.logger.info(f"deleted address {address_obj.address.value}")

        # Remove all cables attached to pod interfaces (after cleaning interfaces/IPs)
        cable_objs: list[DcimCable] = await self.client.filters(
            kind=DcimCable,
            ids=cables,
        )
        for cable_obj in cable_objs:
            await cable_obj.delete()
            self.logger.info(f"deleted cable {cable_obj.name.value}")

        # Remove all p2p prefixes from pools
        prefix_objs: list[IpamPrefix] = await self.client.filters(
            kind=IpamPrefix,
            prefix__values=p2p_preffixes,
        )
        for prefix_obj in prefix_objs:
            await prefix_obj.delete()
            self.logger.info(f"deleted prefix {prefix_obj.prefix.value}")

        # Remove all pool prefixes
        # prefix_objs: list[IpamPrefix] = await self.client.filters(
        #     kind=IpamPrefix,
        #     ids=list(set(loopback_pool_prefixes + technical_pool_prefixes)),
        # )
        # for prefix_obj in prefix_objs:
        #     await prefix_obj.delete()
        #     self.logger.info(f"deleted prefix {prefix_obj.prefix.value}")
