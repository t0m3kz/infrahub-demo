"""Generator: MLAG peer-link wiring.

For each ManagedMLAG node:
  1. Assert both peer devices have this MLAG domain in their capabilities
     (device.capabilities → ManagedMLAG, identifier=capabilities).
  2. Create or locate the peer-link interface on each device:
     - virtual_peer_link=false (default): create DcimLAGInterface with all
       physical interfaces (role=mlag-peer) as member_interfaces, named using
       vendor-correct convention (Port-Channel100, port-channel100, lag-100, …).
     - virtual_peer_link=true: create DcimVirtualInterface as the peer-link.
  3. Wire interface_capabilities on the ManagedMLAG node to the peer-link interfaces.

Idempotent: skips relationships and interfaces that are already wired.
"""

from __future__ import annotations

from typing import Any

from infrahub_sdk.generator import InfrahubGenerator

from utils.data_cleaning import clean_data

from ..helpers.interface_naming import get_lag_name, get_loopback_name
from ..logger import FailOnErrorLoggerMixin

_PEER_LINK_LAG_ID = 100


class MLAGGenerator(FailOnErrorLoggerMixin, InfrahubGenerator):
    """Wire capabilities and peer-link interfaces for both devices in an MLAG domain."""

    async def generate(self, data: dict[str, Any]) -> None:
        cleaned = clean_data(data)
        mlag_nodes: list[dict] = cleaned.get("ManagedMLAG", [])

        if not mlag_nodes:
            self.logger.error("No ManagedMLAG found in query response")
            return

        for mlag in mlag_nodes:
            await self._process_mlag_domain(mlag)

    async def _process_mlag_domain(self, mlag: dict) -> None:
        mlag_id: str = mlag["id"]
        mlag_name: str = mlag["name"]
        virtual_peer_link: bool = bool(mlag.get("virtual_peer_link", False))
        devices: list[dict] = mlag.get("capabilities", [])

        if len(devices) != 2:
            self.logger.error(
                "[%s] Expected exactly 2 peer devices in capabilities, got %d",
                mlag_name,
                len(devices),
            )
            return

        self.logger.info(
            f"Processing MLAG domain {mlag_name} ({'virtual' if virtual_peer_link else 'physical'} peer-link)"
        )

        mlag_obj = await self.client.get(kind="ManagedMLAG", id=mlag_id)

        peer_link_iface_ids: list[str] = []

        for device in devices:
            dev_id: str = device["id"]
            dev_name: str = device["name"]
            platform: str = (device.get("platform") or {}).get("name") or ""

            await self._assert_capability(mlag_obj, mlag_id, mlag_name, dev_id, dev_name)

            iface_id = await self._ensure_peer_link(
                mlag_obj=mlag_obj,
                mlag_id=mlag_id,
                mlag_name=mlag_name,
                dev_id=dev_id,
                dev_name=dev_name,
                platform=platform,
                interfaces=device.get("interfaces", []),
                virtual_peer_link=virtual_peer_link,
            )
            if iface_id:
                peer_link_iface_ids.append(iface_id)

        await self._assert_interface_capabilities(mlag_obj, mlag_name, peer_link_iface_ids)

        if not virtual_peer_link:
            await self._ensure_peer_link_cables(mlag_name, devices)

    async def _assert_capability(
        self,
        mlag_obj: Any,
        mlag_id: str,
        mlag_name: str,
        dev_id: str,
        dev_name: str,
    ) -> None:
        """Ensure the device has ManagedMLAG in its capabilities."""
        device_obj = await self.client.get(kind="DcimPhysicalDevice", id=dev_id)
        caps = getattr(device_obj, "capabilities")
        await caps.fetch()

        existing_ids = {peer.id for peer in caps.peers}
        if mlag_id in existing_ids:
            self.logger.info(f"  [{dev_name}] capability already set — ensuring state")
        else:
            self.logger.info(f"  [{dev_name}] Adding MLAG capability → {mlag_name}")
        caps.add(mlag_obj)
        await device_obj.save(allow_upsert=True)

    async def _ensure_peer_link(
        self,
        mlag_obj: Any,
        mlag_id: str,
        mlag_name: str,
        dev_id: str,
        dev_name: str,
        platform: str,
        interfaces: list[dict],
        virtual_peer_link: bool,
    ) -> str | None:
        """Create or locate the peer-link interface on this device. Returns its ID."""
        if virtual_peer_link:
            return await self._ensure_virtual_peer_link(mlag_name, dev_id, dev_name, platform, interfaces)
        return await self._ensure_lag_peer_link(mlag_obj, mlag_id, mlag_name, dev_id, dev_name, platform, interfaces)

    async def _ensure_lag_peer_link(
        self,
        mlag_obj: Any,
        mlag_id: str,
        mlag_name: str,
        dev_id: str,
        dev_name: str,
        platform: str,
        interfaces: list[dict],
    ) -> str | None:
        """Create or locate a DcimLAGInterface peer-link, bundling mlag-peer physical interfaces."""
        lag_name = get_lag_name(platform, _PEER_LINK_LAG_ID)

        # Always activate all mlag-peer physical interfaces (idempotent)
        member_iface_ids = [
            i["id"] for i in interfaces if i.get("typename") == "DcimPhysicalInterface" and i.get("role") == "mlag-peer"
        ]

        if not member_iface_ids:
            self.logger.error(
                "[%s] No physical interfaces with role=mlag-peer found — cannot create peer-link LAG",
                dev_name,
            )
            return None

        for iid in member_iface_ids:
            member_obj = await self.client.get(kind="DcimPhysicalInterface", id=iid)
            getattr(member_obj, "status").value = "active"
            await member_obj.save(allow_upsert=True)

        # Check if peer-link LAG already exists
        existing_lag = next(
            (i for i in interfaces if i.get("typename") == "DcimLAGInterface" and i.get("role") == "mlag-peer"),
            None,
        )

        if existing_lag:
            existing_id = existing_lag["id"]
            self.logger.info(f"  [{dev_name}] Peer-link LAG {existing_lag['name']} already exists — ensuring state")
            lag_obj = await self.client.get(kind="DcimLAGInterface", id=existing_id)
            getattr(lag_obj, "status").value = "active"
            current_domain = existing_lag.get("mlag_domain")
            if not current_domain or current_domain.get("id") != mlag_id:
                lag_obj.mlag_domain = mlag_obj  # type: ignore[attr-defined]
                self.logger.info(f"  [{dev_name}:{existing_lag['name']}] Wired mlag_domain → {mlag_name}")
            await lag_obj.save(allow_upsert=True)
            return existing_id

        self.logger.info(f"  [{dev_name}] Creating peer-link LAG {lag_name} with {len(member_iface_ids)} member(s)")

        lag_obj = await self.client.create(
            kind="DcimLAGInterface",
            data={
                "name": lag_name,
                "device": {"id": dev_id},
                "status": "active",
                "role": "mlag-peer",
                "lag_id": _PEER_LINK_LAG_ID,
                "lacp_mode": "active",
                "mtu": 9000,
                "minimum_links": 1,
                "mlag_domain": {"id": mlag_id},
                "member_interfaces": [{"id": iid} for iid in member_iface_ids],
            },
        )
        await lag_obj.save(allow_upsert=True)
        self.logger.info(f"  [{dev_name}] Created peer-link LAG {lag_name} ({lag_obj.id})")
        return lag_obj.id

    async def _ensure_peer_link_cables(
        self,
        mlag_name: str,
        devices: list[dict],
    ) -> None:
        """Create DcimCable for each pair of physical mlag-peer interfaces between the two devices."""
        if len(devices) != 2:
            return

        dev_a, dev_b = devices[0], devices[1]
        dev_a_name: str = dev_a["name"]
        dev_b_name: str = dev_b["name"]
        _dep = dev_a.get("deployment") or {}
        deployment_id: str | None = (_dep.get("parent") or {}).get("id") or _dep.get("id")

        mlag_peer_a = sorted(
            [
                i
                for i in dev_a.get("interfaces", [])
                if i.get("typename") == "DcimPhysicalInterface" and i.get("role") == "mlag-peer"
            ],
            key=lambda i: i["name"],
        )
        mlag_peer_b = sorted(
            [
                i
                for i in dev_b.get("interfaces", [])
                if i.get("typename") == "DcimPhysicalInterface" and i.get("role") == "mlag-peer"
            ],
            key=lambda i: i["name"],
        )

        if not mlag_peer_a or not mlag_peer_b:
            return

        pairs = list(zip(mlag_peer_a, mlag_peer_b))
        for idx, (iface_a, iface_b) in enumerate(pairs, start=1):
            cable_name = f"CBL-{mlag_name}-PL{idx}"
            iface_a_id = iface_a["id"]
            iface_b_id = iface_b["id"]

            existing = await self.client.filters(kind="DcimCable", name__value=cable_name)
            if existing:
                self.logger.info(f"  [{mlag_name}] Cable {cable_name} already exists — ensuring state")
                await existing[0].save(allow_upsert=True)
                continue

            # Check if either endpoint already has a cable (orphan from a partial run)
            orphan_id = (iface_a.get("cable") or {}).get("id") or (iface_b.get("cable") or {}).get("id")
            if orphan_id:
                orphan_obj = await self.client.get(kind="DcimCable", id=orphan_id)
                old_name = getattr(orphan_obj, "name").value
                getattr(orphan_obj, "name").value = cable_name
                await orphan_obj.save(allow_upsert=True)
                self.logger.info(f"  [{mlag_name}] Adopted orphan cable {old_name} → renamed to {cable_name}")
                continue

            self.logger.info(
                f"  [{mlag_name}] Creating peer-link cable {cable_name}: "
                f"{dev_a_name}:{iface_a['name']} ↔ {dev_b_name}:{iface_b['name']}"
            )

            cable_data: dict = {
                "name": cable_name,
                "type": "smf",
                "endpoints": [iface_a_id, iface_b_id],
            }
            if deployment_id:
                cable_data["deployment"] = {"id": deployment_id}
            cable_obj = await self.client.create(kind="DcimCable", data=cable_data)
            await cable_obj.save(allow_upsert=True)
            self.logger.info(f"  [{mlag_name}] Created {cable_name}")

    async def _ensure_virtual_peer_link(
        self,
        mlag_name: str,
        dev_id: str,
        dev_name: str,
        platform: str,
        interfaces: list[dict],
    ) -> str | None:
        """Create or locate a loopback virtual peer-link (e.g. Loopback100 / loopback-100)."""
        loopback_name = get_loopback_name(platform, 100)

        existing_virt = next(
            (i for i in interfaces if i.get("typename") == "DcimVirtualInterface" and i.get("role") == "mlag-peer"),
            None,
        )

        if existing_virt:
            self.logger.info(
                f"  [{dev_name}] Virtual peer-link {existing_virt['name']} already exists — ensuring state"
            )
            virt_obj = await self.client.get(kind="DcimVirtualInterface", id=existing_virt["id"])
            getattr(virt_obj, "status").value = "active"
            await virt_obj.save(allow_upsert=True)
            return existing_virt["id"]

        self.logger.info(f"  [{dev_name}] Creating virtual peer-link loopback {loopback_name}")

        virt_obj = await self.client.create(
            kind="DcimVirtualInterface",
            data={
                "name": loopback_name,
                "device": {"id": dev_id},
                "status": "active",
                "role": "mlag-peer",
                "description": f"MLAG virtual peer-link — {mlag_name}",
            },
        )
        await virt_obj.save(allow_upsert=True)
        self.logger.info(f"  [{dev_name}] Created virtual peer-link {loopback_name} ({virt_obj.id})")
        return virt_obj.id

    async def _assert_interface_capabilities(
        self,
        mlag_obj: Any,
        mlag_name: str,
        iface_ids: list[str],
    ) -> None:
        """Ensure ManagedMLAG.interface_capabilities contains the peer-link interfaces."""
        iface_caps = getattr(mlag_obj, "interface_capabilities")
        await iface_caps.fetch()
        existing_ids = {peer.id for peer in iface_caps.peers}

        added = False
        for iface_id in iface_ids:
            if iface_id in existing_ids:
                continue
            # Try LAG first, fall back to virtual
            try:
                iface_obj = await self.client.get(kind="DcimLAGInterface", id=iface_id)
            except Exception:
                iface_obj = await self.client.get(kind="DcimVirtualInterface", id=iface_id)
            self.logger.info(f"  [{mlag_name}] Adding peer-link {iface_id} to interface_capabilities")
            iface_caps.add(iface_obj)
            added = True

        if added:
            await mlag_obj.save(allow_upsert=True)
