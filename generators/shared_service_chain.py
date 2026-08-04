"""Shared service chain mixin for DC-scoped firewall/load-balancer provisioning.

This keeps the DC generator focused on orchestration while the dependency
chain for shared services lives in one place.
"""

from __future__ import annotations

from typing import Any

from .protocols import (
    DcimPhysicalDevice,
    IpamNamespace,
    ManagedFirewallHA,
    ManagedLoadbalancerHA,
    TopologyCommonExchange,
)


class SharedServiceChainMixin:
    """Provide shared-service chain helpers for CommonGenerator descendants.

    Expects the host class to also provide ``BorderServicesMixin``'s
    ``_create_role_devices`` and ``CablingMixin``'s ``_cable_border_services``
    — composed directly on the generator alongside this mixin, not through
    inheritance between mixins (each mixin owns one job; the generator wires
    them together).
    """

    client: Any
    logger: Any
    data: Any
    fabric_name: str
    _safe_rel_add: Any
    # BorderServicesMixin._create_role_devices — annotation only, no method body.
    _create_role_devices: Any
    # CablingMixin._cable_border_services — annotation only, no method body.
    _cable_border_services: Any

    async def _generate_dc_shared_service_devices(self, *, border_leaf_names: list[str]) -> None:
        """Create shared DC firewall/load-balancer devices, HA domains, and cabling."""

        data = self.data
        naming_convention = data.get("naming_convention", "standard").lower()
        fabric_templates = data.get("fabric_templates", [])
        firewall_templates = [t for t in fabric_templates if t.get("role") == "firewall" and t.get("quantity", 0) > 0]
        load_balancer_templates = [
            t for t in fabric_templates if t.get("role") == "load-balancer" and t.get("quantity", 0) > 0
        ]
        firewall_names = await self._create_role_devices(
            role="firewall",
            entries=firewall_templates,
            deployment_id=data["id"],
            naming_convention=naming_convention,
            indexes=[data["index"]],
        )
        load_balancer_names = await self._create_role_devices(
            role="load-balancer",
            entries=load_balancer_templates,
            deployment_id=data["id"],
            naming_convention=naming_convention,
            indexes=[data["index"]],
        )

        await self._ensure_service_ha(
            ha_kind=ManagedFirewallHA,
            ha_name=f"{self.fabric_name}-firewall-ha",
            member_device_names=firewall_names,
        )
        await self._ensure_service_ha(
            ha_kind=ManagedLoadbalancerHA,
            ha_name=f"{self.fabric_name}-loadbalancer-ha",
            member_device_names=load_balancer_names,
        )

        await self._cable_border_services(
            border_role_for={"firewall": "firewall", "load-balancer": "load-balancer"},
            connectivity_mode=data.get("connectivity_mode", "pbr"),
            border_names=border_leaf_names,
            firewall_names=firewall_names,
            load_balancer_names=load_balancer_names,
        )

    async def _ensure_service_ha(
        self,
        *,
        ha_kind: type[ManagedFirewallHA] | type[ManagedLoadbalancerHA],
        ha_name: str,
        member_device_names: list[str],
    ) -> None:
        """Create or reconcile a shared HA domain for a pair of DC-scoped service devices."""

        kind_name = ha_kind.__name__

        if len(member_device_names) != 2:
            self.logger.warning(
                f"DC {self.fabric_name}: skipping {kind_name} {ha_name} — expected exactly 2 member devices, "
                f"got {len(member_device_names)}"
            )
            return

        devices = await self.client.filters(kind=DcimPhysicalDevice, name__values=member_device_names)
        if len(devices) != 2:
            self.logger.warning(
                f"DC {self.fabric_name}: skipping {kind_name} {ha_name} — unable to resolve both member devices"
            )
            return

        existing = await self.client.filters(kind=ha_kind, name__value=ha_name)
        if existing:
            ha_obj = existing[0]
        else:
            ha_obj = await self.client.create(
                kind=ha_kind,
                data={
                    "name": ha_name,
                    "status": "active",
                    "mode": "active-passive",
                    "capabilities": [{"id": device.id} for device in devices],
                },
            )
            await ha_obj.save(allow_upsert=True)
            self.logger.info(f"Created shared HA domain {kind_name} {ha_name}")
            self.client.group_context.related_node_ids.append(ha_obj.id)
            return

        capabilities = getattr(ha_obj, "capabilities", None)
        if not capabilities:
            return

        await capabilities.fetch()
        existing_cap_ids = {peer.id for peer in capabilities.peers}
        updated = False
        for device in devices:
            if device.id not in existing_cap_ids:
                await self._safe_rel_add(capabilities, {"id": device.id})
                updated = True
        if updated:
            await ha_obj.save(allow_upsert=True)
            self.logger.info(f"Updated shared HA domain {kind_name} {ha_name}")
        self.client.group_context.related_node_ids.append(ha_obj.id)

    async def _ensure_common_exchange_for_dc(self) -> None:
        """Ensure a default TopologyCommonExchange exists and includes this DC deployment."""

        dc_id = self.data["id"]
        dc_name = self.data["name"]
        try:
            namespaces = await self.client.filters(kind=IpamNamespace, name__value="SHARED-SERVICES")
            if not namespaces:
                self.logger.warning(
                    "Shared services namespace 'SHARED-SERVICES' not found; "
                    f"skipping CommonExchange linking for DC {dc_name}"
                )
                return
            shared_ns = namespaces[0]

            exchanges = await self.client.filters(
                kind=TopologyCommonExchange,
                is_default__value=True,
            )

            exchange_obj = None
            if exchanges:
                exchange_obj = exchanges[0]
                if len(exchanges) > 1:
                    self.logger.warning(
                        "Multiple TopologyCommonExchange objects marked is_default=true; using the first one"
                    )
            else:
                exchange_obj = await self.client.create(
                    kind=TopologyCommonExchange,
                    data={
                        "name": "GLOBAL-SHARED-EXCHANGE",
                        "description": "Auto-provisioned default shared exchange domain",
                        "is_default": True,
                        "namespace": {"id": shared_ns.id},
                        "deployments": [{"id": dc_id}],
                    },
                )
                await exchange_obj.save(allow_upsert=True)
                self.logger.info("Created default CommonExchange 'GLOBAL-SHARED-EXCHANGE' for DC %s", dc_name)
                return

            rel = getattr(exchange_obj, "deployments")
            await rel.fetch()
            if any(peer.id == dc_id for peer in rel.peers):
                return

            await self._safe_rel_add(rel, {"id": dc_id})
            await exchange_obj.save(allow_upsert=True)
            self.logger.info(f"Linked DC {dc_name} to default CommonExchange '{exchange_obj.name.value}'")
        except Exception as exc:
            self.logger.warning(f"Failed to ensure CommonExchange for DC {dc_name}: {exc}")
