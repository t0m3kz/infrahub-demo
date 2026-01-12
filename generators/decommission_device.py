"""Device decommission generators (role-specific wrappers over a shared teardown)."""

from typing import Any, Iterable, Optional, Set

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .schema_protocols import DcimCable, DcimPhysicalDevice, DcimPhysicalInterface, DcimVirtualInterface, IpamIPAddress


class BaseDeviceDecommissionGenerator(CommonGenerator):
    """Shared device teardown; subclasses constrain allowed roles."""

    ALLOWED_ROLES: Optional[set[str]] = None

    async def generate(self, data: dict[str, Any]) -> None:
        try:
            devices = clean_data(data).get("DcimPhysicalDevice", [])
            if not devices:
                self.logger.error("No device data found in GraphQL response")
                return

            device_data = devices[0]
            device_id = device_data.get("id")
            device_name = device_data.get("name") or {}
            device_name_value = device_name.get("value") if isinstance(device_name, dict) else device_name
            device_role = device_data.get("role")
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Failed to parse device payload: %s", exc)
            return

        if not device_id:
            self.logger.error("Device payload missing id; skipping")
            return

        if self.ALLOWED_ROLES and device_role not in self.ALLOWED_ROLES:
            self.logger.info("Skipping device %s (%s) with role %s", device_name_value, device_id, device_role)
            return

        self.logger.info("Decommissioning device %s (%s)", device_name_value, device_id)

        await self._teardown_device(device_data)

    async def _teardown_device(self, device_data: dict[str, Any]) -> None:
        device_id = device_data.get("id")
        if not device_id:
            return

        primary_address_id = device_data.get("primary_address", {}).get("id")

        physical_iface_ids: list[str] = []
        virtual_iface_ids: list[str] = []
        cable_ids: Set[str] = set()
        remote_iface_ids: Set[str] = set()
        ip_ids: Set[str] = set()

        for iface in device_data.get("interfaces", []) or []:
            iface_id = iface.get("id")
            if not iface_id:
                continue

            typename = iface.get("typename")
            if typename == "DcimPhysicalInterface":
                physical_iface_ids.append(iface_id)
                ip_id = iface.get("ip_address", {}).get("id")
                if ip_id:
                    ip_ids.add(ip_id)

                cable = iface.get("cable", {}) or {}
                cable_id = cable.get("id")
                if cable_id:
                    cable_ids.add(cable_id)

                for endpoint in cable.get("endpoints", []) or []:
                    endpoint_id = endpoint.get("id")
                    if endpoint_id and endpoint_id != iface_id:
                        remote_iface_ids.add(endpoint_id)

            elif typename == "DcimVirtualInterface":
                virtual_iface_ids.append(iface_id)
                for ip_data in iface.get("ip_addresses", []) or []:
                    ip_id = ip_data.get("id")
                    if ip_id:
                        ip_ids.add(ip_id)

        if primary_address_id:
            ip_ids.add(primary_address_id)

        # Delete cables first to detach endpoints
        await self._delete_cables(cable_ids)

        # Free remote interfaces that were connected to this device
        await self._free_interfaces(remote_iface_ids)

        # Clean local physical interfaces (status, description, ip_address)
        await self._clean_physical_interfaces(physical_iface_ids)

        # Drop virtual interfaces
        await self._delete_virtual_interfaces(virtual_iface_ids)

        # Remove IP addresses after interfaces are detached
        await self._delete_ip_addresses(ip_ids)

        # Clear primary address and mark device as decommissioned (retain for audit)
        device_obj = await self.client.get(DcimPhysicalDevice, id=device_id)
        if device_obj:
            device_obj.primary_address = None  # type: ignore
            device_obj.status.value = "decommissioned"
            await device_obj.save(allow_upsert=True)

        self.logger.info(
            "Device %s decommissioned (retained): %s cables, %s interfaces, %s IPs",
            device_id,
            len(cable_ids),
            len(physical_iface_ids) + len(virtual_iface_ids),
            len(ip_ids),
        )

    async def _delete_cables(self, cable_ids: Set[str]) -> None:
        if not cable_ids:
            return

        cable_objs = await self.client.filters(kind=DcimCable, ids=list(cable_ids))
        for cable_obj in cable_objs:
            await cable_obj.delete()

    async def _free_interfaces(self, iface_ids: Set[str]) -> None:
        if not iface_ids:
            return

        iface_objs = await self.client.filters(kind=DcimPhysicalInterface, ids=list(iface_ids))
        for iface in iface_objs:
            iface.status.value = "free"
            iface.description.value = ""
            await iface.save(allow_upsert=True)

    async def _clean_physical_interfaces(self, iface_ids: Iterable[str]) -> None:
        ids = list(iface_ids)
        if not ids:
            return

        iface_objs = await self.client.filters(kind=DcimPhysicalInterface, ids=ids)
        for iface in iface_objs:
            iface.status.value = "free"
            iface.description.value = ""
            iface.ip_address = None  # type: ignore
            await iface.save(allow_upsert=True)

    async def _delete_virtual_interfaces(self, iface_ids: Iterable[str]) -> None:
        ids = list(iface_ids)
        if not ids:
            return

        iface_objs = await self.client.filters(kind=DcimVirtualInterface, ids=ids)
        for iface in iface_objs:
            await iface.delete()

    async def _delete_ip_addresses(self, ip_ids: Set[str]) -> None:
        if not ip_ids:
            return

        ip_objs = await self.client.filters(kind=IpamIPAddress, ids=list(ip_ids))
        for ip_obj in ip_objs:
            await ip_obj.delete()


class LeafDecommissionGenerator(BaseDeviceDecommissionGenerator):
    ALLOWED_ROLES = {"leaf"}


class SpineDecommissionGenerator(BaseDeviceDecommissionGenerator):
    ALLOWED_ROLES = {"spine"}


class TorDecommissionGenerator(BaseDeviceDecommissionGenerator):
    ALLOWED_ROLES = {"tor"}


class EndpointDecommissionGenerator(BaseDeviceDecommissionGenerator):
    ALLOWED_ROLES = {"endpoint"}


class DeviceDecommissionGenerator(BaseDeviceDecommissionGenerator):
    """Fallback generator for any role (no role filtering)."""

    ALLOWED_ROLES = None
