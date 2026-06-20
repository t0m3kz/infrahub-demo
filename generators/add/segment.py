"""Generators for network segment deployment activations.

Two generators handle different segment types:
  - VlanSegmentGenerator:  traditional 802.1Q VLAN (allocates VLAN ID only)
  - VxlanSegmentGenerator: VXLAN overlay (allocates VLAN ID + VNI)

Both share the same base class that handles:
  1. Determine target deployments from segment.deployments
  2. For each target deployment, create (or upsert) a ManagedSegmentDeployment
     with locally-allocated VLAN ID (and VNI for VXLAN) from DC pool ranges.

VLAN IDs and VNIs are allocated from the DC's CoreNumberPool via from_pool.
The idempotency check (existing SegmentDeployment lookup) ensures from_pool
is only called for genuinely new deployments, avoiding double allocation.
"""

from __future__ import annotations

from typing import Any

from utils.data_cleaning import clean_data

from ..common import CommonGenerator
from ..protocols import DcimPhysicalDevice, DcimPhysicalInterface

# Typename constants for both segment types
MANAGED_SEGMENT_TYPES = ("ManagedVlanSegment", "ManagedVxlanSegment")


class BaseSegmentGenerator(CommonGenerator):
    """Shared segment activation logic for VLAN and VXLAN segments."""

    # Subclasses set this to their concrete GraphQL root key
    graphql_root_key: str = ""
    # Whether this generator allocates VNI from vni_pool
    allocate_vni: bool = False

    async def generate(self, data: dict[str, Any]) -> None:
        """Create or upsert ManagedSegmentDeployment records for the segment."""
        cleaned = clean_data(data)
        segment_list = cleaned.get(self.graphql_root_key, [])
        if not segment_list:
            self.logger.error(f"No {self.graphql_root_key} data in GraphQL response")
            return

        segment = segment_list[0]
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")

        if not segment_id or not segment_name:
            self.logger.error("Segment missing id or name — cannot proceed")
            return

        self.logger.info(f"Processing segment: {segment_name}")

        # ----------------------------------------------------------------
        # Resolve target deployments directly from segment
        # VlanSegment: single deployment (cardinality one, key "deployment")
        # VxlanSegment: multiple deployments (cardinality many, key "deployments")
        # ----------------------------------------------------------------
        raw = segment.get("deployments") or segment.get("deployment")
        if isinstance(raw, dict):
            target_deployments: list[dict] = [raw]
        else:
            target_deployments: list[dict] = raw or []

        if not target_deployments:
            self.logger.warning(f"Segment {segment_name} has no target deployments (assign deployment to the segment)")
            return

        self.logger.info(
            f"Segment {segment_name} will be activated in "
            f"{len(target_deployments)} deployment(s): "
            f"{[d.get('name', d.get('id')) for d in target_deployments]}"
        )

        # ----------------------------------------------------------------
        # Create/upsert SegmentDeployment per deployment
        # ----------------------------------------------------------------
        for dep in target_deployments:
            dep_id: str = dep.get("id", "")
            dep_name: str = dep.get("name", dep_id)
            if not dep_id:
                self.logger.warning("Deployment entry missing id — skipping")
                continue

            await self._activate_segment_in_deployment(
                segment_id=segment_id,
                segment_name=segment_name,
                deployment_id=dep_id,
                deployment_name=dep_name,
            )

    async def _activate_segment_in_deployment(
        self,
        segment_id: str,
        segment_name: str,
        deployment_id: str,
        deployment_name: str,
    ) -> None:
        """Create or upsert one SegmentDeployment record.

        VLAN ID is always allocated from the DC's vlan_pool via from_pool.
        VNI is allocated from vni_pool only for VXLAN segments.
        Idempotency: checks for existing SegmentDeployment first — from_pool
        is only called for genuinely new deployments.
        """
        # --- Check idempotency first ---
        try:
            existing = await self.client.filters(
                kind="ManagedSegmentDeployment",
                segment__ids=[segment_id],
                deployment__ids=[deployment_id],
            )
            if existing:
                self.logger.info(
                    f"  [{deployment_name}] SegmentDeployment already exists for {segment_name} — skipping"
                )
                await existing[0].save()  # register with tracker
                return
        except Exception as exc:
            self.logger.warning(f"  [{deployment_name}] Error checking existing activations: {exc}")

        # --- VLAN ID (always from pool via from_pool) ---
        vlan_pool = await self._get_dc_pool(deployment_id, deployment_name, "vlan_pool")
        if vlan_pool is None:
            self.logger.error(
                f"  [{deployment_name}] No vlan_pool for {segment_name}. Attach a CoreNumberPool to the DC's vlan_pool."
            )
            return

        # Unique identifier per segment+deployment ensures stable allocation
        vlan_identifier = f"{segment_id}-{deployment_id}-vlan"
        self.logger.info(f"  [{deployment_name}] Allocating VLAN ID from pool {vlan_pool.name.value}")

        # --- VNI (only for VXLAN segments) ---
        # VNI must be globally consistent — the same segment must carry the same VNI
        # in every DC so that EVPN type-2/3 routes stitch correctly across DCI.
        # Strategy: reuse the VNI already allocated for another DC's SegmentDeployment
        # of the same segment. Only fall back to pool allocation when this is the
        # first DC to activate the segment (no prior SegmentDeployment exists yet).
        vni_from_pool: dict[str, Any] | None = None
        vni_literal: int | None = None
        if self.allocate_vni:
            # Check if another DC already allocated a VNI for this segment
            try:
                existing_deployments = await self.client.filters(
                    kind="ManagedSegmentDeployment",
                    segment__ids=[segment_id],
                )
                for ed in existing_deployments:
                    await ed.resolve()
                    existing_vni = getattr(ed, "vni", None)
                    if existing_vni and getattr(existing_vni, "value", None):
                        vni_literal = existing_vni.value
                        self.logger.info(
                            f"  [{deployment_name}] Reusing VNI {vni_literal} "
                            f"from existing SegmentDeployment for {segment_name}"
                        )
                        break
            except Exception as exc:
                self.logger.warning(f"  [{deployment_name}] Could not check existing VNIs: {exc}")

            if vni_literal is None:
                # First DC to activate this segment — allocate from pool
                vni_pool = await self._get_dc_pool(deployment_id, deployment_name, "vni_pool")
                if vni_pool is not None:
                    vni_identifier = f"{segment_id}-vni"
                    vni_from_pool = {"from_pool": {"id": vni_pool.id}, "identifier": vni_identifier}
                    self.logger.info(f"  [{deployment_name}] Allocating VNI from pool {vni_pool.name.value}")
                else:
                    self.logger.warning(
                        f"  [{deployment_name}] No vni_pool — VXLAN segment {segment_name} "
                        "will not have L2 VNI allocated"
                    )

        # --- Create SegmentDeployment with pool-allocated values ---
        activation_data: dict[str, Any] = {
            "vlan_id": {"from_pool": {"id": vlan_pool.id}, "identifier": vlan_identifier},
            "segment": {"id": segment_id},
            "deployment": {"id": deployment_id},
            "status": "provisioning",
        }
        if vni_literal is not None:
            activation_data["vni"] = vni_literal
        elif vni_from_pool is not None:
            activation_data["vni"] = vni_from_pool

        try:
            activation = await self.client.create(
                kind="ManagedSegmentDeployment",
                data=activation_data,
            )
            await activation.save(allow_upsert=True)
            self.logger.info(
                f"  [{deployment_name}] SegmentDeployment saved "
                f"(segment={segment_name}, vlan=from_pool, vni={'from_pool' if vni_from_pool else 'none'})"
            )
        except Exception as exc:
            self.logger.error(f"  [{deployment_name}] Failed to create SegmentDeployment for {segment_name}: {exc}")

    async def _get_dc_pool(self, deployment_id: str, deployment_name: str, pool_attr: str) -> Any:
        """Fetch a pool (vlan_pool, vni_pool, l3_vni_pool) from the TopologyDataCenter.

        Returns the pool SDK object, or None if not found.
        Uses a per-run cache to avoid re-fetching the DC for each pool attribute.
        """
        cache = getattr(self, "_dc_cache", {})
        if deployment_id not in cache:
            try:
                cache[deployment_id] = await self.client.get(
                    kind="TopologyDataCenter",
                    id=deployment_id,
                    include=["vlan_pool", "vni_pool", "l3_vni_pool"],
                    prefetch_relationships=True,
                )
                self._dc_cache = cache
            except Exception:
                self.logger.debug(f"  [{deployment_name}] Could not fetch DC {deployment_id}")
                return None

        dc = cache.get(deployment_id)
        pool_rel = getattr(dc, pool_attr, None) if dc else None
        pool_peer = getattr(pool_rel, "peer", None) if pool_rel else None
        if pool_peer and getattr(pool_peer, "id", None):
            return pool_peer
        self.logger.debug(f"  [{deployment_name}] No {pool_attr} on deployment {deployment_id}")
        return None


