"""Border-leaf/border-spine firewall/load-balancer provisioning mixin for CommonGenerator.

Shared by dc.py (DC-wide border-leaf) and pod.py (a border-spine pod's own
firewall/load-balancer) — both call these with their own scope's
deployment_id/naming_convention/indexes rather than duplicating the logic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from infrahub_sdk.protocols import CoreStandardGroup

if TYPE_CHECKING:
    import logging

from .protocols import DcimPhysicalDevice
from .types import DeviceOptions


class BorderServicesMixin:
    """Mixin providing HA-pairing and role-device provisioning for CommonGenerator.

    Expects the host class to provide: ``client``, ``logger``, and
    ``create_devices`` (all present on ``CommonGenerator``).
    """

    # Attribute declarations for the type checker — provided by CommonGenerator / InfrahubGenerator
    client: Any
    logger: logging.Logger
    # CommonGenerator.create_devices (DeviceMixin) — annotation only, no method body.
    create_devices: Any

    async def _ensure_ha_pair(
        self,
        device_names: list[str],
        *,
        ha_kind: Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"],
        role_label: str,
    ) -> None:
        """Pair exactly 2 same-role devices into an HA domain. Shared by
        dc.py (DC-wide firewall/load-balancer) and pod.py (a border-spine
        pod's own firewall/load-balancer) — never pairs across pods/DCs,
        both members must sit in front of the same fabric to mean anything
        physically."""
        if len(device_names) != 2:
            return

        first, second = sorted(device_names)
        ha_name = f"{first}-{second}-ha"
        existing = await self.client.filters(kind=ha_kind, name__value=ha_name)
        if existing:
            self.client.group_context.related_node_ids.append(existing[0].id)
            return

        devices = await self.client.filters(kind=DcimPhysicalDevice, name__values=[first, second])
        if len(devices) != 2:
            self.logger.error(f"HA pair {first}/{second}: could not resolve both devices.")
            return

        ha_group = await self.client.get(kind=CoreStandardGroup, name__value="ha_domains")
        ha_obj = await self.client.create(
            kind=ha_kind,
            data={
                "name": ha_name,
                "status": "active",
                "capabilities": [{"id": dev.id} for dev in devices],
                "member_of_groups": [{"id": ha_group.id}],
            },
        )
        await ha_obj.save(allow_upsert=True)
        self.logger.info(f"Created HA domain {ha_name} for {role_label}s")

    async def _create_role_devices(
        self,
        *,
        role: Literal["firewall", "load-balancer"],
        entries: list[Any],
        deployment_id: str,
        naming_convention: Literal["standard", "hierarchical", "flat", "computed"],
        indexes: list[int],
    ) -> list[str]:
        """Create firewall/load-balancer devices for one deployment scope
        (DC-wide from dc.py, or one pod from pod.py), pairing each entry's
        devices into an HA domain when quantity == 2. No loopback allocation
        — not part of underlay/overlay routing.

        Each entry is a fabric_templates row — either a plain ``clean_data()``
        dict (``{"quantity": ..., "template": {...}}``, dc.py) or the legacy
        ``DeviceRole`` Pydantic model (pod.py) — both expose the same two
        fields, just via ``[...]``/``.`` access respectively."""
        device_options = DeviceOptions(indexes=indexes)
        if role == "load-balancer":
            # create_devices()'s default group_name is f"{device_role}s" = "load-balancers",
            # but the bootstrap group is named "loadbalancers" (no hyphen) — override.
            device_options["group_name"] = "loadbalancers"
        ha_kind: Literal["ManagedFirewallHA", "ManagedLoadbalancerHA"] = (
            "ManagedFirewallHA" if role == "firewall" else "ManagedLoadbalancerHA"
        )

        all_names: list[str] = []
        for entry in entries:
            if isinstance(entry, dict):
                quantity = entry["quantity"]
                template = entry["template"]
            else:
                quantity = entry.quantity
                template = entry.template.model_dump()
            names = await self.create_devices(
                deployment_id=deployment_id,
                device_role=role,
                quantity=quantity,
                template=template,
                naming_convention=naming_convention,
                options=device_options,
            )
            all_names.extend(names)
            if quantity == 2:
                await self._ensure_ha_pair(names, ha_kind=ha_kind, role_label=role)

        return all_names
