from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from .common import CommonGenerator
from .schema_protocols import DcimPhysicalDevice, DcimPhysicalInterface


class DeviceTypeReplacementGenerator(CommonGenerator):
    """Align device interfaces to a new template while preserving services and IPs."""

    async def generate(self, data: dict[str, Any]) -> None:
        payload = clean_data(data)
        device_entries = payload.get("DcimPhysicalDevice") or []
        template_entries = payload.get("TemplateDcimPhysicalDevice") or []

        if not device_entries:
            self.logger.error("No device data found in GraphQL payload")
            return

        device_info = device_entries[0]
        template_info = template_entries[0] if template_entries else None

        device_id = device_info.get("id")
        device_name = device_info.get("name")
        device_role = device_info.get("role")
        device_type = device_info.get("device_type")

        if not device_id:
            self.logger.error("Device payload missing id")
            return

        if not template_info:
            self.logger.error(
                "No template found for device %s with type=%s role=%s",
                device_name,
                device_type,
                device_role,
            )
            return

        template_interfaces = template_info.get("interfaces") or []
        template_map = {iface.get("name"): iface for iface in template_interfaces if iface.get("name")}

        if not template_map:
            self.logger.warning(
                "Template %s for device %s has no interfaces; skipping retype",
                template_info.get("template_name") or template_info.get("id"),
                device_name,
            )
            return

        template_names = set(template_map.keys())

        existing_interfaces_data = device_info.get("interfaces") or []
        attachments_by_id = {iface.get("id"): iface for iface in existing_interfaces_data if iface.get("id")}

        device = await self.client.get(DcimPhysicalDevice, id=device_id)
        if not device:
            self.logger.error("Device %s not found", device_id)
            return

        template_id = template_info.get("id")
        device_changed = False
        if template_id and (not getattr(device, "object_template", None) or device.object_template.id != template_id):
            # SDK accepts RelatedNode assignment via mapping; type checker needs override.
            device.object_template = {"id": template_id}  # type: ignore[assignment]
            device_changed = True

        interface_nodes = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=[device_id],
        )

        interface_by_name = {iface.name.value: iface for iface in interface_nodes}
        existing_names = set(interface_by_name.keys())

        def has_attachment(iface: DcimPhysicalInterface) -> bool:
            details = attachments_by_id.get(iface.id, {})
            return bool(details.get("cable") or details.get("ip_address") or details.get("interface_services"))

        updated_existing = 0
        for name in sorted(template_names & existing_names):
            iface = interface_by_name[name]
            template_iface = template_map[name]

            changed = False
            target_role = template_iface.get("role")
            if target_role and getattr(iface, "role", None) and iface.role.value != target_role:
                iface.role.value = target_role
                changed = True

            target_type = template_iface.get("interface_type")
            if target_type and getattr(iface, "interface_type", None) and iface.interface_type.value != target_type:
                iface.interface_type.value = target_type
                changed = True

            target_status = template_iface.get("status")
            if target_status and getattr(iface, "status", None) and iface.status.value != target_status:
                iface.status.value = target_status
                changed = True
            elif getattr(iface, "status", None) and not iface.status.value:
                iface.status.value = "active"
                changed = True

            if changed:
                await iface.save(allow_upsert=True)
                updated_existing += 1

        missing_names = [name for name in template_names if name not in existing_names]
        surplus_names = [name for name in existing_names if name not in template_names]

        surplus_ordered = sorted(
            surplus_names,
            key=lambda nm: (1 if has_attachment(interface_by_name[nm]) else 0, nm),
            reverse=True,
        )

        reused = 0
        created = 0
        disabled = 0
        deleted = 0

        for missing in missing_names:
            template_iface = template_map[missing]
            source_name = surplus_ordered.pop(0) if surplus_ordered else None

            if source_name:
                iface = interface_by_name.pop(source_name)
                iface.name.value = missing

                if template_iface.get("role") and getattr(iface, "role", None):
                    iface.role.value = template_iface["role"]

                if template_iface.get("interface_type") and getattr(iface, "interface_type", None):
                    iface.interface_type.value = template_iface["interface_type"]

                if getattr(iface, "status", None):
                    iface.status.value = template_iface.get("status") or iface.status.value or "active"

                await iface.save(allow_upsert=True)
                interface_by_name[missing] = iface
                reused += 1
            else:
                iface = await self.client.create(
                    kind=DcimPhysicalInterface,
                    data={
                        "name": missing,
                        "device": {"id": device_id},
                        "role": template_iface.get("role"),
                        "interface_type": template_iface.get("interface_type"),
                        "status": template_iface.get("status") or "active",
                    },
                )
                await iface.save(allow_upsert=True)
                interface_by_name[missing] = iface
                created += 1

        remaining_surplus = [name for name in interface_by_name if name not in template_names]
        for name in remaining_surplus:
            iface = interface_by_name[name]
            if has_attachment(iface):
                if getattr(iface, "status", None) and iface.status.value != "disabled":
                    iface.status.value = "disabled"
                    await iface.save(allow_upsert=True)
                    disabled += 1
            else:
                await iface.delete()
                deleted += 1

        if device_changed:
            await device.save(allow_upsert=True)

        self.logger.info(
            "Retyped device %s (%s) to %s: updated=%s reused=%s created=%s disabled=%s deleted=%s",
            device_name,
            device_id,
            device_type,
            updated_existing,
            reused,
            created,
            disabled,
            deleted,
        )
