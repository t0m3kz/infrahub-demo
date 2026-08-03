"""Generator: ManagedHAInterface auto-creation.

For each ManagedFirewallHA or ManagedLoadbalancerHA node, creates one
ManagedHAInterface per member device, wiring it to the device's HA sync
interface (identified by role=management and description containing
"HA sync" or "HA mirror").

Idempotent: skips devices that already have an HAInterface in this domain.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from utils.data_cleaning import clean_data

from ..logger import FailOnErrorLoggerMixin
from ..protocols import DcimCable, DcimVirtualInterface, ManagedGeneric, ManagedHAInterface


def _is_sync_iface(iface: dict) -> bool:
    return iface.get("role") == "ha"


def _iface_kind(typename: str) -> str:
    """Map GraphQL __typename to the DcimInterface kind string."""
    return typename if typename else "DcimPhysicalInterface"


def _device_kind(typename: str) -> str:
    """Map GraphQL __typename to the device kind string."""
    return typename if typename else "DcimPhysicalDevice"


class HAGenerator(FailOnErrorLoggerMixin, InfrahubGenerator):
    """Create ManagedHAInterface nodes for all member devices in an HA domain."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)

        # Collect HA domain nodes from both FW and LB query roots
        ha_nodes: list[dict] = []
        for root_key in ("ManagedFirewallHA", "ManagedLoadbalancerHA"):
            ha_nodes.extend(cleaned.get(root_key, []))

        if not ha_nodes:
            self.logger.error("No HA domain found in query response")
            return

        for ha in ha_nodes:
            await self._process_ha_domain(ha)

    async def _ensure_ha_cables(
        self,
        ha_name: str,
        devices: list[dict],
    ) -> None:
        """Create DcimCable for each pair of HA sync interfaces between the two peer devices."""
        if len(devices) != 2:
            return

        dev_a, dev_b = devices[0], devices[1]
        dev_a_name: str = dev_a["name"]
        dev_b_name: str = dev_b["name"]
        _dep = dev_a.get("deployment") or {}
        deployment_id: str | None = (_dep.get("parent") or {}).get("id") or _dep.get("id")

        ha_ifaces_a = sorted(
            [i for i in dev_a.get("interfaces", []) if i.get("role") == "ha"],
            key=lambda i: i["name"],
        )
        ha_ifaces_b = sorted(
            [i for i in dev_b.get("interfaces", []) if i.get("role") == "ha"],
            key=lambda i: i["name"],
        )

        if not ha_ifaces_a or not ha_ifaces_b:
            return

        for idx, (iface_a, iface_b) in enumerate(zip(ha_ifaces_a, ha_ifaces_b), start=1):
            cable_name = f"CBL-{ha_name}-SYNC{idx if idx > 1 else ''}"
            iface_a_id = iface_a["id"]
            iface_b_id = iface_b["id"]

            existing = await self.client.filters(kind=DcimCable, name__value=cable_name)
            if existing:
                self.logger.info(f"  [{ha_name}] Cable {cable_name} already exists — ensuring state")
                await existing[0].save(allow_upsert=True)
                continue

            # Check if either endpoint already has a cable (orphan from a partial run)
            orphan_id = (iface_a.get("cable") or {}).get("id") or (iface_b.get("cable") or {}).get("id")
            if orphan_id:
                orphan_obj = await self.client.get(kind=DcimCable, id=orphan_id)
                old_name = getattr(orphan_obj, "name").value
                getattr(orphan_obj, "name").value = cable_name
                await orphan_obj.save(allow_upsert=True)
                self.logger.info(f"  [{ha_name}] Adopted orphan cable {old_name} → renamed to {cable_name}")
                continue

            self.logger.info(
                f"  [{ha_name}] Creating HA sync cable {cable_name}: "
                f"{dev_a_name}:{iface_a['name']} ↔ {dev_b_name}:{iface_b['name']}"
            )

            cable_data: dict = {
                "name": cable_name,
                "type": "smf",
                "endpoints": [iface_a_id, iface_b_id],
            }
            if deployment_id:
                cable_data["deployment"] = {"id": deployment_id}
            cable_obj = await self.client.create(kind=DcimCable, data=cable_data)
            await cable_obj.save(allow_upsert=True)
            self.logger.info(f"  [{ha_name}] Created {cable_name}")

    async def _process_ha_domain(self, ha: dict) -> None:
        ha_id: str = ha["id"]
        ha_name: str = ha["name"]
        devices: list[dict] = ha.get("capabilities", [])

        self.logger.info(f"Processing HA domain {ha_name} ({len(devices)} devices)")

        ha_obj = await self.client.get(kind=ManagedGeneric, id=ha_id)

        for device in devices:
            dev_id: str = device["id"]
            dev_name: str = device["name"]
            dev_kind: str = _device_kind(device.get("typename", "DcimPhysicalDevice"))
            device["resolved_kind"] = dev_kind

            try:
                device_obj = await self.client.get(kind=dev_kind, id=dev_id)
            except Exception as exc:
                self.logger.warning(
                    "  [%s] Unable to load capability member (%s:%s): %s",
                    dev_name,
                    dev_kind,
                    dev_id,
                    exc,
                )
                continue

            caps = getattr(device_obj, "capabilities", None)
            if not caps:
                self.logger.info("  [%s] %s has no capabilities relation — skipping", dev_name, dev_kind)
                continue

            await caps.fetch()
            existing_cap_ids = {peer.id for peer in caps.peers}
            if ha_id not in existing_cap_ids:
                self.logger.info(f"  [{dev_name}] Adding HA capability → {ha_name}")
            else:
                self.logger.info(f"  [{dev_name}] capability already set — ensuring state")
            caps.add(ha_obj)
            await device_obj.save(allow_upsert=True)

        # Fetch existing HAInterface nodes for this domain — for idempotency
        existing = await self.client.filters(
            kind=ManagedHAInterface,
            ha_domain__ids=[ha_id],
        )
        existing_iface_ids = set()
        for node in existing:
            iface_caps = getattr(node, "interface_capabilities", None)
            if iface_caps:
                await iface_caps.fetch()
                for peer in iface_caps.peers:
                    existing_iface_ids.add(peer.id)

        for device in devices:
            dev_name: str = device["name"]
            dev_id: str = device["id"]
            dev_kind: str = device.get("resolved_kind", _device_kind(device.get("typename", "")))
            interfaces: list[dict] = device.get("interfaces", [])

            sync_ifaces = [i for i in interfaces if _is_sync_iface(i)]
            if not sync_ifaces:
                if dev_kind == "DcimVirtualDevice":
                    self.logger.info("[%s] Virtual device has no HA sync interface — creating one", dev_name)
                    candidate_name = "eth7"
                    existing = await self.client.filters(
                        kind=DcimVirtualInterface,
                        device__ids=[dev_id],
                        name__value=candidate_name,
                    )

                    if existing:
                        sync_obj = existing[0]
                        getattr(sync_obj, "role").value = "ha"
                        getattr(sync_obj, "status").value = "active"
                        getattr(sync_obj, "description").value = f"HA sync — {dev_name}"
                        await sync_obj.save(allow_upsert=True)
                        sync_ifaces = [
                            {
                                "id": sync_obj.id,
                                "name": getattr(sync_obj, "name").value,
                                "typename": "DcimVirtualInterface",
                                "role": "ha",
                            }
                        ]
                    else:
                        sync_obj = await self.client.create(
                            kind=DcimVirtualInterface,
                            data={
                                "name": candidate_name,
                                "device": {"id": dev_id},
                                "status": "active",
                                "role": "ha",
                                "description": f"HA sync — {dev_name}",
                            },
                        )
                        await sync_obj.save(allow_upsert=True)
                        sync_ifaces = [
                            {
                                "id": sync_obj.id,
                                "name": candidate_name,
                                "typename": "DcimVirtualInterface",
                                "role": "ha",
                            }
                        ]
                else:
                    self.logger.error("[%s] No HA sync interface found (expected role=ha)", dev_name)

            for iface in sync_ifaces:
                iface_id = str(iface.get("id") or "")
                iface_name = str(iface.get("name") or "")
                iface_typename = str(iface.get("typename") or "DcimPhysicalInterface")

                # Always activate the HA sync interface (idempotent)
                iface_obj = await self.client.get(kind=_iface_kind(iface_typename), id=iface_id)
                getattr(iface_obj, "status").value = "active"
                await iface_obj.save(allow_upsert=True)

                if iface_id in existing_iface_ids:
                    self.logger.info(f"  [{dev_name}:{iface_name}] HAInterface already exists — ensuring state")
                    # Re-apply active status on the existing HAInterface node
                    for node in existing:
                        iface_caps_check = getattr(node, "interface_capabilities", None)
                        if iface_caps_check:
                            await iface_caps_check.fetch()
                            if any(peer.id == iface_id for peer in iface_caps_check.peers):
                                getattr(node, "status").value = "active"
                                await node.save(allow_upsert=True)
                                break
                    continue

                # Name: <DEVICE>-HA-SYNC or -HA-MIRROR depending on interface type
                suffix = "HA-MIRROR" if "mirror" in iface_name.lower() else "HA-SYNC"
                node_name = f"{dev_name}-{suffix}"

                self.logger.info(f"  [{dev_name}:{iface_name}] Creating ManagedHAInterface {node_name}")

                ha_iface = await self.client.create(
                    kind=ManagedHAInterface,
                    data={
                        "name": node_name,
                        "link_type": "sync",
                        "status": "active",
                        "description": f"HA link — {dev_name}:{iface_name}",
                        "ha_domain": {"id": ha_id},
                    },
                )
                await ha_iface.save(allow_upsert=True)

                # Re-fetch so relationship managers are initialized
                ha_iface = await self.client.get(kind=ManagedHAInterface, id=ha_iface.id)

                # Wire interface_capabilities → the physical/virtual interface
                iface_caps = getattr(ha_iface, "interface_capabilities")
                await iface_caps.fetch()
                iface_caps.add(iface_obj)
                await ha_iface.save(allow_upsert=True)

                self.logger.info(f"  [{dev_name}:{iface_name}] Created {node_name}")

        # Create HA sync cables between peer devices
        physical_devices = [d for d in devices if d.get("resolved_kind") == "DcimPhysicalDevice"]
        if len(physical_devices) == len(devices):
            await self._ensure_ha_cables(ha_name, devices)
        else:
            self.logger.info("[%s] Skipping cable creation for non-physical HA domain", ha_name)