class VlanSegmentGenerator(BaseSegmentGenerator):
    """VLAN segment generator — allocates only VLAN ID from vlan_pool."""

    graphql_root_key = "ManagedVlanSegment"
    allocate_vni = False


class VxlanSegmentGenerator(BaseSegmentGenerator):
    """VXLAN segment generator — allocates VLAN ID + VNI from pools, then assigns
    the segment to all leaf/tor customer-facing interfaces in each target deployment.
    This populates interface_capabilities so the leaf transform can generate VXLAN config.
    """

    graphql_root_key = "ManagedVxlanSegment"
    allocate_vni = True

    async def generate(self, data: dict[str, Any]) -> None:
        await super().generate(data)
        await self._assign_to_deployment_interfaces(data)

    async def _assign_to_deployment_interfaces(self, data: dict[str, Any]) -> None:
        """Assign this segment to all leaf/tor customer-facing interfaces in each deployment."""
        cleaned = clean_data(data)
        segment_list = cleaned.get(self.graphql_root_key, [])
        if not segment_list:
            return

        segment = segment_list[0]
        segment_id: str = segment.get("id", "")
        segment_name: str = segment.get("name", "")
        raw = segment.get("deployments") or segment.get("deployment")
        target_deployments: list[dict] = [raw] if isinstance(raw, dict) else (raw or [])

        if not segment_id or not target_deployments:
            return

        segment_obj = await self.client.get(kind="ManagedVxlanSegment", id=segment_id)
        if not segment_obj:
            self.logger.warning(f"Could not fetch segment SDK object for {segment_name}")
            return

        for dep in target_deployments:
            dep_id: str = dep.get("id", "")
            dep_name: str = dep.get("name", dep_id)
            if not dep_id:
                continue
            await self._assign_segment_to_dc_interfaces(
                segment_id=segment_id,
                segment_obj=segment_obj,
                segment_name=segment_name,
                deployment_id=dep_id,
                deployment_name=dep_name,
            )

    async def _assign_segment_to_dc_interfaces(
        self,
        segment_id: str,
        segment_obj: Any,
        segment_name: str,
        deployment_id: str,
        deployment_name: str,
    ) -> None:
        """Find all leaf/tor customer-facing interfaces in a DC and add the segment
        to their interface_capabilities relationship (queried by the leaf transform)."""
        devices = await self.client.filters(
            kind=DcimPhysicalDevice,
            deployment__ids=[deployment_id],
            role__values=["leaf", "tor"],
        )
        if not devices:
            self.logger.debug(f"  [{deployment_name}] No leaf/tor devices — skipping interface assignment")
            return

        device_ids = [d.id for d in devices]
        interfaces = await self.client.filters(
            kind=DcimPhysicalInterface,
            device__ids=device_ids,
            role__value="customer",
        )
        if not interfaces:
            self.logger.debug(f"  [{deployment_name}] No customer/downlink interfaces — skipping")
            return

        assigned = 0
        for iface in interfaces:
            iface_services = getattr(iface, "interface_capabilities")
            await iface_services.fetch()
            existing_ids = {peer.id for peer in iface_services.peers}
            if segment_id not in existing_ids:
                await iface_services.add(segment_obj)
            iface.status.value = "active"
            await iface.save(allow_upsert=True)
            if segment_id not in existing_ids:
                assigned += 1

        self.logger.info(
            f"  [{deployment_name}] Assigned segment '{segment_name}' to {assigned} interface(s) "
            f"({len(interfaces) - assigned} already assigned)"
        )
